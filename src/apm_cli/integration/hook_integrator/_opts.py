# pylint: disable=duplicate-code
"""Dataclass parameter objects for hook integrator helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class HookRewriteOpts:
    """Optional arguments for hook command and data rewriting."""

    package_path: Path
    package_name: str
    target: str
    hook_file_dir: Path | None = None
    root_dir: str | None = None
    deploy_root: Path | None = None


@dataclass(frozen=True, slots=True)
class HookIntegrateOpts:
    """Optional arguments for hook integration functions."""

    force: bool = False
    managed_files: Any = None
    diagnostics: Any = None
    target: Any = None


from apm_cli.integration.base_integrator import IntegrationResult


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

    config_filename: str  # e.g. "settings.json" or "hooks.json"
    target_key: str  # target name passed to _rewrite_hooks_data
    require_dir: bool  # True = skip if target dir doesn't exist
    schema_strict: bool = False  # True = strip _apm_source before writing


# Per-target hook event name mapping.  Packages are authored with
# Copilot (camelCase) or Claude (PascalCase) names; targets that use
# different conventions get their events renamed during merge.
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


# Mapping from hook-file stem suffix to the set of target keys that
# should receive the file.  Files whose stem does not match any
# suffix are treated as universal and deployed to every target.
_HOOK_FILE_TARGET_SUFFIXES: dict[str, set[str]] = {
    "copilot-hooks": {"copilot", "vscode"},
    "cursor-hooks": {"cursor"},
    "claude-hooks": {"claude"},
    "codex-hooks": {"codex"},
    "gemini-hooks": {"gemini"},
    "windsurf-hooks": {"windsurf"},
}


def _filter_hook_files_for_target(
    hook_files: list[Path],
    target_key: str,
) -> list[Path]:
    """Return only hook files intended for *target_key*.

    Routing is based on the file stem (case-insensitive):
      - Stems ending with a known ``-<target>-hooks`` suffix are
        restricted to matching targets.
      - All other stems (e.g. ``hooks``, ``my-custom-hooks``) are
        universal and pass through for every target.

    Args:
        hook_files: All discovered hook JSON files.
        target_key: Lowercase target name (e.g. ``"claude"``, ``"cursor"``).

    Returns:
        Filtered list preserving original order.
    """
    result: list[Path] = []
    for hf in hook_files:
        stem_lower = hf.stem.lower()
        matched_suffix: str | None = None
        for suffix, allowed_targets in _HOOK_FILE_TARGET_SUFFIXES.items():
            if stem_lower == suffix or stem_lower.endswith(f"-{suffix}"):
                matched_suffix = suffix
                if target_key in allowed_targets:
                    result.append(hf)
                break
        if matched_suffix is None:
            # Universal file -- deploy to all targets
            result.append(hf)
    return result
