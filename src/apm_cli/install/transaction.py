"""Canonical completion and rollback owner for one install attempt."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import weakref
from collections.abc import Callable, Iterator
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from apm_cli.cache.commit_reachability import open_verdict_stage
from apm_cli.core.scope import manifest_file
from apm_cli.install.locking import acquire_lifecycle_lock
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.marketplace.version_pins import open_ref_pin_stage
from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from apm_cli.cache.deploy_store import StagedEvidence
    from apm_cli.core.command_logger import InstallLogger, _ValidationOutcome
    from apm_cli.install.deploy_snapshot import DeployJournal


class ApplyRestoreIncomplete(Exception):
    """A failed fast apply could not put every path back; the full run must not start."""

    def __init__(self, issues: list[tuple[Path, str]]) -> None:
        self.issues = issues
        super().__init__(
            "Some deployed files could not be restored. Close programs using them, "
            "then copy them back from the retained pre-install copies before retrying."
        )


@contextlib.contextmanager
def journaled_apply(contain: Path) -> Iterator[DeployJournal]:
    """Journal one verified fast apply (install/diff_apply.py) like an install's deploys.

    The caller records each path it creates; every path written at the
    integrator write seam inside the block is recorded, with its expected
    bytes and its post-write seal, too. If the block raises, every recorded
    path is restored before the exception propagates; a restore that leaves
    any path behind raises :class:`ApplyRestoreIncomplete`. Call it under the
    lifecycle lock, after the install entry replayed any journal a killed
    install left (#4863).
    """
    from apm_cli.core.scope import get_apm_home
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.integration import write_seam

    store_parent = get_apm_home() / "install-snapshots"
    journal = DeployJournal(store_parent)
    # *contain* (the project) bounds the rollback only; nothing is persisted for replay.
    journal.contain = Path(os.path.abspath(contain))

    def seam(
        path: Path, contents: bool, expected: str | None = None, size: int | None = None
    ) -> None:
        journal.record(path, contents=contents, expected=expected, size=size)

    write_seam.listen(seam, journal.after_write, journal=journal.token)
    try:
        yield journal
    except BaseException as exc:
        write_seam.stop_listening(seam)
        if issues := journal.restore():
            raise ApplyRestoreIncomplete(issues) from exc
        raise
    write_seam.stop_listening(seam)
    journal.discard()


def journaled_local_bundle(install: Callable[..., Any]) -> Callable[..., Any]:
    """Journal a local-bundle install's deploy writes like a package install's (#5046).

    The journal follows the bundle's lockfile: it names the staged lockfile
    just before the commit and settles once it is on disk, or settles when
    the command ends without one, so in process nothing is restored. Only a
    process killed mid-deploy leaves it for the next install, which replays
    it under the #4863 rules. Wrap the install inside its lockfile writer,
    under the lifecycle lock; a dry run writes nothing and is not journaled.
    """

    @wraps(install)
    def journaled(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("dry_run"):
            return install(*args, **kwargs)
        with _local_bundle_journal(user_scope=bool(kwargs.get("global_"))):
            return install(*args, **kwargs)

    return journaled


@contextlib.contextmanager
def _local_bundle_journal(*, user_scope: bool) -> Iterator[None]:
    from apm_cli.core.scope import InstallScope, get_apm_home, get_modules_dir
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.deps.lockfile_write_authority import (
        after_lockfile_commit,
        before_lockfile_commit,
        staged_lockfile_text,
        unless_lockfile_commits,
    )
    from apm_cli.install.deploy_snapshot import DeployJournal, lockfile_identity
    from apm_cli.integration import write_seam

    apm_modules_dir = get_modules_dir(InstallScope.USER if user_scope else InstallScope.PROJECT)
    workspace = Path(os.path.abspath(apm_modules_dir)).parent
    journal = DeployJournal(
        get_apm_home() / "install-snapshots",
        root=_deploy_root_of(apm_modules_dir),
        workspace=workspace,
    )

    def seam(
        path: Path, contents: bool, expected: str | None = None, size: int | None = None
    ) -> None:
        journal.record(path, contents=contents, expected=expected, size=size)

    def mark() -> None:
        text = staged_lockfile_text(get_lockfile_path(workspace))
        if text is not None:
            journal.mark_commit(lockfile_identity(text))

    def settle() -> None:
        committed = _committed_lockfile(workspace)
        if journal.store is not None and committed is not None:
            journal.mark_commit(committed)
        journal.discard()

    write_seam.listen(seam, journal.after_write)
    try:
        yield
    finally:
        write_seam.stop_listening(seam)
        before_lockfile_commit(mark)
        unless_lockfile_commits(journal.discard)
        after_lockfile_commit(settle, first=True)


def _restore_manifest_from_snapshot(manifest_path: Path, snapshot: bytes) -> None:
    """Atomically replace *manifest_path* with byte-exact *snapshot*."""
    fd, temporary_name = tempfile.mkstemp(
        prefix="apm-restore-",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary_name)
        raise
    try:
        os.replace(temporary_name, manifest_path)
    except OSError as exc:
        raise OSError(
            f"Could not restore {manifest_path}: {exc}. "
            f"Prior manifest snapshot retained at {temporary_name} for recovery."
        ) from exc


def _maybe_rollback_manifest(
    manifest_path: Path,
    snapshot: bytes | None,
    logger: InstallLogger,
    manifest_existed: bool | None = None,
) -> None:
    """Best-effort restore or removal of the attempt's manifest."""
    if manifest_existed is None:
        manifest_existed = snapshot is not None or manifest_path.exists()
    if not manifest_existed:
        if not manifest_path.exists():
            return
        try:
            manifest_path.unlink()
            if logger is not None:
                logger.progress("Removed apm.yml created by the failed install.")
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    f"Failed to remove {manifest_path} created by this install: {exc}. "
                    "Inspect and remove that new manifest before retrying."
                )
        return
    try:
        if snapshot is None:
            return
        if manifest_path.exists() and manifest_path.read_bytes() == snapshot:
            return
        _restore_manifest_from_snapshot(manifest_path, snapshot)
        if logger is not None:
            logger.progress("apm.yml restored to its previous state.")
    except Exception as exc:
        if logger is not None:
            logger.warning(
                f"Failed to restore apm.yml: {exc}. Inspect the recovery data before retrying."
            )


def _deploy_root_of(apm_modules_dir: Path) -> Path:
    """The deploy root of the workspace whose ``apm_modules`` is *apm_modules_dir*."""
    from apm_cli.core.scope import get_workspace_deploy_root

    return get_workspace_deploy_root(Path(os.path.abspath(apm_modules_dir)).parent)


def _receipts_directory(root: Path) -> Path:
    """The deployment receipts directory: the parent of every receipt file."""
    from apm_cli.core.deployment_ledger import materialization_evidence_paths
    from apm_cli.core.scope import InstallScope

    return materialization_evidence_paths(root, InstallScope.PROJECT)[0].parent


def _committed_lockfile(apm_dir: Path) -> str | None:
    """The identity of the lockfile on disk in *apm_dir*, or ``None``."""
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.deploy_snapshot import lockfile_identity

    try:
        return lockfile_identity(get_lockfile_path(apm_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _ledger_hashes(apm_dir: Path) -> dict[str, str | None]:
    """Each deployed path the lockfile in *apm_dir* claims, mapped to its recorded hash."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.install.deploy_snapshot import LedgerUnavailable

    path = get_lockfile_path(apm_dir)
    try:
        lockfile = LockFile.read(path)
    except Exception as exc:
        # Unreadable evidence is not "nothing claimed": replay keeps the journal.
        raise LedgerUnavailable(path, f"the lockfile cannot be read: {exc}") from None
    if lockfile is None:
        return {}
    ledger: dict[str, str | None] = dict.fromkeys(
        DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)
    )
    ledger.update(DeploymentLedgerCodec.deployed_file_hashes(lockfile))
    return ledger


def replay_abandoned_deploys(apm_modules_dir: Path, logger: Any) -> None:
    """Replay the deploy journal a killed install left for this workspace (#4863).

    The one recovery owner: every install entry that mutates deployed state
    calls it under the lifecycle lock, before it changes anything. A journal
    that cannot be proven against the deployment ledger, or a path that
    cannot be restored, keeps the journal, names each path and raises
    ``InstallFailureAlreadyRendered``.
    """
    from apm_cli.core.scope import get_apm_home
    from apm_cli.install.deploy_snapshot import JournalReplayError, replay_abandoned_journals
    from apm_cli.install.errors import InstallFailureAlreadyRendered

    apm_dir = Path(os.path.abspath(apm_modules_dir)).parent
    store_parent = get_apm_home() / "install-snapshots"
    if not any(store_parent.glob("deploy-*")):
        return
    try:
        root = _deploy_root_of(apm_modules_dir)
        outcome = replay_abandoned_journals(
            store_parent,
            root,
            lambda: _ledger_hashes(apm_dir),
            receipts=_receipts_directory(root),
            committed=lambda: _committed_lockfile(apm_dir),
            workspace=apm_dir,
        )
    except JournalReplayError as exc:
        _report_kept_journal(exc, logger)
        raise InstallFailureAlreadyRendered(str(exc)) from None
    if logger is None:
        return
    from apm_cli.core.remediation import kept_note

    copies = ", ".join(str(store) for store in outcome.retained)
    for path, reason in outcome.kept:
        note = kept_note(
            path,
            f"{reason}, so it was not restored",
            f"Review it; the pre-install copies are kept at {copies}.",
        )
        logger.warning(note)
    if outcome.restored:
        logger.progress(
            f"Restored {outcome.restored} deployed path(s) left by an interrupted install."
        )


def replay_before_install_entry(scope: Any, *, dry_run: bool, logger: Any) -> None:
    """Replay before any ``apm install`` entry writes; a dry run changes nothing.

    The local-bundle and ``--mcp`` entries never open an install transaction.
    """
    if not dry_run:
        from apm_cli.core.scope import get_modules_dir

        replay_abandoned_deploys(get_modules_dir(scope), logger)


def _report_kept_journal(exc: Any, logger: Any) -> None:
    """Name each path of a journal replay kept, and where its copies are."""
    if logger is None:
        return
    if exc.home is not None:
        _report_group_writable_home(exc, logger)
        return
    if exc.applied:
        logger.error("An interrupted install left deployed files that could not all be restored:")
    else:
        logger.error(
            "An interrupted install left a deploy journal that cannot be replayed "
            "safely; nothing was restored:"
        )
    for path, reason in exc.issues:
        logger.error(f"  {path}: {reason}")
    logger.error(
        f"The journal is kept at {exc.store}. Restore or remove the listed paths "
        "by hand, then run the command again."
    )


def _report_group_writable_home(exc: Any, logger: Any) -> None:
    """Name APM_HOME, its fix and the paths an interrupted install left (#5385)."""
    logger.warning(
        f"An interrupted install left deployed files, but APM_HOME ({exc.home}) is group "
        "writable and its group cannot be shown private to you, so they were not restored:"
    )
    for path, _reason in exc.issues:
        logger.warning(f"  {path}")
    import shlex

    logger.warning(
        f"Run 'chmod g-w {shlex.quote(str(exc.home))}', then run the command again; "
        f"the journal is kept at {exc.store} and is replayed then."
    )


class InstallTransaction:
    """Own install completion meaning and rollback-scoped filesystem state.

    The resolution journal is limited to paths prepared below ``apm_modules``.
    From :meth:`journal_deploy_writes` on, the transaction listens at the
    integrator write seam (``integration/write_seam.py``) and records the
    prior state of each path the first time it is written (#4282). A rollback
    restores the recorded paths and discards the staged lockfile, so disk and
    ``apm.lock.yaml`` keep their pre-command state. After
    :meth:`release_deploy_targets` (every gate that scans deployed bytes
    passed) the journal is discarded as soon as the staged lockfile reaches
    disk: from then on the lockfile records what was written. On every
    rollback, target roots the attempt created
    (:meth:`track_created_target_roots`) are removed when left empty.
    """

    def __init__(
        self,
        *,
        manifest_path: Path,
        apm_modules_dir: Path,
        validation: _ValidationOutcome | None,
        logger: InstallLogger,
        acquire_lock: bool = True,
        recover_deploys: bool = True,
        accept_ref_changes: bool = False,
    ) -> None:
        """Capture the manifest and create one resolution staging session.

        *recover_deploys* is ``False`` for a lock-only run (``apm lock``) or a
        dry run, which never change deployed files, not even to replay a journal.
        *accept_ref_changes* (``--accept-ref-change``) lets this attempt accept
        marketplace plugin refs that contradict their recorded pins.
        """
        from apm_cli.cache.deploy_store import process_umask

        # Read the umask now, before the pipeline starts any worker thread.
        process_umask()
        self.manifest_path = manifest_path
        self.apm_modules_dir = apm_modules_dir
        self._validation = validation
        self._logger = logger
        self._workspace_lock = acquire_lifecycle_lock() if acquire_lock else None
        # Finalize this acquisition, not the shared FileLock object's lifetime.
        self._workspace_lock_finalizer = (
            weakref.finalize(self, self._workspace_lock.release)
            if self._workspace_lock is not None
            else None
        )
        try:
            self._manifest_existed = manifest_path.exists()
            self._manifest_snapshot = manifest_path.read_bytes() if self._manifest_existed else None
            if self._workspace_lock is not None and recover_deploys:
                # Under the lifecycle lock, before this attempt changes anything.
                self._replay_abandoned_deploys()
            self._resolution = ResolutionStagingSession(apm_modules_dir)
            # Marketplace ref pins resolved by this attempt; written on commit only.
            self._ref_pins = open_ref_pin_stage(accept_changes=accept_ref_changes)
            # Commit reachability verdicts of this attempt; written on commit only.
            self._verdicts = open_verdict_stage()
        except BaseException:
            self._release_workspace_lock()
            raise
        self._lock = threading.RLock()
        self._deploy_journal: DeployJournal | None = None
        self._journal_released = False
        self._journal_held_until_commit = False
        self._created_target_roots: list[Path] = []
        self._migrated_lockfile: tuple[Path, Path, bytes] | None = None
        self.committed = False
        self._completed = False
        self._rolled_back = False
        self._deploy_evidence: StagedEvidence = ([], [])

    @property
    def resolution(self) -> ResolutionStagingSession:
        """Return the single resolution journal owned by this attempt."""
        return self._resolution

    def record_validation(self, validation: _ValidationOutcome) -> None:
        """Attach the validation outcome produced after transaction creation."""
        self._validation = validation

    def validation_result(self) -> InstallResult | None:
        """Return the terminal result for an all-invalid positional batch."""
        if self._validation is None or not self._validation.all_failed:
            return None
        self.rollback()
        return InstallResult(
            disposition=InstallDisposition.VALIDATION_FAILED,
            exit_code=1,
        )

    def commit(self, result: InstallResult) -> InstallResult:
        """Finalize staged resolution paths and mark *result* committed."""
        with self._lock:
            if self._rolled_back:
                raise RuntimeError("Cannot commit an install transaction after rollback")
            try:
                if not self.committed:
                    if not self._settle_at_lockfile_commit():
                        self._settle_deploy_journal()
                    cleanup_issues = self._resolution.commit() or []
                    self.committed = True
                    self._ref_pins.commit()
                    self._verdicts.commit()
                    self._completed = True
                    cleanup_issues.extend(self._resolution.remove_abandoned_roots())
                    self._report_resolution_cleanup_issues(cleanup_issues)
                    self._publish_deploy_evidence()
                if (
                    self._validation is not None
                    and self._validation.has_failures
                    and result.disposition is InstallDisposition.SUCCESS
                ):
                    result.disposition = InstallDisposition.PARTIAL_SUCCESS
                result.committed = True
                return result
            except BaseException:
                self.rollback()
                raise
            finally:
                if self.committed:
                    self._release_workspace_lock()

    def hold_deploy_evidence(self) -> None:
        """Take what the pipeline staged for the deploy store into this attempt.

        Called when the pipeline returns, before any post-install script runs:
        a script resets the invocation memos the evidence was staged in.
        """
        from apm_cli.cache.deploy_store import take_staged

        files, records = take_staged()
        with self._lock:
            self._deploy_evidence[0].extend(files)
            self._deploy_evidence[1].extend(records)

    def _publish_deploy_evidence(self) -> None:
        """Store the deployed bytes and package records this committed attempt staged.

        The store is a best-effort cache: failing to fill it never fails an install.
        """
        from apm_cli.cache.deploy_store import commit_staged

        taken, self._deploy_evidence = self._deploy_evidence, ([], [])
        with contextlib.suppress(Exception):
            commit_staged(taken)

    def _discard_deploy_evidence(self) -> None:
        """Forget what an attempt that did not commit staged for the deploy store."""
        from apm_cli.cache.deploy_store import discard_staged

        self._deploy_evidence = ([], [])
        discard_staged()

    def _report_resolution_cleanup_issues(self, issues: list[tuple[Path, str]]) -> None:
        """Report cleanup paths that require safe manual recovery."""
        if not issues or self._logger is None:
            return
        self._logger.warning(
            "Install recovery or staging cleanup is incomplete. Close programs using the "
            "affected paths, then inspect the retained data before retrying. "
            "Do not delete backups needed to recover the prior installation."
        )
        for path, reason in issues:
            self._logger.warning(f"Resolution recovery path {path}: {reason}")

    def complete(self, result: InstallResult) -> InstallResult:
        """Finalize one result according to its canonical disposition."""
        if result.disposition is InstallDisposition.DRY_RUN:
            with self._lock:
                if self._rolled_back:
                    # A preview whose staging already rolled back changed nothing.
                    return result
                try:
                    self._discard_deploy_evidence()
                    self._settle_deploy_journal()
                    self._ref_pins.discard()
                    self._verdicts.discard()
                    cleanup_issues = self._resolution.rollback() or []
                    self._report_resolution_cleanup_issues(cleanup_issues)
                    self._completed = True
                finally:
                    self._release_workspace_lock()
            return result
        if result.disposition in {
            InstallDisposition.CANCELLED,
            InstallDisposition.FAILED,
            InstallDisposition.VALIDATION_FAILED,
        }:
            self.rollback()
            return result
        return self.commit(result)

    def _replay_abandoned_deploys(self) -> None:
        """Replay the deploy journal a killed install left for this deploy root (#4863)."""
        replay_abandoned_deploys(self.apm_modules_dir, self._logger)

    def journal_deploy_writes(self, ctx: Any = None) -> None:
        """Record, at the integrator write seam, the prior state of every path written from now on.

        ``--frozen`` withholds the lockfile, so its no-rewrite check runs after
        the pipeline: the journal is then kept until this install commits and
        a refusal there rolls back every journaled path (#4638).
        """
        from apm_cli.cache.deploy_store import discard_staged
        from apm_cli.core.scope import get_apm_home
        from apm_cli.install.deploy_snapshot import DeployJournal
        from apm_cli.integration import write_seam

        with self._lock:
            if self._deploy_journal is not None or self.committed or self._rolled_back:
                return
            # Deploy-store evidence counts only from this attempt's integrate phase on.
            discard_staged()
            if ctx is not None:
                from apm_cli.install.service import InstallService

                frozen = InstallService.frozen_disposition(ctx).withhold_lockfile_writes
                self._journal_held_until_commit = frozen
            store_parent = get_apm_home() / "install-snapshots"
            # The pipeline's write root; replay matches it to the workspace root.
            root = getattr(ctx, "project_root", None) or _deploy_root_of(self.apm_modules_dir)
            self._deploy_journal = DeployJournal(
                store_parent, root=root, workspace=self.apm_modules_dir.parent
            )
            token = self._deploy_journal.token
            write_seam.listen(self._record_deploy_write, self._seal_deploy_write, journal=token)

    def _mark_deploy_commit(self) -> None:
        """Seal the journal and name the staged lockfile it is about to commit."""
        from apm_cli.deps.lockfile import get_lockfile_path
        from apm_cli.deps.lockfile_write_authority import staged_lockfile_text
        from apm_cli.install.deploy_snapshot import lockfile_identity

        journal = self._deploy_journal
        text = staged_lockfile_text(get_lockfile_path(self.apm_modules_dir.parent))
        if journal is not None and text is not None:
            journal.mark_commit(lockfile_identity(text))

    def _record_deploy_write(
        self, path: Path, contents: bool, expected: str | None = None, size: int | None = None
    ) -> None:
        """Keep *path*'s prior state the first time this attempt touches it."""
        journal = self._deploy_journal
        if journal is not None:
            journal.record(path, contents=contents, expected=expected, size=size)

    def _seal_deploy_write(self, path: Path) -> None:
        """Seal what this attempt just wrote at *path* (#4863)."""
        journal = self._deploy_journal
        if journal is not None:
            journal.after_write(path)

    def undo_lockfile_migration_on_rollback(self, apm_dir: Path) -> None:
        """A rollback moves the ``apm.lock.yaml`` just migrated back to ``apm.lock`` (#4826)."""
        from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, get_lockfile_path

        current = get_lockfile_path(apm_dir)
        with contextlib.suppress(OSError):
            legacy = Path(apm_dir) / LEGACY_LOCKFILE_NAME
            self._migrated_lockfile = (current, legacy, current.read_bytes())

    def _undo_lockfile_migration(self) -> None:
        """Put the legacy lockfile back when this attempt left its bytes unchanged."""
        migrated, self._migrated_lockfile = self._migrated_lockfile, None
        if migrated is None:
            return
        current, legacy, snapshot = migrated
        with contextlib.suppress(OSError):
            if not legacy.exists() and current.read_bytes() == snapshot:
                current.rename(legacy)

    def track_created_target_roots(self, roots: list[Path]) -> None:
        """Follow the target roots this attempt creates; rollback removes empty ones."""
        self._created_target_roots = roots

    def _remove_created_target_roots(self) -> None:
        """Remove target roots this attempt created that a failure left empty (#4159)."""
        from apm_cli.install.deploy_snapshot import remove_empty_directories

        remove_empty_directories(self._created_target_roots)

    @property
    def restores_deploy_targets(self) -> bool:
        """Whether rollback will restore the paths the deploy journal recorded."""
        journal = self._deploy_journal
        return journal is not None and bool(journal.units)

    def release_deploy_targets(self) -> None:
        """Every gate that scans deployed bytes passed: keep the journal until the lock commits."""
        from apm_cli.deps.lockfile_write_authority import (
            after_lockfile_commit,
            before_lockfile_commit,
        )

        with self._lock:
            if self._deploy_journal is None or self._journal_released:
                return
            self._journal_released = True
            if self._journal_held_until_commit:
                return
        # Mark the journal with the lockfile about to commit, so a kill after
        # the write settles it instead of rolling it back (#4863).
        before_lockfile_commit(self._mark_deploy_commit)
        # Settle at the durable commit, before receipt binding or any other
        # fallible post-commit action: the lockfile then records the deploys.
        after_lockfile_commit(self._settle_deploy_journal, first=True)

    def _settle_at_lockfile_commit(self) -> bool:
        """Keep a released journal until the staged lockfile reaches disk (#4664).

        A command that runs the pipeline inside its own lockfile writer (``apm
        update``, ``apm deps update``) completes this transaction before the
        writer commits. The journal then still restores every deployed path if
        that commit fails or the command is abandoned; a successful commit
        settles it (:meth:`release_deploy_targets`).
        """
        from apm_cli.deps.lockfile_write_authority import unless_lockfile_commits

        if not self._journal_released or self._deploy_journal is None:
            return False
        return unless_lockfile_commits(self._undo_deploys)

    def _undo_deploys(self) -> None:
        """Restore the journaled paths and drop target roots this attempt created.

        Rollback and an uncommitted completed transaction (#4664) both undo here.
        """
        self._restore_deploy_targets()
        self._remove_created_target_roots()

    def _take_deploy_journal(self) -> DeployJournal | None:
        """Stop listening at the write seam and hand over the journal."""
        from apm_cli.integration import write_seam

        with self._lock:
            journal, self._deploy_journal = self._deploy_journal, None
        write_seam.stop_listening(self._record_deploy_write)
        return journal

    def _settle_deploy_journal(self) -> None:
        """The lockfile records what was written: drop the pre-write copies.

        The journal first names the lockfile on disk, so a kill before the
        copies are gone settles it too (#4863).
        """
        journal = self._take_deploy_journal()
        if journal is None:
            return
        if journal.store is not None:
            committed = _committed_lockfile(self.apm_modules_dir.parent)
            if committed is not None:
                journal.mark_commit(committed)
        journal.discard()

    def _restore_deploy_targets(self) -> None:
        """Put every journaled path back and discard the lockfile this attempt staged."""
        from apm_cli.deps.lockfile_write_authority import discard_staged_lockfile_writes

        journal = self._take_deploy_journal()
        if journal is None:
            return
        # Even with nothing deployed (a lockfile-only attempt) the staged
        # lockfile of a rolled-back attempt must not be published.
        discard_staged_lockfile_writes()
        if not journal.units:
            journal.discard()
            return
        issues = journal.restore()
        if self._logger is None:
            return
        if not issues:
            self._logger.progress("Deployed files restored to their previous state.")
            return
        self._logger.warning(
            "Some deployed files could not be restored. Close programs using them, "
            "then copy them back from the retained pre-install copies before retrying."
        )
        for path, reason in issues:
            self._logger.warning(f"Deploy restore path {path}: {reason}")

    def rollback(self) -> None:
        """Restore the manifest, journaled deploy paths and resolution paths."""
        with self._lock:
            if self.committed or self._rolled_back:
                return
            try:
                self._discard_deploy_evidence()
                self._ref_pins.discard()
                try:
                    self._verdicts.discard()
                    self._undo_deploys()
                finally:
                    try:
                        cleanup_issues = self._resolution.rollback() or []
                        self._report_resolution_cleanup_issues(cleanup_issues)
                    finally:
                        self._restore_manifest()
                        self._undo_lockfile_migration()
                self._rolled_back = True
                self._completed = True
            finally:
                self._release_workspace_lock()

    def fail(self, error: BaseException) -> InstallResult:
        """Rollback and return a structured failed install result."""
        self.rollback()
        return InstallResult(
            disposition=InstallDisposition.FAILED,
            exit_code=1,
            error=error,
        )

    def __enter__(self) -> InstallTransaction:
        """Enter this install attempt."""
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        """Rollback every uncommitted exit and preserve exception semantics."""
        if exc is not None or not self._completed:
            self.rollback()
        return False

    def _restore_manifest(self) -> None:
        """Atomically restore the byte-exact manifest snapshot when present."""
        _maybe_rollback_manifest(
            self.manifest_path,
            self._manifest_snapshot,
            self._logger,
            self._manifest_existed,
        )

    def _release_workspace_lock(self) -> None:
        """Release this transaction's nested lifecycle acquisition once."""
        if self._workspace_lock_finalizer is not None:
            self._workspace_lock_finalizer()


def resolution_for_context(ctx: Any) -> ResolutionStagingSession:
    """Return the context transaction's journal, creating a legacy adapter."""
    if ctx.transaction is None:
        ctx.transaction = InstallTransaction(
            manifest_path=manifest_file(ctx.source_root),
            apm_modules_dir=ctx.apm_modules_dir,
            validation=None,
            logger=ctx.logger,
            acquire_lock=False,
        )
    return cast(ResolutionStagingSession, ctx.transaction.resolution)


def transactional_pipeline(
    run: Callable[..., InstallResult | None],
) -> Callable[..., InstallResult]:
    """Supply a compatibility transaction and rollback every exceptional exit."""

    @wraps(run)
    def wrapped(*args: Any, **kwargs: Any) -> InstallResult:
        transaction = kwargs.get("transaction")
        owns_transaction = transaction is None
        if transaction is None:
            from apm_cli.core.scope import InstallScope, get_manifest_path, get_modules_dir

            scope = kwargs.get("scope") or InstallScope.PROJECT
            try:
                manifest_path = get_manifest_path(scope)
                modules_dir = get_modules_dir(scope)
            except (AttributeError, TypeError):
                manifest_path = manifest_file(Path.cwd())
                modules_dir = Path.cwd() / "apm_modules"
            transaction = InstallTransaction(
                manifest_path=manifest_path,
                apm_modules_dir=modules_dir,
                validation=None,
                logger=kwargs.get("logger"),
                # apm lock and a dry run never touch deployed files (#4863).
                recover_deploys=not (kwargs.get("lockfile_only") or kwargs.get("dry_run")),
            )
            kwargs["transaction"] = transaction
        try:
            result = run(*args, **kwargs)
        except BaseException:
            transaction.rollback()
            raise
        if result is None:
            result = InstallResult()
        if owns_transaction:
            result = transaction.complete(result)
        return result

    return wrapped


def stage_existing_copy(ctx: Any, path: Path, locked: Any, *, skip: bool = False) -> None:
    """Preserve an existing ``apm_modules`` copy before a re-fetch replaces it.

    The install transaction restores it byte for byte when the attempt is
    refused (for example on a content hash mismatch) and discards it once the
    attempt completes, so a refused install leaves ``apm_modules`` as it was.
    Only a copy whose *locked* row records a ``content_hash`` is staged: that is
    the row a content hash refusal can reject. *skip* marks a copy this attempt
    already fetched (it is staged already).
    """
    if skip or not getattr(locked, "content_hash", None):
        return
    if path.exists() or path.is_symlink():
        resolution_for_context(ctx).prepare_path(path)
