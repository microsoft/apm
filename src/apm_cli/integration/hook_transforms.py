"""Pure transform and rewrite helpers for APM hook integration.

This module holds stateless transform utilities and config data structures
that are shared by the hook integration, merge, and sync layers.
"""

import copy
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.utils.console import _rich_warning
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

_log = logging.getLogger("apm_cli.integration.hook_integrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Superset of all known script-path keys across supported hook specs.
#   "command":    Claude Code (primary), VS Code (default/cross-platform), Cursor
#   "bash":       GitHub Copilot Agent cloud/CLI
#   "powershell": GitHub Copilot Agent cloud/CLI
#   "windows":    VS Code (OS-specific override)
#   "linux":      VS Code (OS-specific override)
#   "osx":        VS Code (OS-specific override)
_HOOK_COMMAND_KEYS: tuple[str, ...] = (
    "command",
    "bash",
    "powershell",
    "windows",
    "linux",
    "osx",
)

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
    "kiro": {
        # Copilot / Claude -> Kiro camelCase events
        "PreToolUse": "preToolUse",
        "preToolUse": "preToolUse",
        "PostToolUse": "postToolUse",
        "postToolUse": "postToolUse",
        "UserPromptSubmit": "promptSubmit",
        "userPromptSubmit": "promptSubmit",
        "promptSubmit": "promptSubmit",
        "Stop": "agentStop",
        "stop": "agentStop",
        "AgentStop": "agentStop",
        "agentStop": "agentStop",
        "PreTaskExecution": "preTaskExecution",
        "preTaskExecution": "preTaskExecution",
        "PostTaskExecution": "postTaskExecution",
        "postTaskExecution": "postTaskExecution",
    },
}

# Expected hook event naming convention per target.
# Used to warn when a package author deploys events whose casing does not
# match the target's convention AND no explicit rename mapping exists.
_HOOK_EVENT_EXPECTED_CASING: dict[str, str] = {
    "copilot": "camelCase",
    "vscode": "PascalCase",
    "claude": "PascalCase",
    "cursor": "PascalCase",
    "codex": "PascalCase",
    "gemini": "PascalCase",
    "antigravity": "PascalCase",
    "windsurf": "PascalCase",
    "kiro": "camelCase",
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
    "antigravity-hooks": {"antigravity"},
    "windsurf-hooks": {"windsurf"},
    "kiro-hooks": {"kiro"},
}

# Filename used to persist _apm_source markers for schema-strict targets.
_APM_HOOKS_SIDECAR = "apm-hooks.json"


@dataclass(frozen=True)
class _MergeHookConfig:
    """Configuration for targets that merge hooks into a single JSON file."""

    config_filename: str  # e.g. "settings.json" or "hooks.json"
    target_key: str  # target name passed to _rewrite_hooks_data
    require_dir: bool  # True = skip if target dir doesn't exist
    schema_strict: bool = False  # True = strip _apm_source before writing to disk
    # Top-level JSON key the merged event map lives under.  Defaults to
    # "hooks" (Claude/Cursor/Codex/Gemini/Windsurf).  Antigravity's native
    # schema keys hooks by an arbitrary hook *name*, so APM reserves the
    # single name "apm" as its container and leaves sibling user hook-names
    # untouched.
    event_container_key: str = "hooks"
    # Target-specific top-level keys to inject into the config file when
    # absent.  Used to emit required schema fields (e.g. "version": 1 for
    # Cursor) that APM does not otherwise write.  Existing keys are never
    # overwritten -- the guard in _integrate_merged_hooks() preserves any
    # value the user has set manually.
    top_level_defaults: dict[str, Any] = field(default_factory=dict)


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
        top_level_defaults={"version": 1},
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
    "antigravity": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="antigravity",
        require_dir=True,
        event_container_key="apm",
    ),
    "windsurf": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="windsurf",
        require_dir=True,
    ),
}

# ---------------------------------------------------------------------------
# Event name utilities
# ---------------------------------------------------------------------------


