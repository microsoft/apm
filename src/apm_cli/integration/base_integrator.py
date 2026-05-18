"""Base integrator with shared collision detection and sync logic."""

import re
from dataclasses import dataclass
from pathlib import Path

from apm_cli.compilation.link_resolver import UnifiedLinkResolver
from apm_cli.primitives.discovery import discover_primitives
from apm_cli.utils.console import _rich_warning

from . import _file_ops, _partition, _sync
from ._opts import SyncRemoveOpts


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


class BaseIntegrator:
    """Shared infrastructure for file-level integrators.

    Subclasses only need to override the abstract hooks; the collision
    detection, sync removal, and link resolution logic is
    handled here.
    """

    def __init__(self):
        self.link_resolver: UnifiedLinkResolver | None = None

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
        """
        if managed_files is None:
            managed_files = set()
        if not target_path.exists():
            return False
        # managed_files is pre-normalized at the call site  -- O(1) lookup
        if rel_path.replace("\\", "/") in managed_files:
            return False
        if force:
            return False

        if diagnostics is not None:
            diagnostics.skip(rel_path)
        else:
            _rich_warning(
                f"Skipping {rel_path} — local file exists (not managed by APM). "
                f"Use 'apm install --force' to overwrite."
            )
        return True

    @staticmethod
    def normalize_managed_files(managed_files: set[str] | None) -> set[str] | None:
        """Normalize path separators once for O(1) lookups."""
        if managed_files is None:
            return None
        return {p.replace("\\", "/") for p in managed_files}

    @staticmethod
    def is_content_identical_to_source(target_path: Path, source_path: Path) -> bool:
        """Return True if *target_path* is byte-identical to *source_path*.

        Uses TOCTOU-hardened O_NOFOLLOW reads to prevent symlink-race
        adoption of attacker-controlled content.  Full rationale in
        :func:`._file_ops.is_content_identical_to_source`.
        """
        return _file_ops.is_content_identical_to_source(target_path, source_path)

    def _check_adopt_or_skip(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        managed_files: set[str] | None,
        force: bool,
        diagnostics,
        target_paths: list,
    ) -> tuple[bool, bool]:
        """Check whether *target_path* should be adopted or skipped.

        Combines :meth:`is_content_identical_to_source` (adopt) and
        :meth:`check_collision` (skip) into a single call so integrators
        share the decision logic without code duplication.

        When adopting, *target_path* is appended to *target_paths* as a
        side effect so the caller's bookkeeping stays correct.

        Args:
            target_path: Destination path on disk.
            source_file: Source file to compare against for byte-identity.
            rel_path: Relative path string used for collision detection and
                diagnostics.
            managed_files: Set of APM-managed relative paths; ``None`` means
                none managed.
            force: When ``True``, collisions are silently overwritten.
            diagnostics: Optional diagnostics collector; forwarded to
                :meth:`check_collision`.
            target_paths: Mutable list; *target_path* is appended on adopt.

        Returns:
            ``(skip, adopted)`` — when ``skip`` is ``True`` the caller must
            ``continue`` (or otherwise skip writing this file); ``adopted``
            is ``True`` only when the existing file was byte-identical and
            has been silently adopted.
        """
        if self.is_content_identical_to_source(target_path, source_file):
            target_paths.append(target_path)
            return True, True
        if self.check_collision(
            target_path, rel_path, managed_files, force, diagnostics=diagnostics
        ):
            return True, False
        return False, False

    # Known integration prefixes that APM is allowed to deploy/remove under.
    # Derived from ``targets.KNOWN_TARGETS`` so adding a target auto-propagates.
    @staticmethod
    def _get_integration_prefixes(targets=None) -> tuple:
        from apm_cli.integration.targets import get_integration_prefixes

        return get_integration_prefixes(targets=targets)

    @staticmethod
    def validate_deploy_path(
        rel_path: str,
        project_root: Path,
        allowed_prefixes: tuple | None = None,
        targets=None,
    ) -> bool:
        """Return True if *rel_path* is safe for APM to deploy or remove.

        Centralised security gate for all paths read from ``deployed_files``
        before any filesystem operation.

        When *targets* is provided, allowed prefixes are derived from
        those (scope-resolved) profiles.  Otherwise uses all known
        target prefixes.

        Checks:
        1. No path-traversal components (``..``)
        2. Starts with an allowed integration prefix
        3. Resolves within *project_root* (or within the cowork root
           for ``cowork://`` paths)
        """
        from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

        if allowed_prefixes is None:
            allowed_prefixes = BaseIntegrator._get_integration_prefixes(targets=targets)
        if ".." in rel_path:
            return False

        # --- cowork:// paths: validate against cowork root ---
        if rel_path.startswith(COWORK_URI_SCHEME):
            if not rel_path.startswith(allowed_prefixes):
                return False
            # Resolve to absolute and validate containment against cowork root.
            try:
                from apm_cli.integration.copilot_cowork_paths import (
                    from_lockfile_path,
                    resolve_copilot_cowork_skills_dir,
                )

                cowork_root = resolve_copilot_cowork_skills_dir()
                if cowork_root is None:
                    return False
                # from_lockfile_path internally calls ensure_path_within.
                from_lockfile_path(rel_path, cowork_root)
                return True
            except Exception:
                return False

        if not rel_path.startswith(allowed_prefixes):
            return False
        target = project_root / rel_path
        try:
            if not target.resolve().is_relative_to(project_root.resolve()):
                return False
        except (ValueError, OSError):
            return False
        return True

    # Backward-compat aliases; canonical definition lives in ``_partition``.
    _BUCKET_ALIASES: dict = _partition._BUCKET_ALIASES  # noqa: RUF012

    @staticmethod
    def partition_bucket_key(prim_name: str, target_name: str) -> str:
        """Return the canonical bucket key for a (primitive, target) pair.

        Applies backward-compat aliases so callers stay in sync with
        ``partition_managed_files`` bucket naming.  Implementation in
        :func:`._partition.partition_bucket_key`.
        """
        return _partition.partition_bucket_key(prim_name, target_name)

    @staticmethod
    def partition_managed_files(
        managed_files: set[str],
        targets=None,
    ) -> dict:
        """Partition *managed_files* by integration prefix in a single pass.

        When *targets* is provided, prefixes and bucket keys are derived
        from those (scope-resolved) profiles.  Otherwise falls back to
        ``KNOWN_TARGETS`` for backward compatibility.  Full implementation
        in :func:`._partition.partition_managed_files`.
        """
        return _partition.partition_managed_files(managed_files, targets=targets)

    @staticmethod
    def cleanup_empty_parents(
        deleted_paths: list[Path],
        stop_at: Path,
    ) -> None:
        """Remove empty parent directories in a single bottom-up pass.

        Full implementation in :func:`._sync.cleanup_empty_parents`.
        """
        return _sync.cleanup_empty_parents(deleted_paths, stop_at)

    # ------------------------------------------------------------------
    # Link resolution helpers
    # ------------------------------------------------------------------

    def init_link_resolver(self, package_info, project_root: Path) -> None:
        """Initialise and register the link resolver for a package."""
        self.link_resolver = UnifiedLinkResolver(project_root)
        try:
            scan_root = package_info.install_path
            # When install_path is $HOME (user-scope local package),
            # only scan the .apm/ subdirectory to avoid recursive-
            # globbing the entire home tree.  See issue #830.
            if scan_root == Path.home():
                scan_root = scan_root / ".apm"
            primitives = discover_primitives(scan_root)
            self.link_resolver.register_contexts(primitives)
            # Generalized in-package asset link rewriting (#1147) needs the
            # authoritative source-package root. Use install_path directly:
            # for installed deps it is apm_modules/<owner>/<repo>/ (or any
            # ADO/virtual subdir variant), for local packages it is the
            # package's apm_modules/_local/<name>/ copy. Skip when scan_root
            # was narrowed to .apm/ (user-scope) so we do not let asset
            # links escape the .apm/ boundary on $HOME packages.
            if scan_root == package_info.install_path and Path(scan_root).is_dir():
                self.link_resolver.package_root = Path(scan_root)
        except Exception:
            self.link_resolver = None

    def resolve_links(self, content: str, source: Path, target: Path) -> tuple:
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
        opts: SyncRemoveOpts | None = None,
        **legacy_kwargs,
    ) -> dict[str, int]:
        """Remove APM-managed files matching *prefix* from *managed_files*.

        Falls back to a legacy glob when *managed_files* is ``None``.
        Full implementation in :func:`._sync.sync_remove_files`.
        """
        resolved_opts = opts or SyncRemoveOpts(
            legacy_glob_dir=legacy_kwargs.get("legacy_glob_dir"),
            legacy_glob_pattern=legacy_kwargs.get("legacy_glob_pattern"),
            targets=legacy_kwargs.get("targets"),
            logger=legacy_kwargs.get("logger"),
            warn_fn=legacy_kwargs.get("warn_fn"),
        )
        if resolved_opts.warn_fn is None:
            resolved_opts = SyncRemoveOpts(
                legacy_glob_dir=resolved_opts.legacy_glob_dir,
                legacy_glob_pattern=resolved_opts.legacy_glob_pattern,
                targets=resolved_opts.targets,
                logger=resolved_opts.logger,
                warn_fn=_rich_warning,
            )
        return _sync.sync_remove_files(project_root, managed_files, prefix, resolved_opts)

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

        Symlinks and hardlinks escaping the package root are rejected to
        prevent traversal attacks.  Full implementation in
        :func:`._file_ops.find_files_by_glob`.
        """
        return _file_ops.find_files_by_glob(package_path, pattern, subdirs)
