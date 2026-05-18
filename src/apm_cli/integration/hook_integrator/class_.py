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
from dataclasses import dataclass
from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult

from ._opts import (
    _HOOK_EVENT_MAP,
    _HOOK_FILE_TARGET_SUFFIXES,
    _MERGE_HOOK_TARGETS,
    HookIntegrateOpts,
    HookIntegrationResult,
    HookRewriteOpts,
    _copilot_keys_to_gemini,
    _filter_hook_files_for_target,
    _MergeHookConfig,
    _to_gemini_hook_entries,
)

_log = logging.getLogger(__name__)


class HookIntegrator(BaseIntegrator):
    """Handles integration of APM package hooks into target locations.

    Discovers hook JSON files and their referenced scripts from packages,
    then installs them to the appropriate target location:
    - VSCode: .github/hooks/<pkg>-<name>.json + .github/hooks/scripts/<pkg>/
    - Claude: Merged into .claude/settings.json hooks key + .claude/hooks/<pkg>/
    - Cursor: Merged into .cursor/hooks.json hooks key + .cursor/hooks/<pkg>/
    """

    # Superset of all known script-path keys across supported hook specs.
    # Every call site in _rewrite_hooks_data() iterates over this tuple,
    # so a single addition here propagates everywhere.
    #
    #   "command":    Claude Code (primary), VS Code (default/cross-platform), Cursor
    #   "bash":       GitHub Copilot Agent cloud/CLI
    #   "powershell": GitHub Copilot Agent cloud/CLI
    #   "windows":    VS Code (OS-specific override)
    #   "linux":      VS Code (OS-specific override)
    #   "osx":        VS Code (OS-specific override)
    #
    # Refs:
    #   GH Copilot Agent: https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks
    #   VS Code:          https://code.visualstudio.com/docs/copilot/customization/hooks
    #   Claude Code:      https://code.claude.com/docs/en/hooks
    HOOK_COMMAND_KEYS: tuple[str, ...] = (
        "command",
        "bash",
        "powershell",
        "windows",
        "linux",
        "osx",
    )

    def find_hook_files(self, package_path: Path) -> list[Path]:
        return _filter_files.find_hook_files(self, package_path)

    def _parse_hook_json(self, hook_file: Path) -> dict | None:
        """Parse a hook JSON file and return the data dict.

        Args:
            hook_file: Path to the hook JSON file

        Returns:
            Optional[Dict]: Parsed JSON dict, or None if invalid
        """
        try:
            with open(hook_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def _rewrite_command_for_target(
        self,
        command: str,
        opts_or_package_path=None,
        package_name: str | None = None,
        target: str | None = None,
        **legacy_kwargs,
    ) -> tuple[str, list[tuple[Path, str]]]:
        if isinstance(opts_or_package_path, HookRewriteOpts):
            opts = opts_or_package_path
        else:
            opts = HookRewriteOpts(
                package_path=opts_or_package_path,
                package_name=package_name or "",
                target=target or "",
                hook_file_dir=legacy_kwargs.get("hook_file_dir"),
                root_dir=legacy_kwargs.get("root_dir"),
                deploy_root=legacy_kwargs.get("deploy_root"),
            )
        return _gemini_translate._rewrite_command_for_target(self, command, opts)

    def _rewrite_hooks_data(
        self,
        data: dict,
        opts_or_package_path=None,
        package_name: str | None = None,
        target: str | None = None,
        **legacy_kwargs,
    ) -> tuple[dict, list[tuple[Path, str]]]:
        if isinstance(opts_or_package_path, HookRewriteOpts):
            opts = opts_or_package_path
        else:
            opts = HookRewriteOpts(
                package_path=opts_or_package_path,
                package_name=package_name or "",
                target=target or "",
                hook_file_dir=legacy_kwargs.get("hook_file_dir"),
                root_dir=legacy_kwargs.get("root_dir"),
                deploy_root=legacy_kwargs.get("deploy_root"),
            )
        return _gemini_translate._rewrite_hooks_data(self, data, opts)

    def _get_package_name(self, package_info) -> str:
        """Get a short package name for use in file/directory naming.

        Args:
            package_info: PackageInfo object

        Returns:
            str: Package name derived from install path
        """
        return package_info.install_path.name

    def integrate_package_hooks(
        self,
        package_info,
        project_root: Path,
        opts: HookIntegrateOpts | None = None,
        **legacy_kwargs,
    ) -> HookIntegrationResult:
        if opts is None and legacy_kwargs:
            opts = HookIntegrateOpts(
                force=legacy_kwargs.get("force", False),
                managed_files=legacy_kwargs.get("managed_files"),
                diagnostics=legacy_kwargs.get("diagnostics"),
                target=legacy_kwargs.get("target"),
            )
        return _merge_config.integrate_package_hooks(self, package_info, project_root, opts)

    # ------------------------------------------------------------------
    # Shared JSON-merge implementation for Claude / Cursor / Codex
    # ------------------------------------------------------------------

    def _integrate_merged_hooks(
        self,
        config: "_MergeHookConfig",
        package_info,
        project_root: Path,
        opts: HookIntegrateOpts | None = None,
    ) -> HookIntegrationResult:
        return _merge_config._integrate_merged_hooks(
            self,
            config,
            package_info,
            project_root,
            opts=opts,
        )

    # ------------------------------------------------------------------
    # DEPRECATED per-target methods -- delegate to _integrate_merged_hooks
    # ------------------------------------------------------------------

    def integrate_package_hooks_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .claude/settings.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["claude"],
            package_info,
            project_root,
            HookIntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    def integrate_package_hooks_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .cursor/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["cursor"],
            package_info,
            project_root,
            HookIntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    def integrate_package_hooks_codex(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .codex/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["codex"],
            package_info,
            project_root,
            HookIntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # ------------------------------------------------------------------
    # Target-driven API
    # ------------------------------------------------------------------

    def integrate_hooks_for_target(
        self,
        target,
        package_info,
        project_root: Path,
        opts: HookIntegrateOpts | None = None,
    ) -> "HookIntegrationResult":
        return _merge_config.integrate_hooks_for_target(
            self,
            target,
            package_info,
            project_root,
            opts=opts,
        )

    def sync_integration(
        self, apm_package, project_root: Path, managed_files: set | None = None, targets=None
    ) -> dict:
        return _filter_files.sync_integration(
            self, apm_package, project_root, managed_files, targets
        )

    @staticmethod
    def _clean_apm_entries_from_json(json_path: Path, stats: dict[str, int]) -> None:
        return _filter_files._clean_apm_entries_from_json(json_path, stats)


from . import filter_files as _filter_files
from . import gemini_translate as _gemini_translate
from . import merge_config as _merge_config
