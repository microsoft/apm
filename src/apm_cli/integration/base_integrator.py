"""Base integrator with shared collision detection and sync logic."""

import errno
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.compilation.link_resolver import UnifiedLinkResolver
from apm_cli.core.deployment_state import MaterializationResult
from apm_cli.primitives.discovery import discover_primitives
from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.console import _rich_warning
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within


def _managed_absolute_target_root(candidate: Path, targets: Any) -> Path | None:
    """Return the managed root for *candidate*, or ``None`` if unmanaged."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    source = targets
    if source is None:
        source = []
        for profile in KNOWN_TARGETS.values():
            if not profile.user_supported or profile.user_root_resolver is not None:
                continue
            scoped = profile.for_scope(user_scope=True)
            if scoped is not None:
                source.append(scoped)
    try:
        resolved = candidate.resolve()
        for target_profile in source:
            if target_profile is None:
                continue
            deploy_root = target_profile.managed_deploy_root
            if deploy_root is None:
                continue
            resolved_root = deploy_root.resolve()
            for mapping in target_profile.primitives.values():
                if not mapping.subdir:
                    continue
                primitive_root = (resolved_root / mapping.subdir).resolve()
                try:
                    contained = ensure_path_within(resolved, primitive_root)
                except PathTraversalError:
                    continue
                if contained != primitive_root:
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


class BaseIntegrator:
    """Shared infrastructure for file-level integrators.

    Subclasses only need to override the abstract hooks; the collision
    detection, sync removal, and link resolution logic is
    handled here.
    """

    # Deploy-mode for the shared adopt predicate (:meth:`_check_adopt_or_skip`).
    # ``False`` = byte-preserving identity (the safe default any future
    # byte-preserving integrator inherits). Integrators that deploy via
    # ``write_text_lf`` (instruction / command / agent) override this to
    # ``True`` so a CRLF-source file already deployed as LF is adopted rather
    # than churned. Making the mode an explicit, named class attribute keeps
    # it out of a hardcoded literal buried inside the predicate.
    _LF_NORMALIZED_DEPLOY = False

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
        CRLF->LF normalization). Format-transforming targets
        (``codex_agent``, ``cursor_rules``, ``claude_rules``,
        ``windsurf_rules``, ``gemini_command``, ...) won't match -- they
        keep the existing skip behavior. This means we never silently
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
        so secondary call sites in agent/prompt/hook integrators share a
        single predicate call instead of repeating the three-line block.

        ``lf_normalized_deploy`` is forwarded to
        :meth:`is_content_identical_to_source`: LF-normalizing deployers
        (agent, prompt) pass ``True``; byte-preserving deployers (hook,
        kiro-hook) keep the ``False`` default.

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

        Returns:
            ``(skip, adopted)`` -- when ``skip`` is ``True`` the caller must
            ``continue`` (or otherwise skip writing this file); ``adopted``
            is ``True`` only when the existing file already matched the
            deployed content and has been silently adopted.
        """
        if self.is_content_identical_to_source(
            target_path, source_file, lf_normalized_deploy=self._LF_NORMALIZED_DEPLOY
        ):
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
        3. Resolves within *project_root*, a configured absolute target root,
           or the cowork root for ``cowork://`` paths
        """
        from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

        if ".." in rel_path:
            return False

        candidate = Path(rel_path)
        if candidate.is_absolute():
            return _managed_absolute_target_root(candidate, targets) is not None

        if allowed_prefixes is None:
            allowed_prefixes = BaseIntegrator._get_integration_prefixes(targets=targets)

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
                prefix = (
                    f"{effective_root}/{mapping.subdir}/"
                    if mapping.subdir
                    else f"{effective_root}/"
                )
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
            while parent not in (parent.parent, stop_at) and parent.resolve() != cleanup_boundary:
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

    @staticmethod
    def _is_root_local_package(package_info, project_root: Path) -> bool:
        """Return True when *package_info* represents the project's own
        synthetic ``_local`` package (install_path == project_root).

        Used to scope discovery to ``.apm/`` and ``.github/`` instead of
        walking the entire project tree -- see issue #1507.
        """
        try:
            return Path(package_info.install_path).resolve() == Path(project_root).resolve()
        except (OSError, RuntimeError):
            return False

    def init_link_resolver(self, package_info, project_root: Path) -> None:
        """Initialise and register the link resolver for a package."""
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
            if install_path.resolve() == home_root.resolve():
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
            if install_path.resolve() != home_root.resolve() and install_path.is_dir():
                if narrowed_local or (len(scan_roots) == 1 and scan_roots[0] == install_path):
                    self.link_resolver.package_root = Path(install_path)
                    self.link_resolver.deployment_package_root = Path(
                        getattr(package_info, "deployment_package_root", None) or install_path
                    )
        except Exception:
            self.link_resolver = None

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
    ) -> dict[str, int]:
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
            logger: Optional logger for diagnostic messages.

        Returns:
            ``{"files_removed": int, "errors": int}``
        """
        stats: dict[str, int] = {"files_removed": 0, "errors": 0}

        if managed_files is not None:
            # Lazy-resolve cowork root at most once per invocation.
            _cowork_root_resolved: bool = False
            _cowork_root_cached: Path | None = None
            _cowork_orphans_skipped: int = 0

            for rel_path in managed_files:
                # managed_files is pre-normalized  -- no .replace() needed
                if not rel_path.startswith(prefix):
                    continue
                if not BaseIntegrator.validate_deploy_path(rel_path, project_root, targets=targets):
                    continue
                # Resolve cowork:// paths to absolute before filesystem ops.
                from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

                if rel_path.startswith(COWORK_URI_SCHEME):
                    try:
                        if not _cowork_root_resolved:
                            from apm_cli.integration.copilot_cowork_paths import (
                                resolve_copilot_cowork_skills_dir,
                            )

                            _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                            _cowork_root_resolved = True
                        if _cowork_root_cached is None:
                            _cowork_orphans_skipped += 1
                            continue
                        from apm_cli.integration.copilot_cowork_paths import (
                            from_lockfile_path,
                        )

                        target = from_lockfile_path(rel_path, _cowork_root_cached)
                    except Exception:  # noqa: S112
                        continue
                else:
                    target = project_root / rel_path
                if target.exists():
                    try:
                        target.unlink()
                        stats["files_removed"] += 1
                    except Exception:
                        stats["errors"] += 1

            # Emit a one-time warning when cowork orphans were skipped.
            if _cowork_orphans_skipped > 0:
                _orphan_msg = (
                    f"Cowork: skipping {_cowork_orphans_skipped} orphaned lockfile "
                    f"{'entry' if _cowork_orphans_skipped == 1 else 'entries'}"
                    " -- OneDrive path not detected.\n"
                    "Run: apm config set copilot-cowork-skills-dir <path>  "
                    "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                    "to clean up these entries on the next install/uninstall."
                )
                if logger:
                    logger.warning(_orphan_msg, symbol="warning")
                else:
                    _rich_warning(_orphan_msg, symbol="warning")
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
                resolved = f.resolve()
                # Defense-in-depth containment guard: skip files whose
                # resolved path escapes the package root.  Belt-and-
                # suspenders against any future regression in the
                # is_symlink() check above.  See path_security.ensure_path_within
                # for the canonical predicate; we inline the check here to stay
                # loop-fast and avoid raising on every malicious entry.
                try:
                    pkg_resolved = package_path.resolve()
                    if not resolved.is_relative_to(pkg_resolved):
                        continue
                except (ValueError, OSError):
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    results.append(f)

        return results
