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
    - ${CLAUDE_PLUGIN_ROOT}/path -> resolved relative to package root, rewritten for target
    - ./path -> relative path, resolved from hook file's parent directory, rewritten for target
    - System commands (no path separators) -> passed through unchanged
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.paths import portable_relpath

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


@dataclass(frozen=True)
class _MergeHookConfig:
    """Configuration for targets that merge hooks into a single JSON file."""

    config_filename: str    # e.g. "settings.json" or "hooks.json"
    target_key: str         # target name passed to _rewrite_hooks_data
    require_dir: bool       # True = skip if target dir doesn't exist


# Per-target hook event name mapping.  Packages are authored with
# Copilot (camelCase) or Claude (PascalCase) names; targets that use
# different conventions get their events renamed during merge.
_HOOK_EVENT_MAP: dict[str, dict[str, str]] = {
    "gemini": {
        # Copilot / Claude -> Gemini
        "PreToolUse": "BeforeTool",
        "preToolUse": "BeforeTool",
        "PostToolUse": "AfterTool",
        "postToolUse": "AfterTool",
        "Stop": "SessionEnd",
    },
}

def _to_gemini_hook_entries(entries: list) -> list:
    """Transform hook entries into Gemini CLI format.

    Gemini requires ``{"hooks": [...]}`` nesting, uses ``command`` (not
    ``bash``), and ``timeout`` in milliseconds (not ``timeoutSec`` in
    seconds).  Entries already in Claude/Gemini nested format are left
    unchanged.
    """
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        # Already nested (Claude / Gemini format) -- just fix inner keys
        if "hooks" in entry and isinstance(entry["hooks"], list):
            for hook in entry["hooks"]:
                _copilot_keys_to_gemini(hook)
            result.append(entry)
            continue
        # Flat Copilot entry -- wrap in nested format
        inner = dict(entry)
        _copilot_keys_to_gemini(inner)
        # Pull _apm_source to outer level (set later, but keep if present)
        apm_source = inner.pop("_apm_source", None)
        outer: dict = {"hooks": [inner]}
        if apm_source:
            outer["_apm_source"] = apm_source
        result.append(outer)
    return result


def _copilot_keys_to_gemini(hook: dict) -> None:
    """Rename Copilot hook keys to Gemini equivalents in-place."""
    # bash / powershell -> command
    if "command" not in hook:
        for key in ("bash", "powershell", "windows"):
            if key in hook:
                hook["command"] = hook.pop(key)
                break
    # timeoutSec (seconds) -> timeout (milliseconds)
    if "timeoutSec" in hook:
        hook["timeout"] = hook.pop("timeoutSec") * 1000


