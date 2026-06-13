"""Hook integration functionality for APM packages.

Integrates hook JSON files and their referenced scripts during package
installation. Supports VSCode Copilot (.github/hooks/), Claude Code
(.claude/settings.json), and Cursor (.cursor/hooks.json) targets.

Hook JSON format (Claude Code  -- nested matcher groups):
    {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "./scripts/validate.sh", "timeout": 10}
                    ]
                }
            ]
        }
    }

Hook JSON format (GitHub Copilot  -- flat arrays with bash/powershell keys):
    {
        "version": 1,
        "hooks": {
            "preToolUse": [
                {"type": "command", "bash": "./scripts/validate.sh", "timeoutSec": 10}
            ]
        }
    }

Hook JSON format (Cursor  -- flat arrays with command key):
    {
        "hooks": {
            "afterFileEdit": [
                {"command": "./hooks/format.sh"}
            ]
        }
    }

Script path handling:
    - ${CLAUDE_PLUGIN_ROOT}/path, ${CURSOR_PLUGIN_ROOT}/path, ${PLUGIN_ROOT}/path
      -> resolved relative to package root, rewritten for target
    - ./path -> relative path, resolved from hook file's parent directory, rewritten for target
    - System commands (no path separators) -> passed through unchanged
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.console import _rich_warning
from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

from .hook_merge import (
    _clean_apm_entries_from_json,
    _dependency_hook_sources,
    _get_hook_source_marker,
    _get_package_name,
    _is_root_local_package,
    _load_merged_config_and_sidecar,
    _merge_hook_file_entries,
    _sync_claude_hooks_settings,
    _warn_empty_hook_file,
    _write_merged_config,
)
from .hook_merge import (
    _parse_hook_json as _parse_hook_json_impl,
)
from .hook_transforms import (
    _APM_HOOKS_SIDECAR,
    _HOOK_EVENT_MAP,
    _MERGE_HOOK_TARGETS,
    _emit_hook_event_diagnostics,
    _filter_hook_files_for_target,
    _MergeHookConfig,
    _rewrite_command_for_target,
    _rewrite_hooks_data,
)

# ---------------------------------------------------------------------------
# Re-exports: symbols imported by external callers / tests from this module.
# The ``X as X`` form marks them as intentional public re-exports (PEP 484).
# ---------------------------------------------------------------------------
from .hook_transforms import _HOOK_EVENT_EXPECTED_CASING as _HOOK_EVENT_EXPECTED_CASING
from .hook_transforms import (
    _build_display_payload as _build_display_payload_impl,
)
from .hook_transforms import _copilot_keys_to_gemini as _copilot_keys_to_gemini
from .hook_transforms import _detect_event_casing as _detect_event_casing
from .hook_transforms import (
    _iter_hook_entries as _iter_hook_entries_impl,
)
from .hook_transforms import _reinject_apm_source_from_sidecar as _reinject_apm_source_from_sidecar
from .hook_transforms import (
    _summarize_command as _summarize_command_impl,
)
from .hook_transforms import _to_gemini_hook_entries as _to_gemini_hook_entries

_log = logging.getLogger(__name__)


# DEPRECATED -- use IntegrationResult directly for new code.
# Backward-compatible shim: accepts hooks_integrated= kwarg and
# exposes a hooks_integrated property for consumers of the old API.
class HookIntegrationResult(IntegrationResult):
    """Backward-compatible wrapper around IntegrationResult."""

    def __init__(self, *args, hooks_integrated=None, **kwargs):
        if hooks_integrated is not None:
            kwargs.setdefault("files_integrated", hooks_integrated)
            kwargs.setdefault("files_updated", 0)
            kwargs.setdefault("files_skipped", 0)
            kwargs.setdefault("target_paths", [])
        super().__init__(*args, **kwargs)

    @property
    def hooks_integrated(self):
        """Alias for files_integrated (backward compat)."""
        return self.files_integrated


class HookIntegrator(BaseIntegrator):
    """Handles integration of APM package hooks into target locations.

    Discovers hook JSON files and their referenced scripts from packages,
    then installs them to the appropriate target location:
    - VSCode: .github/hooks/<pkg>-<name>.json + .github/hooks/scripts/<pkg>/
    - Claude: Merged into .claude/settings.json hooks key + .claude/hooks/<pkg>/
    - Cursor: Merged into .cursor/hooks.json hooks key + .cursor/hooks/<pkg>/
    """

    # ---------------------------------------------------------------------------
    # Static wrappers -- keep callable via HookIntegrator.xxx() for back-compat
    # ---------------------------------------------------------------------------

    @staticmethod
    def _is_root_local_package(package_info, project_root: Path | None) -> bool:
        """Return True when package_info represents the project's own .apm content."""
        return _is_root_local_package(package_info, project_root)

    @staticmethod
    def _dependency_hook_sources(project_root: Path) -> set[str]:
        """Return source markers that correspond to installed dependency dirs."""
        return _dependency_hook_sources(project_root)

    @staticmethod
    def _clean_apm_entries_from_json(json_path: Path, stats: dict[str, int]) -> None:
        """Remove APM-tagged entries from a hooks JSON file."""
        _clean_apm_entries_from_json(json_path, stats)

    def _rewrite_command_for_target(
        self,
        command: str,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Path | None = None,
        root_dir: str | None = None,
        deploy_root: Path | None = None,
    ) -> tuple[str, list[tuple[Path, str]]]:
        """Rewrite a hook command to use installed script paths."""
        return _rewrite_command_for_target(
            command,
            package_path,
            package_name,
            target,
            hook_file_dir=hook_file_dir,
            root_dir=root_dir,
            deploy_root=deploy_root,
            _warn=_rich_warning,
        )

    def _rewrite_hooks_data(
        self,
        data: dict,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Path | None = None,
        root_dir: str | None = None,
        deploy_root: Path | None = None,
    ) -> tuple[dict, list[tuple[Path, str]]]:
        """Rewrite all command paths in a hooks JSON structure."""
        return _rewrite_hooks_data(
            data,
            package_path,
            package_name,
            target,
            hook_file_dir=hook_file_dir,
            root_dir=root_dir,
            deploy_root=deploy_root,
        )

    # ---------------------------------------------------------------------------
    # Hook file discovery
    # ---------------------------------------------------------------------------

    @staticmethod
    def _iter_hook_entries(payload: dict) -> list[tuple[str, dict]]:
        """Flatten hook payloads into (event_name, entry_dict) pairs."""
        return _iter_hook_entries_impl(payload)

    @staticmethod
    def _summarize_command(entry: dict) -> str:
        """Return a human-readable summary for a single hook command entry."""
        return _summarize_command_impl(entry)

    def _build_display_payload(
        self,
        target_label: str,
        output_path: str,
        source_hook_file: Any,
        rewritten: dict,
    ) -> dict:
        """Build CLI display metadata for an integrated hook file."""
        return _build_display_payload_impl(target_label, output_path, source_hook_file, rewritten)

    def find_hook_files(self, package_path: Path) -> list[Path]:
        """Find all hook JSON files in a package.

        Searches in:
        - .apm/hooks/ subdirectory (APM convention)
        - hooks/ subdirectory (Claude-native convention)

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to hook JSON files
        """
        hook_files = []
        seen = set()

        # Search in .apm/hooks/ (APM convention)
        apm_hooks = package_path / ".apm" / "hooks"
        if apm_hooks.exists():
            for f in sorted(apm_hooks.glob("*.json")):
                if f.is_symlink():
                    continue
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    hook_files.append(f)

        # Search in hooks/ (Claude-native convention)
        hooks_dir = package_path / "hooks"
        if hooks_dir.exists():
            for f in sorted(hooks_dir.glob("*.json")):
                if f.is_symlink():
                    continue
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    hook_files.append(f)

        return hook_files

    def _parse_hook_json(self, hook_file: Path) -> dict | None:
        """Parse a hook JSON file and return the data dict.

        Accepts both the wrapped format and the naked Claude-settings
        hooks-slice format.  See ``hook_merge._parse_hook_json`` for details.
        """
        return _parse_hook_json_impl(hook_file)

    # ---------------------------------------------------------------------------
    # Script copy helper
    # ---------------------------------------------------------------------------

    def _copy_hook_scripts(
        self,
        scripts: list[tuple[Path, str]],
        project_root: Path,
        target_paths: list[Path],
        managed_files: set | None,
        force: bool,
        diagnostics,
    ) -> tuple[int, int]:
        """Copy referenced hook scripts to their target locations.

        Returns:
            (scripts_copied, scripts_adopted) counts.
        """
        scripts_copied = 0
        scripts_adopted = 0
        for source_file, target_rel in scripts:
            target_script = project_root / target_rel
            ensure_path_within(target_script, project_root)
            if self.try_adopt_identical(target_script, source_file, target_paths):
                scripts_adopted += 1
                continue
            if self.check_collision(
                target_script, target_rel, managed_files, force, diagnostics=diagnostics
            ):
                continue
            target_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_script)
            scripts_copied += 1
            target_paths.append(target_script)
        return scripts_copied, scripts_adopted

    # ---------------------------------------------------------------------------
    # Copilot (individual-file) integration
    # ---------------------------------------------------------------------------

    def integrate_package_hooks(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        target=None,
    ) -> HookIntegrationResult:
        """Integrate hooks from a package into hooks dir (Copilot target).

        Deploys hook JSON files with clean filenames and copies referenced
        script files. Skips user-authored files unless force=True.

        Args:
            package_info: PackageInfo with package metadata and install path
            project_root: Root directory of the project
            force: If True, overwrite user-authored files on collision
            managed_files: Set of relative paths known to be APM-managed
            target: Optional TargetProfile for scope-resolved root_dir

        Returns:
            HookIntegrationResult: Results of the integration operation
        """
        hook_files = self.find_hook_files(package_info.install_path)
        hook_files = _filter_hook_files_for_target(hook_files, "copilot")

        if not hook_files:
            return HookIntegrationResult(
                files_integrated=0,
                files_updated=0,
                files_skipped=0,
                target_paths=[],
            )

        root_dir = target.root_dir if target else ".github"
        hooks_dir = project_root / root_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        package_name = _get_package_name(package_info, project_root)
        hooks_integrated = 0
        scripts_copied = 0
        scripts_adopted = 0
        target_paths: list[Path] = []
        display_payloads: list = []

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            # Rewrite script paths for VSCode target
            rewritten, scripts = _rewrite_hooks_data(
                data,
                package_info.install_path,
                package_name,
                "vscode",
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
            )

            # Generate target filename (clean, no -apm suffix)
            stem = hook_file.stem
            target_filename = f"{package_name}-{stem}.json"
            target_path = hooks_dir / target_filename
            rel_path = portable_relpath(target_path, project_root)

            if self.check_collision(
                target_path, rel_path, managed_files, force, diagnostics=diagnostics
            ):
                continue

            _emit_hook_event_diagnostics(list(rewritten.get("hooks", {}).keys()), "copilot", {})

            # Write rewritten JSON
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(rewritten, f, indent=2)
                f.write("\n")

            hooks_integrated += 1
            target_paths.append(target_path)
            display_payloads.append(
                self._build_display_payload(
                    f"{root_dir}/hooks/",
                    target_filename,
                    hook_file,
                    rewritten,
                )
            )

            sc, sa = self._copy_hook_scripts(
                scripts, project_root, target_paths, managed_files, force, diagnostics
            )
            scripts_copied += sc
            scripts_adopted += sa

        return HookIntegrationResult(
            files_integrated=hooks_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            scripts_copied=scripts_copied,
            files_adopted=scripts_adopted,
            display_payloads=display_payloads,
        )

    # ------------------------------------------------------------------
    # Shared JSON-merge implementation for Claude / Cursor / Codex
    # ------------------------------------------------------------------

    def _integrate_merged_hooks(
        self,
        config: "_MergeHookConfig",
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        target=None,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        """Integrate hooks by merging into a target-specific JSON config.

        This is the shared implementation for Claude, Cursor, and Codex
        targets that merge hook entries into a single JSON file (as
        opposed to Copilot which uses individual JSON files).
        """
        _empty = HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

        root_dir = target.root_dir if target else f".{config.target_key}"
        target_dir = project_root / root_dir

        # Opt-in check: some targets only deploy when their dir exists
        if config.require_dir and not target_dir.exists():
            return _empty

        # Absolutize hook commands only for user-scope deploys.
        _deploy_root_for_rewrite = project_root if user_scope else None

        hook_files = self.find_hook_files(package_info.install_path)
        hook_files = _filter_hook_files_for_target(hook_files, config.target_key)
        if not hook_files:
            return _empty

        package_name = _get_package_name(package_info, project_root)
        source_marker = _get_hook_source_marker(package_info, project_root, package_name)
        heal_stale = _is_root_local_package(package_info, project_root)
        dep_sources = _dependency_hook_sources(project_root) if heal_stale else set()

        hooks_integrated = 0
        scripts_copied = 0
        scripts_adopted = 0
        target_paths: list[Path] = []
        display_payloads: list = []
        # Per-file display metadata is captured during the merge loop but
        # the payloads are BUILT after the JSON config is finalized (Gemini
        # transform applied, schema-strict _apm_source stripped) so that
        # rendered_json reflects the actual on-disk/executed content.
        pending_display: list = []
        cleared_events: set = set()

        json_path = target_dir / config.config_filename
        sidecar_path = target_dir / _APM_HOOKS_SIDECAR
        json_config = _load_merged_config_and_sidecar(json_path, sidecar_path, config.schema_strict)

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            rewritten, scripts = _rewrite_hooks_data(
                data,
                package_info.install_path,
                package_name,
                config.target_key,
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
                deploy_root=_deploy_root_for_rewrite,
            )

            hooks = rewritten.get("hooks", {})
            event_map = _HOOK_EVENT_MAP.get(config.target_key, {})
            _emit_hook_event_diagnostics(list(hooks.keys()), config.target_key, event_map)

            file_event_entries: dict = {}
            appended = _merge_hook_file_entries(
                json_config,
                hooks,
                config.target_key,
                event_map,
                source_marker,
                cleared_events,
                heal_stale_root_source=heal_stale,
                dependency_sources=dep_sources,
                capture_entries=file_event_entries,
            )

            if appended:
                hooks_integrated += 1
                pending_display.append(
                    (
                        config.config_filename,
                        config.config_filename,
                        hook_file,
                        file_event_entries,
                    )
                )
            else:
                _warn_empty_hook_file(hook_file, config.target_key)

            copied, adopted = self._copy_hook_scripts(
                scripts, project_root, target_paths, managed_files, force, diagnostics
            )
            scripts_copied += copied
            scripts_adopted += adopted

        json_path.parent.mkdir(parents=True, exist_ok=True)
        _write_merged_config(json_path, sidecar_path, json_config, config.schema_strict)

        # Build display payloads from the finalized entry objects (post
        # Gemini transform and post schema-strict _apm_source strip) so the
        # CLI summary and rendered_json faithfully reflect what is written
        # to disk and executed -- not the pre-transform per-file data.
        for _label, _path, _hook_file, _file_event_entries in pending_display:
            display_payloads.append(
                self._build_display_payload(
                    _label,
                    _path,
                    _hook_file,
                    {"hooks": _file_event_entries},
                )
            )

        return HookIntegrationResult(
            files_integrated=hooks_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            scripts_copied=scripts_copied,
            files_adopted=scripts_adopted,
            display_payloads=display_payloads,
        )

    def integrate_package_hooks_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        """Integrate hooks into .claude/settings.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    def integrate_package_hooks_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        """Integrate hooks into .cursor/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["cursor"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    def integrate_package_hooks_codex(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        """Integrate hooks into .codex/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["codex"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    # ------------------------------------------------------------------
    # Target-driven API
    # ------------------------------------------------------------------

    def integrate_hooks_for_target(
        self,
        target,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        scope=None,
        user_scope: bool = False,
    ) -> "HookIntegrationResult":
        """Integrate hooks for a single *target*.

        Copilot uses individual JSON files (genuinely different pattern).
        All other merge-based targets are dispatched via the
        ``_MERGE_HOOK_TARGETS`` registry.

        ``user_scope`` controls whether merged-hook ``command`` paths are
        rewritten to absolute paths (required when deploying to
        ``~/.claude/settings.json`` -- see #1310 / #1354) or left
        repo-relative so checked-in project-scope configs stay portable
        across clones, contributors, and CI runners (#1394).
        """
        if target.name == "copilot":
            return self.integrate_package_hooks(
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
            )

        if target.name == "kiro":
            from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

            return integrate_kiro_hooks(
                self,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                user_scope=user_scope,
            )

        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is not None:
            return self._integrate_merged_hooks(
                config,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                user_scope=user_scope,
            )

        return HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
        targets=None,
    ) -> dict:
        """Remove APM-managed hook files.

        Uses *managed_files* (relative paths) to surgically remove only
        APM-tracked files.  Falls back to legacy ``*-apm.json`` glob when
        *managed_files* is ``None``.

        **Never** calls ``shutil.rmtree``.

        Also cleans APM entries from merged-hook JSON files via the
        ``_apm_source`` marker.
        """
        from .targets import KNOWN_TARGETS

        stats: dict[str, int] = {"files_removed": 0, "errors": 0}

        # Derive hook prefixes dynamically from targets
        source = targets if targets is not None else list(KNOWN_TARGETS.values())
        hook_prefixes = []
        for t in source:
            if t.supports("hooks"):
                sm = t.primitives["hooks"]
                effective_root = sm.deploy_root or t.root_dir
                hook_prefixes.append(f"{effective_root}/hooks/")
        hook_prefix_tuple = tuple(hook_prefixes)

        if managed_files is not None:
            # Manifest-based removal -- only remove tracked files
            deleted: list = []
            for rel_path in managed_files:
                normalized = rel_path.replace("\\", "/")
                if not normalized.startswith(hook_prefix_tuple):
                    continue
                if ".." in rel_path:
                    continue
                target_file = project_root / rel_path
                if target_file.exists() and target_file.is_file():
                    try:
                        target_file.unlink()
                        stats["files_removed"] += 1
                        deleted.append(target_file)
                    except Exception:
                        stats["errors"] += 1
            # Batch parent cleanup -- single bottom-up pass
            self.cleanup_empty_parents(deleted, stop_at=project_root)
        else:
            # Legacy fallback  -- glob for old -apm suffix files
            hooks_dir = project_root / ".github" / "hooks"
            if hooks_dir.exists():
                for hook_file in hooks_dir.glob("*-apm.json"):
                    try:
                        hook_file.unlink()
                        stats["files_removed"] += 1
                    except Exception:
                        stats["errors"] += 1

        # Clean APM entries from merged-hook JSON configs (uses _apm_source marker)
        for t in source:
            config = _MERGE_HOOK_TARGETS.get(t.name)
            if config is not None:
                json_path = project_root / t.root_dir / config.config_filename
                if t.name == "claude":
                    _sync_claude_hooks_settings(json_path, stats)
                else:
                    _clean_apm_entries_from_json(json_path, stats)

        return stats