def _detect_event_casing(name: str) -> str | None:
    """Return 'camelCase', 'PascalCase', or None for an event name string."""
    if not name or not name[0].isalpha():
        return None
    if name[0].islower() and any(c.isupper() for c in name[1:]):
        return "camelCase"
    if name[0].isupper():
        return "PascalCase"
    return None


def _sanitize_event_name(name: str) -> str:
    """Return event name with non-printable-ASCII characters stripped, for safe logging."""
    return "".join(c for c in name if 0x20 <= ord(c) <= 0x7E)


def _emit_hook_event_diagnostics(
    event_names: list[str],
    target_key: str,
    event_map: dict[str, str],
) -> None:
    """Log hook events per-target and warn on unmapped casing mismatches.

    This is informational only -- it never blocks deployment.
    """
    if not event_names:
        return
    event_label = "hook event" if len(event_names) == 1 else "hook events"
    _log.info(
        "target %s: detected %s: %s",
        target_key,
        event_label,
        ", ".join(sorted(_sanitize_event_name(n) for n in event_names)),
    )
    expected_casing = _HOOK_EVENT_EXPECTED_CASING.get(target_key)
    if not expected_casing:
        return
    # Warn for events whose detected casing does not match the target convention
    # and that are not covered by an explicit rename in event_map.
    mismatched = [
        n
        for n in event_names
        if _detect_event_casing(n) not in (None, expected_casing) and n not in event_map
    ]
    if mismatched:
        example = "preToolUse" if expected_casing == "camelCase" else "PreToolUse"
        safe_mismatched = sorted(_sanitize_event_name(n) for n in mismatched)
        _rich_warning(
            f"Hook events for target '{target_key}' may not be recognized: "
            f"{', '.join(safe_mismatched)}. "
            f"Target expects {expected_casing} (e.g. {example}). "
            f"Rename events to match the {expected_casing} convention, then reinstall."
        )
        _log.warning(
            "target %s: hook event casing mismatch (no mapping): %s",
            target_key,
            ", ".join(safe_mismatched),
        )


# ---------------------------------------------------------------------------
# Gemini transforms
# ---------------------------------------------------------------------------


def _to_nested_hook_entries(entries: list, key_fixer) -> list:
    """Wrap flat Copilot hook entries in the ``{"hooks": [...]}`` nesting.

    Shared by the Gemini and Antigravity transforms (both use the Claude
    nested matcher shape for tool events).  *key_fixer* renames the inner
    command/timeout keys in place for the specific target.  Entries already
    in nested form have only their inner keys fixed.
    """
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        # Already nested (Claude / Gemini format) -- just fix inner keys
        if "hooks" in entry and isinstance(entry["hooks"], list):
            for hook in entry["hooks"]:
                key_fixer(hook)
            result.append(entry)
            continue
        # Flat Copilot entry -- wrap in nested format
        inner = dict(entry)
        key_fixer(inner)
        apm_source = inner.pop("_apm_source", None)
        outer: dict = {"hooks": [inner]}
        if apm_source:
            outer["_apm_source"] = apm_source
        result.append(outer)
    return result


def _to_gemini_hook_entries(entries: list) -> list:
    """Transform hook entries into Gemini CLI format.

    Gemini requires ``{"hooks": [...]}`` nesting, uses ``command`` (not
    ``bash``), and ``timeout`` in milliseconds (not ``timeoutSec`` in
    seconds).  Entries already in Claude/Gemini nested format are left
    unchanged.
    """
    return _to_nested_hook_entries(entries, _copilot_keys_to_gemini)


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


# Antigravity events that use the nested ``{matcher, hooks:[...]}`` matcher
# shape.  All other events (PreInvocation/PostInvocation/Stop) take a flat
# list of handler dicts; matcher has no meaning there.
_ANTIGRAVITY_NESTED_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})


