"""Canonical completion and rollback owner for one install attempt."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import weakref
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from apm_cli.core.scope import manifest_file
from apm_cli.install.locking import acquire_lifecycle_lock
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.marketplace.version_pins import open_ref_pin_stage
from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger, _ValidationOutcome
    from apm_cli.install.deploy_snapshot import DeployJournal


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
    ) -> None:
        """Capture the manifest and create one resolution staging session."""
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
            self._resolution = ResolutionStagingSession(apm_modules_dir)
            # Marketplace ref pins resolved by this attempt; written on commit only.
            self._ref_pins = open_ref_pin_stage()
        except BaseException:
            self._release_workspace_lock()
            raise
        self._lock = threading.RLock()
        self._deploy_journal: DeployJournal | None = None
        self._journal_released = False
        self._journal_held_until_commit = False
        self._created_target_roots: list[Path] = []
        self.committed = False
        self._completed = False
        self._rolled_back = False

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
                    self._completed = True
                    cleanup_issues.extend(self._resolution.remove_abandoned_roots())
                    self._report_resolution_cleanup_issues(cleanup_issues)
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
                    self._settle_deploy_journal()
                    self._ref_pins.discard()
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

    def journal_deploy_writes(self, ctx: Any = None) -> None:
        """Record, at the integrator write seam, the prior state of every path written from now on.

        ``--frozen`` withholds the lockfile, so its no-rewrite check runs after
        the pipeline: the journal is then kept until this install commits and
        a refusal there rolls back every journaled path (#4638).
        """
        from apm_cli.core.scope import get_apm_home
        from apm_cli.install.deploy_snapshot import DeployJournal, reap_abandoned_journals
        from apm_cli.integration import write_seam

        with self._lock:
            if self._deploy_journal is not None or self.committed or self._rolled_back:
                return
            if ctx is not None:
                from apm_cli.install.service import InstallService

                frozen = InstallService.frozen_disposition(ctx).withhold_lockfile_writes
                self._journal_held_until_commit = frozen
            store_parent = get_apm_home() / "install-snapshots"
            if self._workspace_lock is not None:
                # Under the lifecycle lock no other install owns a journal here.
                reap_abandoned_journals(store_parent)
            self._deploy_journal = DeployJournal(store_parent)
            write_seam.listen(self._record_deploy_write)

    def _record_deploy_write(self, path: Path, contents: bool) -> None:
        """Keep *path*'s prior state the first time this attempt touches it."""
        journal = self._deploy_journal
        if journal is not None:
            journal.record(path, contents=contents)

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
        from apm_cli.deps.lockfile_write_authority import after_lockfile_commit

        with self._lock:
            if self._deploy_journal is None or self._journal_released:
                return
            self._journal_released = True
            if self._journal_held_until_commit:
                return
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
        """The lockfile records what was written: drop the pre-write copies."""
        journal = self._take_deploy_journal()
        if journal is not None:
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
                self._ref_pins.discard()
                try:
                    self._undo_deploys()
                finally:
                    try:
                        cleanup_issues = self._resolution.rollback() or []
                        self._report_resolution_cleanup_issues(cleanup_issues)
                    finally:
                        self._restore_manifest()
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
