"""Base integrator with shared collision detection and sync logic."""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from dataclasses import dataclass, field

from apm_cli.compilation.link_resolver import UnifiedLinkResolver
from apm_cli.primitives.discovery import discover_primitives
from apm_cli.utils.console import _rich_warning


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
    target_paths: List[Path]
    links_resolved: int = 0

    # Hook-specific (default 0 when not applicable)
    scripts_copied: int = 0

    # Skill-specific (default 0/False when not applicable)
    sub_skills_promoted: int = 0
    skill_created: bool = False


class BaseIntegrator:
    """Shared infrastructure for file-level integrators.

    Subclasses only need to override the abstract hooks; the collision
    detection, sync removal, and link resolution logic is
    handled here.
    """

    def __init__(self):
        self.link_resolver: Optional[UnifiedLinkResolver] = None

    # ------------------------------------------------------------------
    # Common behaviour  -- subclasses inherit directly
    # ------------------------------------------------------------------

    def should_integrate(self, project_root: Path) -> bool:  # noqa: ARG002
        """Check if integration should be performed (always True)."""
        return True

    # ------------------------------------------------------------------
    # Collision detection
    # ------------------------------------------------------------------

    @staticmethod
    def check_collision(
        target_path: Path,
        rel_path: str,
        managed_files: Optional[Set[str]],
        force: bool,
        diagnostics=None,
    ) -> bool:
        """Return True if *target_path* is a user-authored collision.

        A collision exists when **all** of these are true:
        1. ``managed_files`` is not ``None`` (manifest mode)
        2. ``target_path`` already exists on disk
        3. ``rel_path`` is **not** in the managed set (-> user-authored)
        4. ``force`` is ``False``

        When *diagnostics* is provided the skip is recorded there;
        otherwise a warning is emitted via ``_rich_warning``.

        .. note:: Callers must pre-normalize *managed_files* with
           forward-slash separators (see ``normalize_managed_files``).
        """
        if managed_files is None:
            return False
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
    def normalize_managed_files(managed_files: Optional[Set[str]]) -> Optional[Set[str]]:
        """Normalize path separators once for O(1) lookups."""
        if managed_files is None:
            return None
        return {p.replace("\\", "/") for p in managed_files}

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
        3. Resolves within *project_root*
        """
        if allowed_prefixes is None:
            allowed_prefixes = BaseIntegrator._get_integration_prefixes(targets=targets)
        if ".." in rel_path:
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

    # Backward-compat aliases mapping raw ``{prim}_{target}`` keys to
    # the bucket names that existing callers expect.  Shared between
    # ``partition_managed_files`` and ``partition_bucket_key`` so the
    # mapping is defined exactly once.
    _BUCKET_ALIASES: dict = {
        "prompts_copilot": "prompts",
        "agents_copilot": "agents_github",
        "commands_claude": "commands",
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
        managed_files: Set[str],
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
                effective_root = mapping.deploy_root or target.root_dir
                prefix = f"{effective_root}/{mapping.subdir}/" if mapping.subdir else f"{effective_root}/"
                if prim_name == "skills":
                    skill_prefixes.append(prefix)
                elif prim_name == "hooks":
                    hook_prefixes.append(prefix)
                else:
                    raw_key = f"{prim_name}_{target.name}"
                    bucket_key = BaseIntegrator._BUCKET_ALIASES.get(
                        raw_key, raw_key
                    )
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
        deleted_paths: List[Path],
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
        # Collect unique parents (skip stop_at itself)
        candidates: set = set()
        for p in deleted_paths:
            parent = p.parent
            while parent != stop_at and parent.resolve() != stop_resolved:
                candidates.add(parent)
                parent = parent.parent
        # Sort deepest-first for safe bottom-up removal
        for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

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

        link_pattern = re.compile(r'\]\(([^)]+)\)')
        original_links = set(link_pattern.findall(content))
        resolved_links = set(link_pattern.findall(resolved))
        return resolved, len(original_links - resolved_links)

    # ------------------------------------------------------------------
    # Sync (manifest-based file removal)
    # ------------------------------------------------------------------

    @staticmethod
    def sync_remove_files(
        project_root: Path,
        managed_files: Optional[Set[str]],
        prefix: str,
        legacy_glob_dir: Optional[Path] = None,
        legacy_glob_pattern: Optional[str] = None,
        targets=None,
    ) -> Dict[str, int]:
        """Remove APM-managed files matching *prefix* from *managed_files*.

        Falls back to a legacy glob when *managed_files* is ``None``.

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

        Returns:
            ``{"files_removed": int, "errors": int}``
        """
        stats: Dict[str, int] = {"files_removed": 0, "errors": 0}

        if managed_files is not None:
            for rel_path in managed_files:
                # managed_files is pre-normalized  -- no .replace() needed
                if not rel_path.startswith(prefix):
                    continue
                if not BaseIntegrator.validate_deploy_path(rel_path, project_root, targets=targets):
                    continue
                target = project_root / rel_path
                if target.exists():
                    try:
                        target.unlink()
                        stats["files_removed"] += 1
                    except Exception:
                        stats["errors"] += 1
        elif legacy_glob_dir and legacy_glob_pattern and legacy_glob_dir.exists():
            for f in legacy_glob_dir.glob(legacy_glob_pattern):
                try:
                    f.unlink()
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
        subdirs: Optional[List[str]] = None,
    ) -> List[Path]:
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
        results: List[Path] = []
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
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    results.append(f)

        return results