_MERGE_HOOK_TARGETS: dict[str, _MergeHookConfig] = {
    "claude": _MergeHookConfig(
        config_filename="settings.json",
        target_key="claude",
        require_dir=False,
    ),
    "cursor": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="cursor",
        require_dir=True,
    ),
    "codex": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="codex",
        require_dir=True,
    ),
    "gemini": _MergeHookConfig(
        config_filename="settings.json",
        target_key="gemini",
        require_dir=True,
    ),
}


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
    HOOK_COMMAND_KEYS: Tuple[str, ...] = ("command", "bash", "powershell", "windows", "linux", "osx")

    def find_hook_files(self, package_path: Path) -> List[Path]:
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

    def _parse_hook_json(self, hook_file: Path) -> Optional[Dict]:
        """Parse a hook JSON file and return the data dict.

        Args:
            hook_file: Path to the hook JSON file

        Returns:
            Optional[Dict]: Parsed JSON dict, or None if invalid
        """
        try:
            with open(hook_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def _rewrite_command_for_target(
        self,
        command: str,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Optional[Path] = None,
        root_dir: Optional[str] = None,
    ) -> Tuple[str, List[Tuple[Path, str]]]:
        """Rewrite a hook command to use installed script paths.

        Handles:
        - ${CLAUDE_PLUGIN_ROOT}/path references (resolved from package root)
        - ./path relative references (resolved from hook file's parent directory)
        - Windows backslash variants of both (.\\ and ${CLAUDE_PLUGIN_ROOT}\\)

        Args:
            command: Original command string
            package_path: Root path of the source package
            package_name: Name used for the scripts subdirectory
            target: "vscode" or "claude"
            hook_file_dir: Directory containing the hook JSON file (for ./path resolution)
            root_dir: Override root directory (e.g. ".copilot" for user scope)

        Returns:
            Tuple of (rewritten_command, list of (source_file, relative_target_path))
        """
        scripts_to_copy = []
        new_command = command

        if target == "vscode":
            base_root = root_dir or ".github"
            scripts_base = f"{base_root}/hooks/scripts/{package_name}"
        elif target == "cursor":
            base_root = root_dir or ".cursor"
            scripts_base = f"{base_root}/hooks/{package_name}"
        elif target == "codex":
            base_root = root_dir or ".codex"
            scripts_base = f"{base_root}/hooks/{package_name}"
        else:
            base_root = root_dir or ".claude"
            scripts_base = f"{base_root}/hooks/{package_name}"

        # Handle ${CLAUDE_PLUGIN_ROOT} references (always relative to package root)
        # Match both forward-slash and backslash separators (Windows hook JSON
        # may use backslashes: ${CLAUDE_PLUGIN_ROOT}\scripts\scan.ps1)
        plugin_root_pattern = r'\$\{CLAUDE_PLUGIN_ROOT\}([\\/][^\s]+)'
        for match in re.finditer(plugin_root_pattern, command):
            full_var = match.group(0)
            # Normalize backslashes to forward slashes before Path construction
            # (on Unix, Path treats backslashes as literal filename chars)
            rel_path = match.group(1).replace('\\', '/').lstrip('/')

            source_file = (package_path / rel_path).resolve()
            # Reject path traversal outside the package directory
            if not source_file.is_relative_to(package_path.resolve()):
                continue
            if source_file.exists() and source_file.is_file():
                target_rel = f"{scripts_base}/{rel_path}"
                scripts_to_copy.append((source_file, target_rel))
                new_command = new_command.replace(full_var, target_rel)

        # Handle relative ./path and .\path references (safe to run after
        # ${CLAUDE_PLUGIN_ROOT} substitution since replacements produce paths
        # like ".github/..." not "./" or ".\")
        # Match both forward-slash and backslash separators (Windows hook JSON
        # may use backslashes: .\scripts\scan.ps1)
        # Resolve from hook file's directory if available, else fall back to package root
        resolve_base = hook_file_dir if hook_file_dir else package_path
        rel_pattern = r'(\.[\\/][^\s]+)'
        for match in re.finditer(rel_pattern, new_command):
            rel_ref = match.group(1)
            # Normalize to forward slashes for path resolution
            rel_path = rel_ref[2:].replace('\\', '/')

            source_file = (resolve_base / rel_path).resolve()
            # Reject path traversal outside the package directory
            if not source_file.is_relative_to(package_path.resolve()):
                continue
            if source_file.exists() and source_file.is_file():
                target_rel = f"{scripts_base}/{rel_path}"
                scripts_to_copy.append((source_file, target_rel))
                new_command = new_command.replace(rel_ref, target_rel)

        return new_command, scripts_to_copy

    def _rewrite_hooks_data(
        self,
        data: Dict,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Optional[Path] = None,
        root_dir: Optional[str] = None,
    ) -> Tuple[Dict, List[Tuple[Path, str]]]:
        """Rewrite all command paths in a hooks JSON structure.

        Creates a deep copy and rewrites command paths for the target platform.

        Args:
            data: Parsed hook JSON data
            package_path: Root path of the source package
            package_name: Name for scripts subdirectory
            target: "vscode" or "claude"
            hook_file_dir: Directory containing the hook JSON file (for ./path resolution)
            root_dir: Override root directory (e.g. ".copilot" for user scope)

        Returns:
            Tuple of (rewritten_data_copy, list of (source_file, target_rel_path))
        """
        import copy
        rewritten = copy.deepcopy(data)
        all_scripts: List[Tuple[Path, str]] = []

        hooks = rewritten.get("hooks", {})
        for event_name, matchers in hooks.items():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    continue
                # Rewrite script paths in the matcher dict itself
                # (GitHub Copilot flat format: bash/powershell/windows keys at this level)
                for key in self.HOOK_COMMAND_KEYS:
                    if key in matcher:
                        new_cmd, scripts = self._rewrite_command_for_target(
                            matcher[key], package_path, package_name, target,
                            hook_file_dir=hook_file_dir,
                            root_dir=root_dir,
                        )
                        if scripts:
                            _log.debug(
                                "Hook %s/%s: rewrote '%s' key (%d script(s))",
                                package_name, event_name, key, len(scripts),
                            )
                        matcher[key] = new_cmd
                        all_scripts.extend(scripts)

                # Rewrite script paths in nested hooks array
                # (Claude format: matcher groups with inner hooks array)
                for hook in matcher.get("hooks", []):
                    if not isinstance(hook, dict):
                        continue
                    for key in self.HOOK_COMMAND_KEYS:
                        if key in hook:
                            new_cmd, scripts = self._rewrite_command_for_target(
                                hook[key], package_path, package_name, target,
                                hook_file_dir=hook_file_dir,
                                root_dir=root_dir,
                            )
                            if scripts:
                                _log.debug(
                                    "Hook %s/%s: rewrote '%s' key (%d script(s))",
                                    package_name, event_name, key, len(scripts),
                                )
                            hook[key] = new_cmd
                            all_scripts.extend(scripts)

        # De-duplicate by target path to avoid redundant copies when
        # multiple keys (e.g. command + bash) reference the same script.
        seen_targets: dict[str, Path] = {}
        for source, target_rel in all_scripts:
            if target_rel not in seen_targets:
                seen_targets[target_rel] = source
        unique_scripts = [(src, tgt) for tgt, src in seen_targets.items()]

        return rewritten, unique_scripts

    def _get_package_name(self, package_info) -> str:
        """Get a short package name for use in file/directory naming.

        Args:
            package_info: PackageInfo object

        Returns:
            str: Package name derived from install path
        """
        return package_info.install_path.name

    def integrate_package_hooks(self, package_info, project_root: Path,
                                 force: bool = False,
                                 managed_files: set = None,
                                 diagnostics=None,
                                 target=None) -> HookIntegrationResult:
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

        if not hook_files:
            return HookIntegrationResult(
                files_integrated=0, files_updated=0,
                files_skipped=0, target_paths=[],
            )

        root_dir = target.root_dir if target else ".github"
        hooks_dir = project_root / root_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        package_name = self._get_package_name(package_info)
        hooks_integrated = 0
        scripts_copied = 0
        target_paths: List[Path] = []

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            # Rewrite script paths for VSCode target
            rewritten, scripts = self._rewrite_hooks_data(
                data, package_info.install_path, package_name, "vscode",
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
            )

            # Generate target filename (clean, no -apm suffix)
            stem = hook_file.stem
            target_filename = f"{package_name}-{stem}.json"
            target_path = hooks_dir / target_filename
            rel_path = portable_relpath(target_path, project_root)

            if self.check_collision(target_path, rel_path, managed_files, force, diagnostics=diagnostics):
                continue

            # Write rewritten JSON
            with open(target_path, 'w', encoding='utf-8') as f:
                json.dump(rewritten, f, indent=2)
                f.write('\n')

            hooks_integrated += 1
            target_paths.append(target_path)

            # Copy referenced scripts (individual file tracking)
            for source_file, target_rel in scripts:
                target_script = project_root / target_rel
                if self.check_collision(target_script, target_rel, managed_files, force, diagnostics=diagnostics):
                    continue
                target_script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_script)
                scripts_copied += 1
                target_paths.append(target_script)

        return HookIntegrationResult(
            files_integrated=hooks_integrated, files_updated=0,
            files_skipped=0, target_paths=target_paths,
            scripts_copied=scripts_copied,
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
        managed_files: set = None,
        diagnostics=None,
        target=None,
    ) -> HookIntegrationResult:
        """Integrate hooks by merging into a target-specific JSON config.

        This is the shared implementation for Claude, Cursor, and Codex
        targets that merge hook entries into a single JSON file (as
        opposed to Copilot which uses individual JSON files).
        """
        _empty = HookIntegrationResult(
            files_integrated=0, files_updated=0,
            files_skipped=0, target_paths=[],
        )

        root_dir = target.root_dir if target else f".{config.target_key}"
        target_dir = project_root / root_dir

        # Opt-in check: some targets only deploy when their dir exists
        if config.require_dir and not target_dir.exists():
            return _empty

        hook_files = self.find_hook_files(package_info.install_path)
        if not hook_files:
            return _empty

        package_name = self._get_package_name(package_info)
        hooks_integrated = 0
        scripts_copied = 0
        target_paths: List[Path] = []
        # Events whose prior-owned entries have already been cleared on
        # this install run. Packages can contribute to the same event
        # from multiple hook files — we must only strip once so earlier
        # files' fresh entries aren't wiped by later iterations.
        cleared_events: set = set()

        # Read existing JSON config
        json_path = target_dir / config.config_filename
        json_config: Dict = {}
        if json_path.exists():
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    json_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                json_config = {}

        if "hooks" not in json_config:
            json_config["hooks"] = {}

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            # Rewrite script paths for the target
            rewritten, scripts = self._rewrite_hooks_data(
                data, package_info.install_path, package_name,
                config.target_key,
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
            )

            # Merge hooks into config (additive)
            hooks = rewritten.get("hooks", {})
            event_map = _HOOK_EVENT_MAP.get(config.target_key, {})
            for raw_event_name, entries in hooks.items():
                if not isinstance(entries, list):
                    continue
                event_name = event_map.get(raw_event_name, raw_event_name)
                if event_name not in json_config["hooks"]:
                    json_config["hooks"][event_name] = []

                # Transform flat Copilot entries to Gemini nested format
                if config.target_key == "gemini":
                    entries = _to_gemini_hook_entries(entries)

                # Mark each entry with APM source for sync/cleanup
                for entry in entries:
                    if isinstance(entry, dict):
                        entry["_apm_source"] = package_name

                # Idempotent upsert: drop any prior entries owned by this
                # package before appending fresh ones. Without this, every
                # `apm install` re-run duplicates the package's hooks
                # because `.extend()` is unconditional. See microsoft/apm#708.
                # Only strip once per event per install run — a package
                # with multiple hook files targeting the same event
                # contributes each file's entries in turn, and stripping
                # on every iteration would erase earlier files' work.
                if event_name not in cleared_events:
                    json_config["hooks"][event_name] = [
                        e for e in json_config["hooks"][event_name]
                        if not (isinstance(e, dict) and e.get("_apm_source") == package_name)
                    ]
                    cleared_events.add(event_name)
                json_config["hooks"][event_name].extend(entries)

            hooks_integrated += 1

            # Copy referenced scripts
            for source_file, target_rel in scripts:
                target_script = project_root / target_rel
                if self.check_collision(
                    target_script, target_rel, managed_files, force,
                    diagnostics=diagnostics,
                ):
                    continue
                target_script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_script)
                scripts_copied += 1
                target_paths.append(target_script)

        # Write JSON config back
        # Don't track the config file in target_paths -- it's a shared
        # file cleaned via _apm_source markers, not file-level deletion
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_config, f, indent=2)
            f.write('\n')

        return HookIntegrationResult(
            files_integrated=hooks_integrated, files_updated=0,
            files_skipped=0, target_paths=target_paths,
            scripts_copied=scripts_copied,
        )

    # ------------------------------------------------------------------
    # DEPRECATED per-target methods -- delegate to _integrate_merged_hooks
    # ------------------------------------------------------------------

    def integrate_package_hooks_claude(self, package_info, project_root: Path,
                                        force: bool = False,
                                        managed_files: set = None,
                                        diagnostics=None) -> HookIntegrationResult:
        """Integrate hooks into .claude/settings.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["claude"], package_info, project_root,
            force=force, managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def integrate_package_hooks_cursor(self, package_info, project_root: Path,
                                        force: bool = False,
                                        managed_files: set = None,
                                        diagnostics=None) -> HookIntegrationResult:
        """Integrate hooks into .cursor/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["cursor"], package_info, project_root,
            force=force, managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def integrate_package_hooks_codex(self, package_info, project_root: Path,
                                      force: bool = False,
                                      managed_files: set = None,
                                      diagnostics=None) -> HookIntegrationResult:
        """Integrate hooks into .codex/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["codex"], package_info, project_root,
            force=force, managed_files=managed_files,
            diagnostics=diagnostics,
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
        managed_files: set = None,
        diagnostics=None,
    ) -> "HookIntegrationResult":
        """Integrate hooks for a single *target*.

        Copilot uses individual JSON files (genuinely different pattern).
        All other merge-based targets are dispatched via the
        ``_MERGE_HOOK_TARGETS`` registry.
        """
        if target.name == "copilot":
            return self.integrate_package_hooks(
                package_info, project_root,
                force=force, managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
            )

        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is not None:
            return self._integrate_merged_hooks(
                config, package_info, project_root,
                force=force, managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
            )

        return HookIntegrationResult(
            files_integrated=0, files_updated=0,
            files_skipped=0, target_paths=[],
        )

    def sync_integration(self, apm_package, project_root: Path,
                          managed_files: set = None,
                          targets=None) -> Dict:
        """Remove APM-managed hook files.

        Uses *managed_files* (relative paths) to surgically remove only
        APM-tracked files.  Falls back to legacy ``*-apm.json`` glob when
        *managed_files* is ``None``.

        **Never** calls ``shutil.rmtree``.

        Also cleans APM entries from merged-hook JSON files via the
        ``_apm_source`` marker.
        """
        from .targets import KNOWN_TARGETS

        stats: Dict[str, int] = {'files_removed': 0, 'errors': 0}

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
                        stats['files_removed'] += 1
                        deleted.append(target_file)
                    except Exception:
                        stats['errors'] += 1
            # Batch parent cleanup -- single bottom-up pass
            self.cleanup_empty_parents(deleted, stop_at=project_root)
        else:
            # Legacy fallback  -- glob for old -apm suffix files
            hooks_dir = project_root / ".github" / "hooks"
            if hooks_dir.exists():
                for hook_file in hooks_dir.glob("*-apm.json"):
                    try:
                        hook_file.unlink()
                        stats['files_removed'] += 1
                    except Exception:
                        stats['errors'] += 1

        # Clean APM entries from merged-hook JSON configs (uses _apm_source marker)
        for t in source:
            config = _MERGE_HOOK_TARGETS.get(t.name)
            if config is not None:
                json_path = project_root / t.root_dir / config.config_filename
                if t.name == "claude":
                    # Claude uses settings.json with special structure
                    if json_path.exists():
                        try:
                            with open(json_path, 'r', encoding='utf-8') as f:
                                settings = json.load(f)

                            if "hooks" in settings:
                                modified = False
                                for event_name in list(settings["hooks"].keys()):
                                    matchers = settings["hooks"][event_name]
                                    if isinstance(matchers, list):
                                        filtered = [
                                            m for m in matchers
                                            if not (isinstance(m, dict) and "_apm_source" in m)
                                        ]
                                        if len(filtered) != len(matchers):
                                            modified = True
                                        settings["hooks"][event_name] = filtered
                                        if not filtered:
                                            del settings["hooks"][event_name]

                                if not settings["hooks"]:
                                    del settings["hooks"]

                                if modified:
                                    with open(json_path, 'w', encoding='utf-8') as f:
                                        json.dump(settings, f, indent=2)
                                        f.write('\n')
                                    stats['files_removed'] += 1
                        except (json.JSONDecodeError, OSError):
                            stats['errors'] += 1
                else:
                    self._clean_apm_entries_from_json(json_path, stats)

        return stats

    @staticmethod
    def _clean_apm_entries_from_json(json_path: Path, stats: Dict[str, int]) -> None:
        """Remove APM-tagged entries from a hooks JSON file.

        Filters out entries with ``_apm_source`` markers and cleans up
        empty event arrays and the ``hooks`` key itself.
        """
        if not json_path.exists():
            return
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if "hooks" not in data:
                return

            modified = False
            for event_name in list(data["hooks"].keys()):
                entries = data["hooks"][event_name]
                if isinstance(entries, list):
                    filtered = [
                        e for e in entries
                        if not (isinstance(e, dict) and "_apm_source" in e)
                    ]
                    if len(filtered) != len(entries):
                        modified = True
                    data["hooks"][event_name] = filtered
                    if not filtered:
                        del data["hooks"][event_name]

            if not data["hooks"]:
                del data["hooks"]

            if modified:
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                    f.write('\n')
                stats['files_removed'] += 1
        except (json.JSONDecodeError, OSError):
            stats['errors'] += 1

