"""Base integrator with shared collision detection and sync logic."""

import errno
import os
import posixpath
import re
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from apm_cli.compilation.link_resolver import UnifiedLinkResolver
from apm_cli.core.deployment_state import MaterializationResult
from apm_cli.core.remediation import LOCAL_FILE_UNMANAGED
from apm_cli.integration import write_seam
from apm_cli.integration.write_seam import write_text_lf
from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.console import _rich_warning
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)
from apm_cli.utils.paths import is_within, relative_to, stored_alias
from apm_cli.utils.realpath import resolve_path

if TYPE_CHECKING:
    from apm_cli.core.deployment_ledger import OverwriteGate
    from apm_cli.integration.cleanup import DeploymentEvidence


def discover_primitives(*args: Any, **kwargs: Any) -> Any:
    from apm_cli.primitives.discovery import discover_primitives as _discover_primitives

    return _discover_primitives(*args, **kwargs)


def _target_root_dirs(base: Path) -> frozenset[Path]:
    """Every project-layout target root directory under *base*.

    A project's target roots signal which harnesses it uses (opt-in targets
    deploy only where their root exists), so project cleanup never removes
    them. Cleanup bounded by the home directory is user scope, where roots
    APM created itself (such as ``~/.claude``) go when their last file is
    retired.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    return frozenset((base / profile.root_dir).resolve() for profile in KNOWN_TARGETS.values())


def _managed_absolute_target_root(
    candidate: Path,
    targets: Any,
    *,
    allow_final_symlink: bool = False,
) -> Path | None:
    """Return the managed root for *candidate*, or ``None`` if unmanaged."""
    from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    source = targets
    if source is None:
        source = []
        for profile in KNOWN_TARGETS.values():
            if not profile.user_supported or profile.user_root_resolver is not None:
                continue
            scoped = scoped_profile_or_none(profile, user_scope=True)
            if scoped is not None:
                source.append(scoped)
    try:
        resolved = (candidate.parent if allow_final_symlink else candidate).resolve()
        for target_profile in source:
            if target_profile is None:
                continue
            deploy_root = target_profile.managed_deploy_root
            if deploy_root is None:
                continue
            resolved_root = deploy_root.resolve()
            if has_symlink_component(deploy_root, candidate.parent):
                continue
            for mapping in target_profile.primitives.values():
                if not mapping.subdir:
                    continue
                primitive_path = deploy_root / mapping.subdir
                primitive_root = primitive_path.resolve()
                try:
                    contained = ensure_path_within(resolved, primitive_root)
                except PathTraversalError:
                    continue
                if allow_final_symlink or contained != primitive_root:
                    return resolved_root
            if target_profile.hooks_config_display:
                hooks_file = resolved_root / Path(target_profile.hooks_config_display).name
                if resolved == hooks_file.resolve():
                    return resolved_root
    except (ValueError, OSError):
        return None
    return None


class _SymlinkRaceError(OSError):
    """Raised by ``_read_bytes_no_follow`` when the path becomes a symlink
    between the pre-check and the open(). Caught locally; never bubbles."""


@dataclass
class IntegrationResult:
    """Result of any file-level integration operation.

    The core fields (files_integrated, files_skipped, target_paths,
    links_resolved) are used by all integrators.  Hook- and skill-specific
    fields default to zero/False and are ignored by integrators that do
    not produce them.
    """

    files_integrated: int
    files_updated: int  # Kept for CLI compat, always 0 today
    files_skipped: int
    target_paths: list[Path]
    links_resolved: int = 0
    materializations: tuple[MaterializationResult, ...] = ()

    # Hook-specific (default 0 when not applicable)
    scripts_copied: int = 0

    # Skill-specific (default 0/False when not applicable)
    sub_skills_promoted: int = 0
    skill_created: bool = False

    # Number of pre-existing on-disk files that were silently *adopted*
    # (byte-identical to source). Counted separately from
    # ``files_integrated`` so the install summary can surface the work
    # done in adopt-only runs instead of looking like a no-op.
    files_adopted: int = 0

    # Hook files whose entries are registered after this call, written or
    # unchanged; adoption display (RA-007) never changes it (#4033).
    hooks_registered: int = 0

    # Hook transparency: per-file display metadata populated by HookIntegrator.
    # Each entry is a dict with keys: target_label, output_path, source_hook_file,
    # actions (list of {event, summary}), rendered_json.
    # Faithfully reflects post-path-rewrite data actually written to disk.
    display_payloads: list = field(default_factory=list)


def _read_bytes_no_follow(path: Path) -> bytes:
    """Read *path* with ``O_NOFOLLOW`` semantics where supported.

    On POSIX, opens the file with ``os.O_NOFOLLOW`` so the kernel
    rejects the open atomically if the final path component is a
    symlink. This closes the TOCTOU race between
    ``Path.is_symlink()`` and ``Path.read_bytes()`` exploited by a
    co-tenant who can swap files for symlinks.

    On Windows (no ``O_NOFOLLOW``), falls back to a plain read; the
    caller's upfront ``is_symlink()`` check plus ``ensure_path_within``
    at the integrator call sites provide the containment guarantee.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        # ELOOP is the canonical errno for "O_NOFOLLOW refused to open
        # a symlink"; some Linux kernels return EMLINK or ELOOP-equivalent.
        if nofollow and exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise _SymlinkRaceError(exc.errno, f"Refused to follow symlink at {path}") from exc
        raise
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def recovery_directory(entry: str, destination: Path, *, user_scope: bool) -> str:
    """Name a deployed skill directory so a user can move it: project-relative or absolute."""
    if user_scope or "://" in entry or Path(entry).is_absolute():
        return destination.resolve().as_posix()
    return entry


def untrusted_baseline_message(
    entry: str, directory: str | None = None, *, user_scope: bool = False
) -> str:
    """Refuse to replace an existing file that has no trustworthy deployment baseline.

    *directory* names the deployed skill directory whose removal lets the next
    install, in the same scope, redeploy it from source.
    """
    head = f"Skipped {entry}: no trustworthy local deployment baseline. "
    if directory is None:
        return (
            f"{head}Move the conflicting file aside and retry; "
            "use --force only to intentionally overwrite it."
        )
    # Scoped recovery: never a global --force that could overwrite edits elsewhere.
    skill = f"{directory.rstrip('/')}/"
    return (
        f"{head}Move the conflicting file aside and retry. To redeploy only this skill, "
        f"move {skill} aside (or delete it if it holds no edits), then run "
        f"'{'apm install -g' if user_scope else 'apm install'}'; that rewrites {skill} "
        "alone and keeps edited files elsewhere."
    )


def kept_edit_message(entry: str, *, user_scope: bool, proven_edit: bool) -> str:
    """Name a managed file install kept, and the runnable way past it.

    *proven_edit* means the file differs from the bytes APM recorded deploying;
    otherwise no receipt or locked render proves APM wrote it (#4360).
    """
    from apm_cli.core import remediation

    # Restore, or keep the edit by exporting it; never a --force overwrite,
    # which would also deploy despite critical security findings (RA-008).
    if user_scope:
        # export-patch reads the project lock only; user scope can only restore.
        restore = remediation.KEPT_FILE_RESTORE.with_command("apm install -g")
        return remediation.kept_file_message(entry, _kept_reason(proven_edit), restore)
    return remediation.kept_file_message(
        entry,
        _kept_reason(proven_edit),
        remediation.KEPT_FILE_RESTORE,
        remediation.LOCAL_EDITS_EXPORT,
    )