def _to_antigravity_hook_entries(entries: list, event_name: str) -> list:
    """Transform hook entries into Antigravity CLI native format.

    Antigravity's ``hooks.json`` uses TWO entry shapes:

    * ``PreToolUse`` / ``PostToolUse`` -- nested
      ``[{"matcher": "*", "hooks": [handler, ...]}]``.
    * ``PreInvocation`` / ``PostInvocation`` / ``Stop`` -- a flat list of
      handler dicts (``matcher`` is ignored).

    A handler is ``{"type": "command", "command": ..., "timeout": <sec>}``.
    Unlike Gemini, ``timeout`` stays in SECONDS (no ms conversion).
    """
    if event_name in _ANTIGRAVITY_NESTED_EVENTS:
        return _to_nested_hook_entries(entries, _copilot_keys_to_antigravity)
    # Flat handler list -- fix inner keys without wrapping.
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        # A pre-nested entry (matcher + hooks[]) is flattened to its handlers.
        if "hooks" in entry and isinstance(entry["hooks"], list):
            apm_source = entry.get("_apm_source")
            for hook in entry["hooks"]:
                if isinstance(hook, dict):
                    _copilot_keys_to_antigravity(hook)
                    if apm_source and "_apm_source" not in hook:
                        hook["_apm_source"] = apm_source
                result.append(hook)
            continue
        handler = dict(entry)
        _copilot_keys_to_antigravity(handler)
        result.append(handler)
    return result


def _copilot_keys_to_antigravity(hook: dict) -> None:
    """Rename Copilot hook keys to Antigravity equivalents in-place."""
    # bash / powershell -> command
    if "command" not in hook:
        for key in ("bash", "powershell", "windows"):
            if key in hook:
                hook["command"] = hook.pop(key)
                break
    # timeoutSec (seconds) -> timeout (SECONDS -- Antigravity uses seconds)
    if "timeoutSec" in hook:
        hook["timeout"] = hook.pop("timeoutSec")


# ---------------------------------------------------------------------------
# Sidecar re-injection
# ---------------------------------------------------------------------------


def _reinject_apm_source_from_sidecar(hooks: dict, sidecar_data: dict) -> None:
    """Restore _apm_source markers from sidecar into in-memory hook entries.

    Schema-strict targets (e.g. Claude) do not persist ``_apm_source`` in
    their settings file.  Instead, ownership metadata is stored in a
    sidecar file.  This helper re-injects those markers so the rest of
    the integration logic can work with them as normal.

    Each sidecar entry is consumed at most once to prevent falsely claiming
    user-owned hooks that happen to have identical content to an APM hook.

    Args:
        hooks: The ``"hooks"`` dict loaded from the target config file
            (mutated in-place).
        sidecar_data: The dict loaded from the sidecar file.
    """
    for event_name, sidecar_entries in sidecar_data.items():
        if event_name not in hooks or not isinstance(sidecar_entries, list):
            continue
        # Build a dict keyed by normalised content -> list of sources.
        # Each source is popped on first match so identical content shared
        # between APM and the user is only claimed once.
        pool: dict[str, deque[str]] = {}
        for sc_entry in sidecar_entries:
            if isinstance(sc_entry, dict) and "_apm_source" in sc_entry:
                cmp = {k: v for k, v in sorted(sc_entry.items()) if k != "_apm_source"}
                cmp_key = json.dumps(cmp, sort_keys=True)
                pool.setdefault(cmp_key, deque()).append(sc_entry["_apm_source"])

        for disk_entry in hooks[event_name]:
            if not isinstance(disk_entry, dict) or "_apm_source" in disk_entry:
                continue
            disk_cmp = {k: v for k, v in sorted(disk_entry.items()) if k != "_apm_source"}
            disk_key = json.dumps(disk_cmp, sort_keys=True)
            sources = pool.get(disk_key)
            if sources:
                disk_entry["_apm_source"] = sources.popleft()
                if not sources:
                    del pool[disk_key]


# ---------------------------------------------------------------------------
# Hook file routing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Command path rewriting
# ---------------------------------------------------------------------------


