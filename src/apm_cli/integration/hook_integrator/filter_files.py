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

import json
import logging
from pathlib import Path

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


def _build_hook_prefixes(source) -> tuple[str, ...]:
    """Return the tuple of hook path prefixes for active targets."""
    prefixes = []
    for target in source:
        if not target.supports("hooks"):
            continue
        mapping = target.primitives["hooks"]
        effective_root = mapping.deploy_root or target.root_dir
        prefixes.append(f"{effective_root}/hooks/")
    return tuple(prefixes)


def _remove_managed_hook_files(
    self, managed_files, *, project_root: Path, hook_prefixes: tuple[str, ...]
) -> dict[str, int]:
    """Remove manifest-tracked hook files and clean empty parents."""
    stats: dict[str, int] = {"files_removed": 0, "errors": 0}
    deleted: list[Path] = []
    for rel_path in managed_files:
        normalized = rel_path.replace("\\", "/")
        if not normalized.startswith(hook_prefixes) or ".." in rel_path:
            continue
        target_file = project_root / rel_path
        if not (target_file.exists() and target_file.is_file()):
            continue
        try:
            target_file.unlink()
            stats["files_removed"] += 1
            deleted.append(target_file)
        except Exception:
            stats["errors"] += 1
    self.cleanup_empty_parents(deleted, stop_at=project_root)
    return stats


def _remove_legacy_hook_files(project_root: Path) -> dict[str, int]:
    """Remove legacy `*-apm.json` hook files."""
    stats: dict[str, int] = {"files_removed": 0, "errors": 0}
    hooks_dir = project_root / ".github" / "hooks"
    if not hooks_dir.exists():
        return stats
    for hook_file in hooks_dir.glob("*-apm.json"):
        try:
            hook_file.unlink()
            stats["files_removed"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def _clean_merged_hook_configs(self, *, source, project_root: Path, stats: dict[str, int]) -> None:
    """Remove APM-owned entries from shared hook config files."""
    for target in source:
        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is None:
            continue
        json_path = project_root / target.root_dir / config.config_filename
        if target.name == "claude":
            _clean_claude_apm_hooks(json_path, stats)
        else:
            self._clean_apm_entries_from_json(json_path, stats)


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


def sync_integration(
    self,
    apm_package,
    project_root: Path,
    managed_files: set | None = None,
    targets=None,
) -> dict:
    """Remove APM-managed hook files."""
    from ..targets import KNOWN_TARGETS

    del apm_package
    source = targets if targets is not None else list(KNOWN_TARGETS.values())
    if managed_files is not None:
        stats = _remove_managed_hook_files(
            self,
            managed_files,
            project_root=project_root,
            hook_prefixes=_build_hook_prefixes(source),
        )
    else:
        stats = _remove_legacy_hook_files(project_root)
    _clean_merged_hook_configs(self, source=source, project_root=project_root, stats=stats)
    return stats


def _clean_claude_apm_hooks(json_path: Path, stats: dict[str, int]) -> None:
    """Remove APM-tagged entries from a Claude ``settings.json`` hooks file.

    Handles Claude's nested matcher-group structure: filters out top-level
    matcher dicts that carry an ``_apm_source`` marker, then cleans up
    empty event arrays and the ``hooks`` key itself.

    Because Claude uses ``schema_strict`` mode, ``_apm_source`` markers are
    stored in a sidecar file (``apm-hooks.json``) rather than inline.  This
    function loads the sidecar first, re-injects the markers into the
    in-memory hooks, filters them out, writes back without markers, and
    finally deletes the sidecar.
    """
    if not json_path.exists():
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            settings = json.load(f)

        # Load sidecar to restore _apm_source markers
        from .merge_config import _APM_HOOKS_SIDECAR

        sidecar_path = json_path.parent / _APM_HOOKS_SIDECAR
        sidecar_data: dict = {}
        if sidecar_path.exists():
            try:
                with open(sidecar_path, encoding="utf-8") as sf:
                    _raw = json.load(sf)
                if isinstance(_raw, dict):
                    sidecar_data = _raw
                else:
                    _log.warning(
                        "Sidecar file %s contains non-dict JSON; treating as empty.",
                        sidecar_path,
                    )
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning(
                    "Failed to read sidecar %s: %s; treating as empty.",
                    sidecar_path,
                    exc,
                )

        # Re-inject _apm_source from sidecar
        if sidecar_data and "hooks" in settings:
            from ._sidecar import _reinject_apm_source_from_sidecar

            _reinject_apm_source_from_sidecar(settings["hooks"], sidecar_data)

        if "hooks" not in settings:
            return

        modified = False
        for event_name in list(settings["hooks"].keys()):
            matchers = settings["hooks"][event_name]
            if isinstance(matchers, list):
                filtered = [m for m in matchers if not (isinstance(m, dict) and "_apm_source" in m)]
                if len(filtered) != len(matchers):
                    modified = True
                settings["hooks"][event_name] = filtered
                if not filtered:
                    del settings["hooks"][event_name]

        if not settings["hooks"]:
            del settings["hooks"]

        if modified:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
                f.write("\n")
            stats["files_removed"] += 1

            # Clean up sidecar
            if sidecar_path.exists():
                sidecar_path.unlink()

        # Remove stale sidecar when no hooks section remains
        if sidecar_path.exists() and "hooks" not in settings:
            sidecar_path.unlink()
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1


def _clean_apm_entries_from_json(json_path: Path, stats: dict[str, int]) -> None:
    """Remove APM-tagged entries from a hooks JSON file.

    Filters out entries with ``_apm_source`` markers and cleans up
    empty event arrays and the ``hooks`` key itself.
    """
    if not json_path.exists():
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if "hooks" not in data:
            return

        modified = False
        for event_name in list(data["hooks"].keys()):
            entries = data["hooks"][event_name]
            if isinstance(entries, list):
                filtered = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
                if len(filtered) != len(entries):
                    modified = True
                data["hooks"][event_name] = filtered
                if not filtered:
                    del data["hooks"][event_name]

        if not data["hooks"]:
            del data["hooks"]

        if modified:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            stats["files_removed"] += 1
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1
