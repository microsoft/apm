# pylint: disable=duplicate-code
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

import logging
import re
from pathlib import Path

from apm_cli.utils.console import _rich_warning

from ._opts import HookRewriteOpts
from .class_ import _MergeHookConfig

_log = logging.getLogger(__name__)
_HOOK_EVENT_MAP: dict[str, dict[str, str]] = {
    "claude": {
        # Copilot camelCase -> Claude PascalCase
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
    },
    "gemini": {
        # Copilot / Claude -> Gemini
        "PreToolUse": "BeforeTool",
        "preToolUse": "BeforeTool",
        "PostToolUse": "AfterTool",
        "postToolUse": "AfterTool",
        "Stop": "SessionEnd",
    },
}
_MERGE_HOOK_TARGETS: dict[str, _MergeHookConfig] = {
    "claude": _MergeHookConfig(
        config_filename="settings.json",
        target_key="claude",
        require_dir=False,
        schema_strict=True,
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
    "windsurf": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="windsurf",
        require_dir=True,
    ),
}
_HOOK_FILE_TARGET_SUFFIXES: dict[str, set[str]] = {
    "copilot-hooks": {"copilot", "vscode"},
    "cursor-hooks": {"cursor"},
    "claude-hooks": {"claude"},
    "codex-hooks": {"codex"},
    "gemini-hooks": {"gemini"},
    "windsurf-hooks": {"windsurf"},
}


def _scripts_base_for_target(opts: HookRewriteOpts) -> str:
    """Return the target scripts base path for one runtime."""
    root_dir = opts.root_dir
    package_name = opts.package_name
    if opts.target == "vscode":
        return f"{root_dir or '.github'}/hooks/scripts/{package_name}"
    if opts.target == "cursor":
        return f"{root_dir or '.cursor'}/hooks/{package_name}"
    if opts.target == "codex":
        return f"{root_dir or '.codex'}/hooks/{package_name}"
    if opts.target == "windsurf":
        return f"{root_dir or '.windsurf'}/hooks/{package_name}"
    return f"{root_dir or '.claude'}/hooks/{package_name}"


def _rewrite_plugin_root_refs(
    command: str, *, opts: HookRewriteOpts, scripts_base: str
) -> tuple[str, list[tuple[Path, str]]]:
    """Rewrite ${PLUGIN_ROOT} references in one command string."""
    package_path = opts.package_path
    scripts_to_copy: list[tuple[Path, str]] = []
    new_command = command
    plugin_root_pattern = (
        r"\$\{(?:CLAUDE_PLUGIN_ROOT|CURSOR_PLUGIN_ROOT|PLUGIN_ROOT)\}([\\/][^\s]+)"
    )
    deploy_root = opts.deploy_root
    for match in re.finditer(plugin_root_pattern, command):
        full_var = match.group(0)
        rel_path = match.group(1).replace("\\", "/").lstrip("/")
        source_file = (package_path / rel_path).resolve()
        if not source_file.is_relative_to(package_path.resolve()):
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            resolved_cmd = (
                str((deploy_root / target_rel).resolve()) if deploy_root is not None else target_rel
            )
            new_command = new_command.replace(full_var, resolved_cmd)
        elif deploy_root is not None:
            _rich_warning(f"Hook script not found: {source_file}")
            new_command = new_command.replace(full_var, str(source_file))
    return new_command, scripts_to_copy


def _rewrite_relative_refs(
    command: str, *, opts: HookRewriteOpts, scripts_base: str
) -> tuple[str, list[tuple[Path, str]]]:
    """Rewrite ./path command references in one command string."""
    package_path = opts.package_path
    resolve_base = opts.hook_file_dir if opts.hook_file_dir else package_path
    scripts_to_copy: list[tuple[Path, str]] = []
    new_command = command
    rel_pattern = r"(\.[\\/][^\s]+)"
    deploy_root = opts.deploy_root
    for match in re.finditer(rel_pattern, new_command):
        rel_ref = match.group(1)
        rel_path = rel_ref[2:].replace("\\", "/")
        source_file = (resolve_base / rel_path).resolve()
        if not source_file.is_relative_to(package_path.resolve()):
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            resolved_cmd = (
                str((deploy_root / target_rel).resolve()) if deploy_root is not None else target_rel
            )
            new_command = new_command.replace(rel_ref, resolved_cmd)
        elif deploy_root is not None:
            _rich_warning(f"Hook script not found: {source_file}")
            new_command = new_command.replace(rel_ref, str(source_file))
    return new_command, scripts_to_copy


def _rewrite_command_dict(
    self, command_container: dict, *, event_name: str, opts: HookRewriteOpts
) -> list[tuple[Path, str]]:
    """Rewrite all command-like keys in one hook or matcher dict."""
    scripts: list[tuple[Path, str]] = []
    for key in self.HOOK_COMMAND_KEYS:
        if key not in command_container:
            continue
        new_cmd, command_scripts = self._rewrite_command_for_target(command_container[key], opts)
        if command_scripts:
            _log.debug(
                "Hook %s/%s: rewrote '%s' key (%d script(s))",
                opts.package_name,
                event_name,
                key,
                len(command_scripts),
            )
        command_container[key] = new_cmd
        scripts.extend(command_scripts)
    return scripts


def _rewrite_command_for_target(
    self,
    command: str,
    opts: HookRewriteOpts,
) -> tuple[str, list[tuple[Path, str]]]:
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
    scripts_base = _scripts_base_for_target(opts)
    rewritten_command, plugin_scripts = _rewrite_plugin_root_refs(
        command,
        opts=opts,
        scripts_base=scripts_base,
    )
    rewritten_command, relative_scripts = _rewrite_relative_refs(
        rewritten_command,
        opts=opts,
        scripts_base=scripts_base,
    )
    return rewritten_command, plugin_scripts + relative_scripts


def _rewrite_hooks_data(
    self,
    data: dict,
    opts: HookRewriteOpts,
) -> tuple[dict, list[tuple[Path, str]]]:
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
    all_scripts: list[tuple[Path, str]] = []

    hooks = rewritten.get("hooks", {})
    for event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            all_scripts.extend(
                _rewrite_command_dict(self, matcher, event_name=event_name, opts=opts)
            )
            for hook in matcher.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                all_scripts.extend(
                    _rewrite_command_dict(self, hook, event_name=event_name, opts=opts)
                )

    # De-duplicate by target path to avoid redundant copies when
    # multiple keys (e.g. command + bash) reference the same script.
    seen_targets: dict[str, Path] = {}
    for source, target_rel in all_scripts:
        if target_rel not in seen_targets:
            seen_targets[target_rel] = source
    unique_scripts = [(src, tgt) for tgt, src in seen_targets.items()]

    return rewritten, unique_scripts