def _rewrite_command_for_target(
    command: str,
    package_path: Path,
    package_name: str,
    target: str,
    hook_file_dir: Path | None = None,
    root_dir: str | None = None,
    deploy_root: Path | None = None,
    _warn=None,
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
        deploy_root: Absolute root of the deployment directory.  When provided,
            rewritten script paths are resolved to absolute paths under this
            root so the target (e.g. Claude Code) can execute them regardless
            of the working directory.  When *None*, rewritten paths stay
            relative (backward-compatible behaviour).
        _warn: Warning callable (defaults to _rich_warning); override in tests
            or when the caller needs to intercept warnings.

    Returns:
        Tuple of (rewritten_command, list of (source_file, relative_target_path))
    """
    if _warn is None:
        _warn = _rich_warning
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
    elif target == "windsurf":
        base_root = root_dir or ".windsurf"
        scripts_base = f"{base_root}/hooks/{package_name}"
    elif target == "kiro":
        base_root = root_dir or ".kiro"
        scripts_base = f"{base_root}/hooks/{package_name}"
    else:
        base_root = root_dir or ".claude"
        scripts_base = f"{base_root}/hooks/{package_name}"

    # Handle plugin root variable references (always relative to package root)
    # Match both forward-slash and backslash separators (Windows hook JSON
    # may use backslashes: ${CLAUDE_PLUGIN_ROOT}\scripts\scan.ps1)
    plugin_root_pattern = (
        r"\$\{(?:CLAUDE_PLUGIN_ROOT|CURSOR_PLUGIN_ROOT|KIRO_PLUGIN_ROOT|PLUGIN_ROOT)\}"
        r"([\\/][^\s\"']+)"
    )
    for match in re.finditer(plugin_root_pattern, command):
        full_var = match.group(0)
        # Normalize backslashes to forward slashes before Path construction
        # (on Unix, Path treats backslashes as literal filename chars)
        rel_path = match.group(1).replace("\\", "/").lstrip("/")

        try:
            source_file = ensure_path_within(package_path / rel_path, package_path)
        except PathTraversalError:
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            resolved_cmd = (
                str((deploy_root / target_rel).resolve()) if deploy_root is not None else target_rel
            )
            new_command = new_command.replace(full_var, resolved_cmd)
        else:
            # File absent: always warn so a misconfigured hook is never
            # silently deployed.  For user-scope (deploy_root set) also
            # rewrite the unexpanded variable to an absolute source path
            # so the target surfaces a clear "file not found".  For
            # project-scope (deploy_root is None) leave the variable in
            # place -- rewriting to an absolute path would re-introduce
            # the #1394 portability regression in committed configs.
            _warn(f"Hook script not found: {source_file}")
            if deploy_root is not None:
                new_command = new_command.replace(full_var, str(source_file))

    # Handle relative ./path and .\path references (safe to run after
    # ${CLAUDE_PLUGIN_ROOT} substitution since replacements produce paths
    # like ".github/..." not "./" or ".\")
    # Match both forward-slash and backslash separators (Windows hook JSON
    # may use backslashes: .\scripts\scan.ps1)
    # Resolve from hook file's directory if available, else fall back to package root
    resolve_base = hook_file_dir if hook_file_dir else package_path
    rel_pattern = r"(\.[\\/][^\s\"']+)"
    for match in re.finditer(rel_pattern, new_command):
        rel_ref = match.group(1)
        # Normalize to forward slashes for path resolution
        rel_path = rel_ref[2:].replace("\\", "/")

        try:
            source_file = ensure_path_within(resolve_base / rel_path, package_path)
        except PathTraversalError:
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            resolved_cmd = (
                str((deploy_root / target_rel).resolve()) if deploy_root is not None else target_rel
            )
            new_command = new_command.replace(rel_ref, resolved_cmd)
        else:
            # File absent: always warn (see ${PLUGIN_ROOT} branch above
            # for the project-scope vs user-scope rationale).
            _warn(f"Hook script not found: {source_file}")
            if deploy_root is not None:
                new_command = new_command.replace(rel_ref, str(source_file))

    return new_command, scripts_to_copy


def _rewrite_hooks_data(
    data: dict,
    package_path: Path,
    package_name: str,
    target: str,
    hook_file_dir: Path | None = None,
    root_dir: str | None = None,
    deploy_root: Path | None = None,
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
        deploy_root: Absolute root of the deployment directory.  When provided,
            all rewritten script paths are resolved to absolute paths so the
            target can locate scripts regardless of the working directory.
            When *None*, paths remain relative (backward-compatible behaviour).

    Returns:
        Tuple of (rewritten_data_copy, list of (source_file, target_rel_path))
    """
    rewritten = copy.deepcopy(data)
    all_scripts: list[tuple[Path, str]] = []

    hooks = rewritten.get("hooks", {})
    for event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            # Rewrite script paths in the matcher dict itself
            # (GitHub Copilot flat format: bash/powershell/windows keys at this level)
            for key in _HOOK_COMMAND_KEYS:
                if key in matcher:
                    new_cmd, scripts = _rewrite_command_for_target(
                        matcher[key],
                        package_path,
                        package_name,
                        target,
                        hook_file_dir=hook_file_dir,
                        root_dir=root_dir,
                        deploy_root=deploy_root,
                    )
                    if scripts:
                        _log.debug(
                            "Hook %s/%s: rewrote '%s' key (%d script(s))",
                            package_name,
                            event_name,
                            key,
                            len(scripts),
                        )
                    matcher[key] = new_cmd
                    all_scripts.extend(scripts)

            # Rewrite script paths in nested hooks array
            # (Claude format: matcher groups with inner hooks array)
            for hook in matcher.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                for key in _HOOK_COMMAND_KEYS:
                    if key in hook:
                        new_cmd, scripts = _rewrite_command_for_target(
                            hook[key],
                            package_path,
                            package_name,
                            target,
                            hook_file_dir=hook_file_dir,
                            root_dir=root_dir,
                            deploy_root=deploy_root,
                        )
                        if scripts:
                            _log.debug(
                                "Hook %s/%s: rewrote '%s' key (%d script(s))",
                                package_name,
                                event_name,
                                key,
                                len(scripts),
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


# ---------------------------------------------------------------------------
# Hook transparency helpers (display payload construction)
# ---------------------------------------------------------------------------


def _iter_hook_entries(payload: dict) -> list[tuple[str, dict]]:
    """Flatten hook payloads into (event_name, entry_dict) pairs."""
    entries: list[tuple[str, dict]] = []
    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return entries
    for event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for key in _HOOK_COMMAND_KEYS:
                value = matcher.get(key)
                if isinstance(value, str):
                    entries.append((event_name, {key: value}))
            nested_hooks = matcher.get("hooks", [])
            if not isinstance(nested_hooks, list):
                continue
            for hook in nested_hooks:
                if not isinstance(hook, dict):
                    continue
                for key in _HOOK_COMMAND_KEYS:
                    value = hook.get(key)
                    if isinstance(value, str):
                        entries.append((event_name, {key: value}))
    return entries


def _summarize_command(entry: dict) -> str:
    """Return a human-readable summary for a single hook command entry."""
    command = ""
    for key in _HOOK_COMMAND_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            command = value.strip()
            break
    if not command:
        return "runs hook command"
    # Collapse any internal whitespace (including embedded newlines) so
    # the summary is always single-line. A hook command containing a
    # newline must not break install-log formatting or enable
    # log-spoofing. Addresses Copilot inline on hook_integrator.py.
    command = " ".join(command.split())
    for token in command.split():
        cleaned = token.strip("\"'")
        if "/" in cleaned or cleaned.startswith("."):
            return f"runs {cleaned}"
    return f"runs {command}"


def _build_display_payload(
    target_label: str,
    output_path: str,
    source_hook_file: Any,
    rewritten: dict,
) -> dict:
    """Build CLI display metadata for an integrated hook file.

    Uses post-path-rewrite data (the 'rewritten' dict) so the summary
    faithfully reflects what is actually written to disk and executed.
    """
    actions = []
    for event_name, entry in _iter_hook_entries(rewritten):
        actions.append(
            {
                "event": event_name,
                "summary": _summarize_command(entry),
            }
        )
    return {
        "target_label": target_label,
        "output_path": output_path,
        "source_hook_file": source_hook_file.name
        if hasattr(source_hook_file, "name")
        else str(source_hook_file),
        "actions": actions,
        "rendered_json": json.dumps(rewritten, indent=2, sort_keys=True),
    }