def edit_kind(recorded: object, rendered: object) -> str:
    """Classify a proven user edit of an output APM recorded deploying as *recorded*.

    ``"drift"`` when the package still renders *recorded* (nothing new to
    deploy: kept, reported, exit 0); ``"blocked"`` when it renders something
    new that the kept edit holds back (exit 1). Files compare hashes, LSP
    server entries compare rendered entries (#4398, RA-012, #4626).
    """
    return "drift" if rendered == recorded else "blocked"


def kept_entry_message(subject: str, *, user_scope: bool, proven_edit: bool) -> str:
    """Name a managed config entry install kept (an LSP server, #4626).

    Same reasons as :func:`kept_edit_message`; the restore removes the entry,
    since an entry inside a shared file cannot be moved aside.
    """
    from apm_cli.core import remediation

    restore = remediation.KEPT_ENTRY_RESTORE
    if user_scope:
        restore = restore.with_command("apm install -g")
    return remediation.kept_file_message(subject, _kept_reason(proven_edit), restore)


def _kept_reason(proven_edit: bool) -> str:
    if proven_edit:
        return "it was modified since APM deployed it"
    return (
        "it differs from what APM renders and there is no local deployment "
        "receipt or locked render proving APM wrote it, so it may hold local edits"
    )


class BaseIntegrator:
    """Shared infrastructure for file-level integrators.

    Subclasses only need to override the abstract hooks; the collision
    detection, sync removal, and link resolution logic is
    handled here.
    """

    # Overwrite evidence for one install call, bound by ``recorded_writes`` (#4360).
    _overwrite_gate: "OverwriteGate | None" = None
    # While install plans its writes (#4381), every file the gate would keep
    # without evidence, with its edit kind, instead of any write or keep.
    _overwrite_plan: list[tuple[Path, str | None]] | None = None

    # Deploy-mode for the shared adopt predicate (:meth:`_check_adopt_or_skip`).
    # ``False`` = byte-preserving identity (the safe default any future
    # byte-preserving integrator inherits). Integrators that deploy via
    # ``write_text_lf`` (instruction / command / agent) override this to
    # ``True`` so a CRLF-source file already deployed as LF is adopted rather
    # than churned. Making the mode an explicit, named class attribute keeps
    # it out of a hardcoded literal buried inside the predicate.
    _LF_NORMALIZED_DEPLOY = False

    # How a path two packages deploy is settled, as this integrator applies it.
    # ``None`` = not declared: a caller predicting the final writer refuses.
    SHARED_PATH_RULE: str | None = None

    def __init__(self):
        self.link_resolver: UnifiedLinkResolver | None = None

    def note_rendered_write(self, path: Path) -> None:
        """Report one final-rendered file write, never an adopted or skipped file (#3701).

        The install service drains these through ``take_rendered_writes`` after
        each integrator call and records them as local deployment receipts.
        """
        self.__dict__.setdefault("_rendered_writes", []).append(path)

    def take_rendered_writes(self) -> list[Path]:
        """Return and forget the writes reported since the previous drain."""
        return self.__dict__.pop("_rendered_writes", [])

    @staticmethod
    def filter_authorized_files(files: list[Path], source_plan=None) -> list[Path]:
        """Keep only files admitted by the canonical deployment source plan."""
        if source_plan is None:
            return files
        from apm_cli.utils.paths import portable_relpaths

        relatives = portable_relpaths(files, source_plan.source_root)
        return [
            path
            for path, relative in zip(files, relatives, strict=True)
            if source_plan.includes(relative)
        ]

    # ------------------------------------------------------------------
    # Common behaviour  -- subclasses inherit directly
    # ------------------------------------------------------------------

    def should_integrate(self, project_root: Path) -> bool:
        """Check if integration should be performed (always True)."""
        return True

    # ------------------------------------------------------------------
    # Collision detection
    # ------------------------------------------------------------------

    @staticmethod
    def check_collision(
        target_path: Path,
        rel_path: str,
        managed_files: set[str] | None,
        force: bool,
        diagnostics=None,
    ) -> bool:
        """Return True if *target_path* is a user-authored collision.

        A collision exists when **all** of these are true:
        1. ``target_path`` already exists on disk
        2. ``rel_path`` is **not** in the managed set (-> user-authored)
        3. ``force`` is ``False``

        When ``managed_files`` is ``None`` it is treated as an empty set:
        no files are managed, so any pre-existing file at the target path
        is considered a user-authored collision and is protected from
        silent overwrite.

        When *diagnostics* is provided the skip is recorded there;
        otherwise a warning is emitted via ``_rich_warning``.

        .. note:: Callers must pre-normalize *managed_files* with
           forward-slash separators (see ``normalize_managed_files``).

        Raises:
            PathTraversalError: *target_path* itself is a symlink (dangling
                or not). Integrators never write through a linked
                destination, regardless of ``force`` or ownership.
        """
        if target_path.is_symlink():
            raise PathTraversalError(
                f"Refusing deployment through a symlinked destination: {rel_path}. "
                "Replace the symlink with a regular file or remove it, then retry."
            )
        if managed_files is None:
            managed_files = set()
        if not target_path.exists():
            return False
        # managed_files is pre-normalized at the call site  -- O(1) lookup
        rel_key = rel_path.replace("\\", "/")
        if rel_key in managed_files:
            return False
        if force:
            return False
        rel_parts = PurePosixPath(rel_key).parts
        if 0 < len(rel_parts) <= len(target_path.parts) and (
            stored_alias(target_path.parents[len(rel_parts) - 1], rel_key, managed_files)
            is not None
        ):
            # A managed file renamed upstream only by case/normalization is
            # still APM-owned on case-insensitive filesystems: its stored
            # spelling is the managed key (set lookups, no inventory scan).
            return False

        if diagnostics is not None:
            diagnostics.skip(rel_path)
        else:
            _rich_warning(
                f"Skipping {rel_path} -- local file exists (not managed by APM). "
                f"{LOCAL_FILE_UNMANAGED.sentence()}"
            )
        return True

    @staticmethod
    def normalize_managed_files(managed_files: set[str] | None) -> set[str] | None:
        """Normalize path separators once for O(1) lookups."""
        if managed_files is None:
            return None
        return {p.replace("\\", "/") for p in managed_files}

    @staticmethod
    def walk_source_tree(source: Path) -> Iterator[tuple[str, list[str], list[str]]]:
        """Enumerate an expected source tree without silently losing unreadable members."""

        def fail(error: OSError) -> None:
            raise OSError(
                f"Cannot enumerate source {source}: {error}. "
                "Restore readable source files and retry installation."
            ) from error

        try:
            if not stat.S_ISDIR(source.stat().st_mode):
                raise NotADirectoryError(f"{source} is not a directory")
        except OSError as error:
            fail(error)
        yield from os.walk(source, followlinks=False, onerror=fail)

    def install_user_scope(self) -> bool:
        """Return whether this integrator deploys at user scope; recovery advice uses -g."""
        return False

    def untrusted_member_message(self, entry: str, directory: str, destination: Path) -> str:
        """Refuse an unprovable member and name the skill directory and scoped retry."""
        user_scope = self.install_user_scope()
        return untrusted_baseline_message(
            entry,
            recovery_directory(directory, destination, user_scope=user_scope),
            user_scope=user_scope,
        )

    def _receipted_hash(self, path: Path) -> str | None:
        """The hash this checkout's deployment receipt binds for *path*, if any."""
        gate = self._overwrite_gate
        if gate is None or gate.session is None:
            return None
        return gate.session.receipted_hash(path, gate.targets)

    def receipted_unchanged(self, path: Path) -> bool:
        """Whether this checkout's receipt already binds *path*'s current bytes (RA-007)."""
        receipted = self._receipted_hash(path)
        return receipted is not None and receipted == self._file_hash(path)

    @staticmethod
    def _file_hash(path: Path) -> str | None:
        from apm_cli.utils.content_hash import compute_file_hash

        try:
            return compute_file_hash(path)
        except OSError:
            return None

    def _receipted_edit(self, path: Path, rendered_hash: str) -> str | None:
        """Classify a managed file a receipt proves the user edited (RA-012).

        ``"drift"`` when the package renders the receipted bytes (nothing new
        to deploy: kept, reported, exit 0); ``"blocked"`` when it renders new
        bytes the edit holds back. ``None`` without a receipt.
        """
        receipted = self._receipted_hash(path)
        if receipted is None or self._file_hash(path) in (None, receipted):
            return None
        return edit_kind(receipted, rendered_hash)

    def _rendered_hash(self, source_file: Path, expected_content: str | None) -> str:
        """Content hash of the bytes this integrator would deploy for one file."""
        from apm_cli.utils.content_hash import compute_file_hash, content_hash_of_bytes

        if expected_content is None:
            return compute_file_hash(source_file)
        text = (
            normalize_crlf_to_lf(expected_content)
            if self._LF_NORMALIZED_DEPLOY
            else expected_content
        )
        return content_hash_of_bytes(text.encode("utf-8"))

    @staticmethod
    def _member_edit(
        target_file: Path, source_file: Path, expected: str | None, *, untrusted: bool
    ) -> str | None:
        """Classify an existing member against the bytes APM recorded deploying.

        ``None`` writes it. ``"untrusted"``: no baseline proves APM wrote it.
        ``"drift"``: a user edit while the package brings the recorded bytes.
        ``"blocked"``: a user edit while the package brings new bytes (an
        upgrade the kept edit blocks). A file that already holds the package's
        bytes is rewritten, which records them.
        """
        from apm_cli.utils.content_hash import compute_file_hash

        if not target_file.is_file():
            return None
        if untrusted:
            return "untrusted"
        if expected is None:
            return None
        disk = compute_file_hash(target_file)
        if disk == expected:
            return None
        incoming = compute_file_hash(source_file)
        if incoming == disk:
            return None
        return edit_kind(expected, incoming)

    @staticmethod
    def unique_destinations(pairs: Iterable[tuple[Path, Path]]) -> dict[Path, Path] | None:
        """``{destination: source}``, or ``None`` when two sources share a destination."""
        planned: dict[Path, Path] = {}
        for destination, source in pairs:
            if planned.setdefault(destination, source) != source:
                return None
        return planned

    def admitted_source_listing(
        self, source: Path, ignore: Callable[[str, list[str]], list[str]]
    ) -> Iterator[tuple[str, list[str]]]:
        """Each directory of *source* a directory copy walks, with its admitted file names."""
        for directory, directories, filenames in self.walk_source_tree(source):
            ignored = set(ignore(directory, [*directories, *filenames]))
            directories[:] = sorted(name for name in directories if name not in ignored)
            yield directory, sorted(name for name in filenames if name not in ignored)

    def admitted_source_members(
        self, source: Path, ignore: Callable[[str, list[str]], list[str]]
    ) -> Iterator[Path]:
        """Every member of *source* a directory copy admits, in copy order."""
        for directory, filenames in self.admitted_source_listing(source, ignore):
            for name in filenames:
                yield Path(directory) / name

    def copy_directory_files(
        self,
        source: Path,
        destination: Path,
        *,
        ignore: Callable[[str, list[str]], list[str]],
        path_entry: Callable[[Path], str],
        is_managed: Callable[[str], bool],
        recorded_hashes: Mapping[str, str],
        write_allowed: Callable[[str], bool] | None = None,
        on_member_skipped: Callable[[Path], None] | None = None,
        baseline_hash: Callable[[Path], str | None] | None = None,
        on_member_written: Callable[[Path], None] | None = None,
        requires_write: Callable[[Path], bool] | None = None,
        unchanged: bool = False,
        force: bool = False,
        diagnostics=None,
        package: str = "",
    ) -> list[Path]:
        """Copy admitted members through the shared skill containment boundary.

        Skill copies route here for symlink and destination-containment checks,
        without deleting or inventorying destination siblings. *package* is the
        owning dependency key used to attribute refusal diagnostics.
        """

        copied: list[Path] = []
        destination_existed = destination.exists()
        for directory, filenames in self.admitted_source_listing(source, ignore):
            for name in filenames:
                source_file = Path(directory) / name
                target_file = destination / relative_to(source_file, source)
                if has_symlink_component(destination.parent, target_file, raise_on_error=True):
                    raise PathTraversalError(
                        f"Skill destination {target_file} contains a symlink -- refusing to deploy"
                    )
                ensure_path_within(target_file, destination)
                entry = path_entry(target_file)
                if write_allowed is not None and not write_allowed(entry):
                    if on_member_skipped is not None:
                        on_member_skipped(target_file)
                    continue
                if target_file.exists() and not target_file.is_file():
                    raise IsADirectoryError(
                        errno.EISDIR, os.strerror(errno.EISDIR), str(target_file)
                    )
                if (
                    unchanged
                    and self.is_content_identical_to_source(target_file, source_file)
                    and not (requires_write is not None and requires_write(target_file))
                ):
                    copied.append(target_file)
                    continue
                if self.check_collision(
                    target_file,
                    entry,
                    {entry} if is_managed(entry) else set(),
                    force,
                    diagnostics=diagnostics,
                ):
                    if on_member_skipped is not None:
                        on_member_skipped(target_file)
                    continue
                expected = (
                    baseline_hash(target_file)
                    if baseline_hash is not None
                    else recorded_hashes.get(entry)
                )
                state = (
                    None
                    if force
                    else self._member_edit(
                        target_file,
                        source_file,
                        expected,
                        untrusted=(baseline_hash is not None and expected is None),
                    )
                )
                if state is not None:
                    if state == "untrusted":
                        message = self.untrusted_member_message(
                            entry, path_entry(destination), destination
                        )
                    else:
                        message = kept_edit_message(
                            entry, user_scope=self.install_user_scope(), proven_edit=True
                        )
                    if diagnostics is not None:
                        if state == "drift":
                            diagnostics.warn(message)
                        else:
                            diagnostics.error(message, package=package)
                    else:
                        _rich_warning(message)
                    if on_member_skipped is not None:
                        on_member_skipped(target_file)
                    continue
                write_seam.mkdir(target_file.parent, parents=True, exist_ok=True)
                try:
                    write_seam.copy2(source_file, target_file)
                except OSError:
                    if not destination_existed:
                        self.cleanup_empty_parents([target_file], destination.parent)
                    raise
                copied.append(target_file)
                if on_member_written is not None:
                    on_member_written(target_file)
        return copied

    @staticmethod
    def is_content_identical_to_source(
        target_path: Path,
        source_path: Path,
        *,
        lf_normalized_deploy: bool = False,
    ) -> bool:
        """Return True if *target_path* matches what APM would deploy from *source_path*.

        Used by non-skill integrators to silently *adopt* a pre-existing
        on-disk file that already matches what APM would deploy.

        The comparison depends on *how the calling integrator deploys*, which
        the caller declares via ``lf_normalized_deploy``:

        * ``lf_normalized_deploy=False`` (default) -- *byte-preserving*
          deployers (the hook, kiro-hook, and canvas integrators copy the
          source verbatim with ``shutil.copy2`` / ``shutil.copyfile``). The
          deployed bytes equal the source bytes exactly, so "identical" is
          raw byte identity.
        * ``lf_normalized_deploy=True`` -- *LF-normalizing* deployers (the
          agent, instruction, prompt, and command integrators write through
          ``write_text_lf``). The deployed bytes equal the *LF-normalized*
          source, so the on-disk *target* is identical iff it equals what
          ``write_text_lf`` would write. Without this, a package whose source
          carries CRLF line endings (Windows text-mode checkout,
          ``core.autocrlf``, or a text-mode write) would never match the LF
          target, so every reinstall would needlessly re-integrate the file
          and report it as freshly installed (apm#1916).

        In the LF-normalizing mode only the *source* side is normalized and
        there is no raw-byte short-circuit: a stale CRLF *target* left by a
        pre-LF install -- even one whose raw bytes happen to equal a CRLF
        source on a ``core.autocrlf`` checkout -- still mismatches the LF
        bytes ``write_text_lf`` would emit and is rewritten to LF rather than
        adopted (apm#1889).

        Why this exists
        ---------------
        Without this short-circuit, the per-file loops in
        ``agent_integrator``, ``instruction_integrator``, ``prompt_integrator``
        and ``command_integrator`` would route the file straight into
        :meth:`check_collision`. When the path is missing from
        ``managed_files`` (e.g. lockfile was wiped, hand-edited, regenerated
        by an older APM build, or the user's previous install crashed before
        ``deployed_files`` was persisted) the file is treated as
        "user-authored", *skipped*, and never appended to ``target_paths``.

        That in turn leaves ``deployed_files`` empty in the new lockfile,
        which trips the ``required-packages-deployed`` policy check at the
        next install. Because ``policy_gate`` runs *before* ``integrate``
        in ``pipeline.py``, the install can never self-heal -- a permanent
        catch-22 lockout.

        ``skill_integrator`` already has an equivalent content-identity
        adopt at ``_promote_sub_skills`` (target.exists() +
        ``_dirs_equal``). This helper restores symmetry for non-skill
        primitives.

        Conservative by design
        ----------------------
        Only fires when the deployed bytes match the source content (modulo
        CRLF->LF normalization). Format-transforming writers (Codex/Kiro
        agents, link-rewritten agents, rules, prompts) must not use this
        raw-source comparison; they render first and pass the rendered text
        to :meth:`_check_adopt_or_skip` as ``expected_content``. This means we never silently
        adopt content that *might* have come from somewhere else; we only
        adopt files that are demonstrably the package's own content already
        on disk.

        TOCTOU hardening
        ----------------
        The classic ``is_symlink()`` -> ``read_bytes()`` sequence has a
        race window: a hostile co-tenant on the same machine could swap
        a regular file for a symlink between the two calls and cause
        ``read_bytes()`` to follow it to an attacker-controlled path,
        whose bytes might match source by construction. We close the
        race by reading through ``os.open(..., O_NOFOLLOW)`` so the
        kernel rejects the open atomically if the final component is a
        symlink. ``O_NOFOLLOW`` is a no-op constant on platforms that
        lack it (Windows), where the upfront ``is_symlink()`` check
        plus ``ensure_path_within`` at the call site provide adequate
        coverage (Windows also lacks the cheap unprivileged-symlink
        creation primitive that makes this race practical on POSIX).
        """
        try:
            if not target_path.exists() or not source_path.exists():
                return False
            # Cheap pre-check: reject obvious symlinks so we never even
            # attempt the open. Race-free verification follows below via
            # O_NOFOLLOW.
            if target_path.is_symlink() or source_path.is_symlink():
                return False
            try:
                target_bytes = _read_bytes_no_follow(target_path)
                source_bytes = _read_bytes_no_follow(source_path)
            except _SymlinkRaceError:
                # The path turned into a symlink between the pre-check
                # and the open() -- treat as non-identical so the caller
                # falls through to ``check_collision`` and the file is
                # NOT adopted. Silent (no diagnostic) because adopt is
                # an optimisation; the user-authored skip path is the
                # safe fallback.
                return False
            if not lf_normalized_deploy:
                # Byte-preserving deployer: deployed bytes equal source
                # bytes verbatim, so "identical" is raw byte identity.
                return target_bytes == source_bytes
            # LF-normalizing deployer: the target is identical iff it equals
            # what ``write_text_lf`` would write -- the LF-normalized source.
            # No raw-byte short-circuit: a stale CRLF target whose raw bytes
            # match a CRLF source must still be rewritten to LF, not adopted
            # (apm#1889).
            try:
                normalized_source = normalize_crlf_to_lf(source_bytes.decode("utf-8")).encode(
                    "utf-8"
                )
            except UnicodeDecodeError:
                # Non-UTF-8 source can't be line-normalized and
                # ``write_text_lf`` could not have produced it; fall back to
                # raw byte identity.
                return target_bytes == source_bytes
            return target_bytes == normalized_source
        except OSError:
            return False

    @staticmethod
    def try_adopt_identical(
        target_path: Path,
        source_path: Path,
        target_paths: list,
        *,
        lf_normalized_deploy: bool = False,
    ) -> bool:
        """Adopt *target_path* when it is identical to what would be deployed.

        Encapsulates the ``is_content_identical_to_source`` + append pattern
        for byte-preserving call sites (hook bundles) that deploy the source
        verbatim. Writers that transform content (agents, prompts) render
        first and adopt via :meth:`_check_adopt_or_skip` with
        ``expected_content`` instead.

        ``lf_normalized_deploy`` is forwarded to
        :meth:`is_content_identical_to_source`; byte-preserving deployers
        (hook, kiro-hook) keep the ``False`` default.

        Returns ``True`` and appends *target_path* to *target_paths* when the
        files are identical; returns ``False`` and leaves *target_paths*
        unchanged otherwise.
        """
        if BaseIntegrator.is_content_identical_to_source(
            target_path, source_path, lf_normalized_deploy=lf_normalized_deploy
        ):
            target_paths.append(target_path)
            return True
        return False

    @staticmethod
    def is_content_identical_to_text(
        target_path: Path,
        expected_content: str,
        *,
        lf_normalized_deploy: bool = False,
    ) -> bool:
        """Return whether a target matches already-prepared deployment text."""
        try:
            if not target_path.exists() or target_path.is_symlink():
                return False
            try:
                target_bytes = _read_bytes_no_follow(target_path)
            except _SymlinkRaceError:
                return False
            expected = (
                normalize_crlf_to_lf(expected_content) if lf_normalized_deploy else expected_content
            ).encode("utf-8")
            return target_bytes == expected
        except OSError:
            return False

    @classmethod
    def write_text_if_changed(
        cls,
        path: Path,
        text: str,
        writer: Callable[[Path, str], None] = write_text_lf,
    ) -> bool:
        """Write rendered *text* unless *path* already holds its LF bytes (#4184).

        Returns whether a write happened, so integrators count byte-identical
        renders as unchanged instead of freshly integrated. *writer* must
        LF-normalize (``write_text_lf`` / ``atomic_write_text``).
        """
        if cls.is_content_identical_to_text(path, text, lf_normalized_deploy=True):
            return False
        from apm_cli.utils.content_hash import text_write_hash, text_write_size

        write_seam.mkdir(path.parent, parents=True, exist_ok=True)
        expected = text_write_hash(text, lf=True)
        write_seam.record_before_write(path, expected=expected, size=text_write_size(text, lf=True))
        writer(path, text)
        write_seam.record_after_write(path)
        return True

    def _check_adopt_or_skip(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        managed_files: set[str] | None,
        force: bool,
        diagnostics,
        target_paths: list,
        *,
        expected_content: str | None = None,
        on_identical: Callable[[], None] | None = None,
    ) -> tuple[bool, bool]:
        """Check whether *target_path* should be adopted or skipped.

        Combines :meth:`is_content_identical_to_source` (adopt) and
        :meth:`check_collision` (skip) into a single call so integrators
        share the decision logic without code duplication.

        When adopting, *target_path* is appended to *target_paths* as a
        side effect so the caller's bookkeeping stays correct.

        The adopt comparison runs in this integrator's deploy mode
        (:attr:`_LF_NORMALIZED_DEPLOY`): LF-normalizing integrators compare
        the on-disk target against the LF-normalized source, while
        byte-preserving integrators compare raw bytes.

        Args:
            target_path: Destination path on disk.
            source_file: Source file to compare against; the file is adopted
                when the on-disk target already matches the deployed form of
                this source.
            rel_path: Relative path string used for collision detection and
                diagnostics.
            managed_files: Set of APM-managed relative paths; ``None`` means
                none managed.
            force: When ``True``, collisions are silently overwritten.
            diagnostics: Optional diagnostics collector; forwarded to
                :meth:`check_collision`.
            target_paths: Mutable list; *target_path* is appended on adopt.
            on_identical: Called when the on-disk file already holds the
                deployed bytes and is retained (adopted or receipted
                unchanged); never on planning, collision or kept-edit skips.

        Returns:
            ``(skip, adopted)`` -- when ``skip`` is ``True`` the caller must
            ``continue`` (or otherwise skip writing this file); ``adopted``
            is ``True`` only when the existing file already matched the
            deployed content and has been silently adopted.
        """
        identical = (
            self.is_content_identical_to_text(
                target_path,
                expected_content,
                lf_normalized_deploy=self._LF_NORMALIZED_DEPLOY,
            )
            if expected_content is not None
            else self.is_content_identical_to_source(
                target_path,
                source_file,
                lf_normalized_deploy=self._LF_NORMALIZED_DEPLOY,
            )
        )
        plan = self._overwrite_plan
        if identical:
            target_paths.append(target_path)
            if plan is not None:
                # Planning records only files it would keep, and an identical
                # file is never kept: its receipt cannot change the plan (#4381).
                return True, False
            if on_identical is not None:
                on_identical()
            # Already receipted with these bytes: unchanged, not an adoption (RA-007).
            if self.receipted_unchanged(target_path):
                return True, False
            # A managed file already holding the final render is APM's bytes:
            # record the receipt the skipped rewrite would have (#4184, #3701).
            if plan is None and managed_files and rel_path.replace("\\", "/") in managed_files:
                self.note_rendered_write(target_path)
            return True, True
        if self.check_collision(
            target_path, rel_path, managed_files, force, diagnostics=diagnostics
        ):
            return True, False
        kept = self.keeps_unproven_file(
            target_path,
            source_file,
            rel_path,
            force,
            diagnostics,
            target_paths,
            expected_content=expected_content,
        )
        return kept, False

    def keeps_edited_output(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        force: bool,
        diagnostics,
        target_paths: list,
        *,
        expected_content: str | None = None,
        held_back: bool = False,
    ) -> bool:
        """Gate one executable output (hook file or script, canvas file) like any file (#4626).

        A file already holding the render is not an overwrite. Outside an
        install session (a local bundle, hook re-integration) no overwrite
        gate is bound and the caller's own evidence applies.
        """
        if self._overwrite_gate is None or not target_path.is_file():
            return False
        identical = (
            self.is_content_identical_to_text(target_path, expected_content)
            if expected_content is not None
            else self.is_content_identical_to_source(target_path, source_file)
        )
        if identical:
            return False
        return self.keeps_unproven_file(
            target_path,
            source_file,
            rel_path,
            force,
            diagnostics,
            target_paths,
            expected_content=expected_content,
            held_back=held_back,
        )

    def keeps_unproven_file(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        force: bool,
        diagnostics,
        target_paths: list,
        *,
        expected_content: str | None = None,
        held_back: bool = False,
    ) -> bool:
        """Whether install keeps an existing managed file that differs from the render.

        The one overwrite gate (#4360, #4626): the file is replaced only with
        deployment evidence that APM wrote its current bytes (a matching
        receipt, legacy ownership or the locked render); the lockfile hash
        alone never authorizes it. A kept file stays in *target_paths* with its
        prior lock entry and finalize names it. While planning (#4381) nothing
        is written and every blocking keep goes to the plan. *held_back*: keeping
        this file holds back other new content, so even a drift edit blocks.
        """
        gate = self._overwrite_gate
        plan = self._overwrite_plan
        if force or not target_path.is_file() or (gate and gate.authorizes(target_path, rel_path)):
            # One literal write-permitting return (install-deployment-overwrite-evidence).
            if plan is not None:  # noqa: SIM103
                return True
            return False
        if gate is not None and (
            gate.legacy_adoptable(target_path, rel_path)
            if plan is not None
            else gate.adopt_legacy(target_path, rel_path)
        ):
            target_paths.append(target_path)
            return True
        rendered = self._rendered_hash(source_file, expected_content)
        edit = (
            gate.legacy_edit(target_path, rel_path, rendered)
            or self._receipted_edit(target_path, rendered)
            if gate is not None
            else None
        )
        if held_back and edit == "drift":
            edit = "blocked"
        if plan is not None:
            # Planning writes nothing; a blocking keep refuses the whole plan (#4381).
            if edit != "drift":
                plan.append((target_path, edit))
        elif gate is not None and gate.keep(target_path, rel_path, edit=edit):
            # The claim stays with its prior lock entry; finalize names the file.
            target_paths.append(target_path)
        else:
            # A proven edit no session can keep still names the true reason (RA-012).
            message = kept_edit_message(
                rel_path, user_scope=bool(gate and gate.user_scope), proven_edit=edit is not None
            )
            if diagnostics is not None:
                diagnostics.error(message, package=gate.owner if gate else "")
            else:
                _rich_warning(message)
        return True

    # Known integration prefixes that APM is allowed to deploy/remove under.
    # Derived from ``targets.KNOWN_TARGETS`` so adding a target auto-propagates.
    @staticmethod
    def _get_integration_prefixes(targets=None, *, user_scope: bool = False) -> tuple:
        from apm_cli.integration.targets import get_integration_prefixes

        return get_integration_prefixes(targets=targets, user_scope=user_scope)

    @staticmethod
    def validate_deploy_path(
        rel_path: str,
        project_root: Path,
        allowed_prefixes: tuple | None = None,
        targets=None,
        user_scope: bool = False,
        *,
        resolved_project_root: Path | None = None,
        allow_final_symlink: bool = False,
    ) -> bool:
        """Return True if *rel_path* is safe for APM to deploy or remove.

        Centralised security gate for all paths read from ``deployed_files``
        before any filesystem operation.

        When *targets* is provided, allowed prefixes are derived from
        those (scope-resolved) profiles.  Otherwise uses all known
        project prefixes, plus known user roots when *user_scope* is true.
        Callers validating multiple paths may pass precomputed values.
        Set *allow_final_symlink* only when unlinking the final path itself.

        Checks:
        1. No path-traversal components (``..``) and no ``.`` segment:
           only canonical paths are admitted
        2. Starts with an allowed integration prefix
        3. Resolves within *project_root*, a configured absolute target root,
           or the cowork root for ``cowork://`` paths
        """
        from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

        if ".." in rel_path or "." in rel_path.replace("\\", "/").split("/"):
            return False

        candidate = Path(rel_path)
        if candidate.is_absolute():
            return (
                _managed_absolute_target_root(
                    candidate,
                    targets,
                    allow_final_symlink=allow_final_symlink,
                )
                is not None
            )

        if allowed_prefixes is None:
            allowed_prefixes = BaseIntegrator._get_integration_prefixes(
                targets=targets,
                user_scope=user_scope,
            )

        # --- cowork:// paths: validate against cowork root ---
        if rel_path.startswith(COWORK_URI_SCHEME):
            if not rel_path.startswith(allowed_prefixes):
                return False
            # Resolve to absolute and validate containment against cowork root.
            try:
                from apm_cli.integration.copilot_cowork_paths import (
                    cowork_locator,
                    from_lockfile_path,
                    resolve_copilot_cowork_skills_dir,
                )

                cowork_root = resolve_copilot_cowork_skills_dir()
                if cowork_root is None:
                    return False
                if allow_final_symlink:
                    # Contain the parent, refuse linked ancestors, never follow
                    # the final link: the project gate's rule for cowork:// (#5022).
                    cowork_locator(rel_path, cowork_root, reject_linked_ancestors=True)
                else:
                    # from_lockfile_path internally calls ensure_path_within.
                    from_lockfile_path(rel_path, cowork_root)
                return True
            except Exception:
                return False

        if not rel_path.startswith(allowed_prefixes):
            return False
        target = project_root / rel_path
        try:
            if has_symlink_component(project_root, target.parent):
                return False
            root = resolved_project_root or resolve_path(project_root)
            containment_target = target.parent if allow_final_symlink else target
            if not is_within(resolve_path(containment_target), root):
                return False
        except (ValueError, OSError):
            return False
        return True

    @staticmethod
    def resolve_deploy_path(
        rel_path: str,
        project_root: Path,
        allowed_prefixes: tuple | None = None,
        targets=None,
    ) -> Path:
        """Return a safe project deployment path without following symlinks."""
        target = project_root / rel_path
        if has_symlink_component(project_root, target):
            raise PathTraversalError(
                f"Refusing deployment through a symlinked path component: {rel_path}"
            )
        if not BaseIntegrator.validate_deploy_path(
            rel_path,
            project_root,
            allowed_prefixes=allowed_prefixes,
            targets=targets,
        ):
            raise PathTraversalError(f"Refusing unsafe deployment path: {rel_path}")
        return ensure_path_within(target, project_root)

    # Backward-compat aliases mapping raw ``{prim}_{target}`` keys to
    # the bucket names that existing callers expect.  Shared between
    # ``partition_managed_files`` and ``partition_bucket_key`` so the
    # mapping is defined exactly once.
    _BUCKET_ALIASES: dict = {  # noqa: RUF012
        "prompts_copilot": "prompts",
        "agents_copilot": "agents_github",
        "commands_claude": "commands",
        "commands_cursor": "commands_cursor",
        "commands_opencode": "commands_opencode",
        "instructions_copilot": "instructions",
        "instructions_cursor": "rules_cursor",
        "instructions_claude": "rules_claude",
    }

    @staticmethod
    def partition_bucket_key(prim_name: str, target_name: str) -> str:
        """Return the canonical bucket key for a (primitive, target) pair.

        Applies backward-compat aliases so callers stay in sync with
        ``partition_managed_files`` bucket naming.
        """
        raw = f"{prim_name}_{target_name}"
        return BaseIntegrator._BUCKET_ALIASES.get(raw, raw)

    @staticmethod
    def partition_managed_files(
        managed_files: set[str],
        targets=None,
    ) -> dict:
        """Partition *managed_files* by integration prefix in a single pass.

        When *targets* is provided, prefixes and bucket keys are derived
        from those (scope-resolved) profiles.  Otherwise falls back to
        ``KNOWN_TARGETS`` for backward compatibility.

        Bucket keys are generated dynamically so adding a new target or
        primitive automatically creates the corresponding bucket.

        Cross-target buckets (``skills``, ``hooks``) group all targets
        together because ``SkillIntegrator`` and ``HookIntegrator``
        handle multi-target sync internally.

        Path routing uses a longest-prefix-match strategy so multi-level
        roots like ``.config/opencode/`` are handled correctly.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        source = targets if targets is not None else KNOWN_TARGETS.values()

        buckets: dict = {}

        # Skills and hooks are cross-target (single bucket each)
        skill_prefixes: list = []
        hook_prefixes: list = []

        # prefix -> bucket_key (longest-prefix-match routing)
        prefix_map: dict = {}

        for target in source:
            for prim_name, mapping in target.primitives.items():
                # Dynamic-root targets (cowork, copilot-app) use URI prefixes.
                if target.resolved_deploy_root is not None:
                    if prim_name == "skills":
                        from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

                        skill_prefixes.append(COWORK_LOCKFILE_PREFIX)
                    elif target.name == "copilot-app":
                        from apm_cli.integration.copilot_app_db import (
                            COPILOT_APP_LOCKFILE_PREFIX,
                        )

                        raw_key = f"{prim_name}_{target.name}"
                        bucket_key = BaseIntegrator._BUCKET_ALIASES.get(raw_key, raw_key)
                        if bucket_key not in buckets:
                            buckets[bucket_key] = set()
                        prefix_map[COPILOT_APP_LOCKFILE_PREFIX] = bucket_key
                    continue
                effective_root = mapping.deploy_root or target.root_dir
                # Canonical spelling, so a root of "." routes "skills/..." claims (#5096).
                prefix = posixpath.normpath(
                    f"{effective_root}/{mapping.subdir}" if mapping.subdir else effective_root
                )
                prefix = "" if prefix == "." else f"{prefix}/"
                if not prefix:
                    continue  # a primitive at the root itself owns no prefix
                if prim_name == "skills":
                    skill_prefixes.append(prefix)
                elif prim_name == "hooks":
                    hook_prefixes.append(prefix)
                else:
                    raw_key = f"{prim_name}_{target.name}"
                    bucket_key = BaseIntegrator._BUCKET_ALIASES.get(raw_key, raw_key)
                    if bucket_key not in buckets:
                        buckets[bucket_key] = set()
                    prefix_map[prefix] = bucket_key

        buckets["skills"] = set()
        buckets["hooks"] = set()

        skill_tuple = tuple(skill_prefixes)
        hook_tuple = tuple(hook_prefixes)

        # Build a prefix trie keyed by path segments for O(depth) routing.
        # Each node is a dict; the special key "_bucket" stores the bucket
        # for a complete prefix ending at that node.  This preserves the
        # "single pass, O(1) per path" property from the original
        # component_map approach while supporting multi-level roots like
        # .config/opencode/.
        #
        # Cross-target hooks/skills prefixes are registered in the trie too
        # so a deeper prefix (e.g. ``.copilot/hooks/``) wins over a shallow
        # catch-all prefix from another primitive (e.g. the user-scope
        # Copilot instructions root ``.copilot/``). Without this, hook paths
        # under a catch-all root would be misrouted and never cleaned up.
        trie: dict = {}
        for prefix, bucket_key in prefix_map.items():
            segments = [s for s in prefix.split("/") if s]
            node = trie
            for segment in segments:
                child = node.get(segment)
                if child is None:
                    child = {}
                    node[segment] = child
                node = child
            node["_bucket"] = bucket_key
        for prefix, bucket_key in ((skill_tuple, "skills"), (hook_tuple, "hooks")):
            for cross_prefix in prefix:
                segments = [s for s in cross_prefix.split("/") if s]
                node = trie
                for segment in segments:
                    child = node.get(segment)
                    if child is None:
                        child = {}
                        node[segment] = child
                    node = child
                node["_bucket"] = bucket_key

        for p in managed_files:
            # Walk the trie; keep the deepest bucket match (longest prefix).
            segments = [s for s in p.split("/") if s]
            node = trie
            last_bucket: str | None = None
            for segment in segments:
                child = node.get(segment)
                if child is None:
                    break
                node = child
                bk = node.get("_bucket")
                if bk is not None:
                    last_bucket = bk
            if last_bucket is not None:
                buckets[last_bucket].add(p)
                continue
            # Fall back to cross-target buckets
            if p.startswith(skill_tuple):
                buckets["skills"].add(p)
            elif p.startswith(hook_tuple):
                buckets["hooks"].add(p)

        return buckets

    @staticmethod
    def cleanup_empty_parents(
        deleted_paths: list[Path],
        stop_at: Path,
    ) -> None:
        """Remove empty parent directories in a single bottom-up pass.

        Collects all parent directories of *deleted_paths*, sorts by
        depth descending, and removes each if empty  -- O(H+D) syscalls
        instead of the per-file O(HxD) approach.

        Args:
            deleted_paths: Paths that were deleted (files or dirs).
            stop_at: Do not remove this directory or any ancestor.
        """
        if not deleted_paths:
            return
        stop_resolved = stop_at.resolve()
        # A project's target roots stay (#4132); at user scope the same names
        # are roots APM created, which go with their last file.
        target_roots = (
            frozenset()
            if stop_resolved == Path.home().resolve()
            else _target_root_dirs(stop_resolved)
        )
        # Collect unique parents (skip stop_at itself)
        candidates: set = set()
        for p in deleted_paths:
            cleanup_boundary = stop_resolved
            try:
                if not p.resolve().is_relative_to(stop_resolved):
                    cleanup_boundary = _managed_absolute_target_root(p, targets=None)
                    if cleanup_boundary is None:
                        cleanup_boundary = p.parent.resolve()
            except (ValueError, OSError):
                cleanup_boundary = p.parent.resolve()
            parent = p.parent
            while (
                parent not in (parent.parent, stop_at)
                and parent.resolve() != cleanup_boundary
                and parent.resolve() not in target_roots
            ):
                candidates.add(parent)
                parent = parent.parent
        # Sort deepest-first for safe bottom-up removal
        for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
            try:
                if d.exists() and not any(d.iterdir()):
                    write_seam.rmdir(d)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Link resolution helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_root_local_package(package_info, project_root: Path) -> bool:
        """Return True when *package_info* represents the project's own
        synthetic ``_local`` package (install_path == project_root).

        Used to scope discovery to ``.apm/`` and ``.github/`` instead of
        walking the entire project tree -- see issue #1507.

        During drift replay *project_root* is the scratch directory;
        fall back to ``root_local_project_root`` when set (same pattern
        as ``HookIntegrator._is_root_local_package``).
        """
        try:
            resolved_install = resolve_path(Path(package_info.install_path))
            if resolved_install == resolve_path(Path(project_root)):
                return True
            root_local = getattr(package_info, "root_local_project_root", None)
            if root_local is not None:
                return resolved_install == Path(root_local).resolve()
            return False
        except (OSError, RuntimeError):
            return False

    def init_link_resolver(
        self,
        package_info,
        project_root: Path,
        *,
        target=None,
        source_plan=None,
        skill_pairing=None,
    ) -> None:
        """Initialise and register the link resolver for a package.

        With *target* and *source_plan* (and the effective *skill_pairing*),
        links to files a promoted skill of the package deploys for *target*
        point at the deployed copy (#5523).
        """
        from apm_cli.utils.realpath import resolve_path

        self.link_resolver = UnifiedLinkResolver(project_root)
        try:
            install_path = Path(package_info.install_path)
            project_root = Path(project_root)
            home_root = Path.home()
            # Determine which directories to scan for primitives.
            # Default: the package's install_path itself (for real
            # installed dependencies under apm_modules/...).
            #
            # Narrowing rules:
            # - $HOME (user-scope local package): scan only ~/.apm/ to
            #   avoid recursive-globbing the entire home tree (#830).
            # - Project-scope synthetic _local package (install_path
            #   equals project_root): scan only ./.apm/ and ./.github/
            #   to avoid full-tree walks on large monorepos (#1507).
            #   Generic patterns like ``**/*.instructions.md`` would
            #   otherwise traverse every file in the repo even when
            #   the user only has a handful of primitives under .apm/.
            if resolve_path(install_path) == resolve_path(home_root):
                home_apm_root = install_path / ".apm"
                scan_roots = [home_apm_root] if home_apm_root.is_dir() else []
                narrowed_local = False
            elif self._is_root_local_package(package_info, project_root):
                candidates = [install_path / ".apm", install_path / ".github"]
                scan_roots = [p for p in candidates if p.is_dir()]
                narrowed_local = True
            else:
                scan_roots = [install_path]
                narrowed_local = False

            for root in scan_roots:
                primitives = discover_primitives(root)
                self.link_resolver.register_contexts(primitives)
            # Context and memory links render in live deployment coordinates
            # for every package, the $HOME user root included (#4832).
            self.link_resolver.deployment_frame = getattr(package_info, "deployment_frame", None)

            # Generalized in-package asset link rewriting (#1147) needs the
            # authoritative source-package root. Use install_path directly:
            # for installed deps it is apm_modules/<owner>/<repo>/ (or any
            # ADO/virtual subdir variant), for local packages it is the
            # package's apm_modules/_local/<name>/ copy. For the project-
            # scope synthetic _local package, the authoritative root is
            # the project itself, even though discovery was narrowed to
            # .apm/ and .github/. Skip only when scan_root was narrowed
            # to ~/.apm/ (user-scope $HOME) so we do not let asset links
            # escape the .apm/ boundary on $HOME packages.
            if resolve_path(install_path) != resolve_path(home_root) and install_path.is_dir():
                if narrowed_local or (len(scan_roots) == 1 and scan_roots[0] == install_path):
                    self.link_resolver.package_root = Path(install_path)
                    self.link_resolver.deployment_package_root = Path(
                        package_info.deployment_package_root or install_path
                    )
                    self.link_resolver.deployment_frame = getattr(
                        package_info, "deployment_frame", None
                    )
                    if target is not None:
                        from apm_cli.integration.skill_preflight import planned_skill_files

                        self.link_resolver.deployed_link_targets = planned_skill_files(
                            package_info, target, project_root, source_plan, skill_pairing
                        )
        except Exception:
            self.link_resolver = None

    def projected_resolve_links(self, package_info) -> Callable[..., tuple] | None:
        """Render another source of the current package into its deployment frame.

        Used to reproduce what an earlier version deployed at the same
        location (#3625). Returns ``None`` without an initialized resolver.
        """
        current = self.link_resolver
        if current is None or package_info is None:
            return None
        renderer = BaseIntegrator()
        renderer.init_link_resolver(
            replace(
                package_info,
                deployment_package_root=current.deployment_package_root or current.package_root,
                deployment_frame=current.deployment_frame,
            ),
            current.base_dir,
        )
        return renderer.resolve_links if renderer.link_resolver is not None else None

    def resolve_links(
        self,
        content: str,
        source: Path,
        target: Path,
        preserved_source_root: Path | None = None,
    ) -> tuple:
        """Resolve context links in *content*.

        Returns:
            ``(resolved_content, links_resolved_count)``
        """
        if not self.link_resolver:
            return content, 0

        resolved = self.link_resolver.resolve_links_for_installation(
            content=content,
            source_file=source,
            target_file=target,
            preserved_source_root=preserved_source_root,
        )
        if resolved == content:
            return content, 0

        link_pattern = re.compile(r"\]\(([^)]+)\)")
        original_links = set(link_pattern.findall(content))
        resolved_links = set(link_pattern.findall(resolved))
        return resolved, len(original_links - resolved_links)

    # ------------------------------------------------------------------
    # Sync (manifest-based file removal)
    # ------------------------------------------------------------------

    @staticmethod
    def sync_remove_files(
        project_root: Path,
        managed_files: set[str] | None,
        prefix: str,
        legacy_glob_dir: Path | None = None,
        legacy_glob_pattern: str | None = None,
        targets=None,
        logger=None,
        *,
        evidence: "DeploymentEvidence | None",
    ) -> dict[str, int]:
        """Remove APM-managed files matching *prefix* from *managed_files*.

        Lockfile-listed files are removed only through the cleanup owner,
        which requires deployment evidence (#3701): *evidence* has no default,
        and ``None`` keeps every listed file with a warning. Falls back to a
        legacy glob when *managed_files* is ``None``.

        Args:
            project_root: Workspace root.
            managed_files: Set of workspace-relative paths.
            prefix: Only process paths that start with this prefix
                    (e.g. ``".github/prompts/"``).
            legacy_glob_dir: Directory to glob inside for the legacy fallback.
            legacy_glob_pattern: Glob pattern for legacy fallback
                                 (e.g. ``"*-apm.prompt.md"``).
            targets: Optional target profiles for path validation.
                     Passed through to ``validate_deploy_path()`` so
                     user-scope prefixes are recognised.
            logger: Unused; diagnostics render through the cleanup owner.
            evidence: Deployment evidence for this transition.

        Returns:
            ``{"files_removed": int, "errors": int}``
        """
        stats: dict[str, int] = {"files_removed": 0, "errors": 0}

        if managed_files is not None:
            from apm_cli.integration.cleanup import remove_synced_files

            cleanup = remove_synced_files(
                managed_files,
                project_root,
                prefixes=(prefix,),
                dep_key="<sync>",
                targets=targets,
                evidence=evidence,
            )
            stats["files_removed"] = len(cleanup.deleted)
            stats["errors"] = len(cleanup.failed)
        elif legacy_glob_dir and legacy_glob_pattern and legacy_glob_dir.exists():
            for f in legacy_glob_dir.glob(legacy_glob_pattern):
                try:
                    write_seam.unlink(f)
                    stats["files_removed"] += 1
                except Exception:
                    stats["errors"] += 1

        return stats

    # ------------------------------------------------------------------
    # File-discovery helpers (reusable globs)
    # ------------------------------------------------------------------

    @staticmethod
    def find_files_by_glob(
        package_path: Path,
        pattern: str,
        subdirs: list[str] | None = None,
    ) -> list[Path]:
        """Search *package_path* (and optional subdirectories) for *pattern*.

        Symlinks are rejected outright to prevent traversal attacks.

        Args:
            package_path: Root of the installed package.
            pattern: Glob pattern (e.g. ``"*.prompt.md"``).
            subdirs: Extra subdirectory paths relative to *package_path*
                     to search (e.g. ``[".apm/prompts"]``).

        Returns:
            De-duplicated list of matching ``Path`` objects.
        """
        from apm_cli.utils.realpath import resolve_path

        results: list[Path] = []
        seen: set = set()

        dirs = [package_path]
        if subdirs:
            dirs.extend(package_path / s for s in subdirs)

        for d in dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob(pattern)):
                if f.is_symlink():
                    continue
                # Hardlink containment: a hardlink is a second directory
                # entry pointing at an inode that may live anywhere on
                # the filesystem.  Path.resolve() returns the hardlink's
                # own path (inside the package root), so the
                # is_relative_to check below cannot catch it.  Reject
                # any file with link-count > 1 to prevent a malicious
                # package shipping a hardlink to (e.g.) /etc/passwd
                # from being read or copied via integration.  False
                # positives are vanishingly rare for ``.prompt.md``-style
                # source files which should always be plain regular files.
                try:
                    if f.stat().st_nlink > 1:
                        continue
                except OSError:
                    continue
                resolved = resolve_path(f)
                # Defense-in-depth containment guard: skip files whose
                # resolved path escapes the package root.  Belt-and-
                # suspenders against any future regression in the
                # is_symlink() check above.  See path_security.ensure_path_within
                # for the canonical predicate; we inline the check here to stay
                # loop-fast and avoid raising on every malicious entry.
                try:
                    pkg_resolved = resolve_path(package_path)
                    if not is_within(resolved, pkg_resolved):
                        continue
                except (ValueError, OSError):
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    results.append(f)

        return results
