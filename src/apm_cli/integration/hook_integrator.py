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
        "version": 1,
        "hooks": {
            "afterFileEdit": [
                {"command": "./hooks/format.sh"}
            ]
        }
    }

Script path handling:
    - ${CLAUDE_PLUGIN_ROOT}/path, ${CURSOR_PLUGIN_ROOT}/path, ${PLUGIN_ROOT}/path
      -> resolved relative to package root, rewritten for target
    - ./path -> relative path, resolved from the hook file context, rewritten for target
    - System commands (no path separators) -> passed through unchanged
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    MaterializationResult,
    MaterializationStatus,
    NativePayloadValidation,
)
from apm_cli.core.scope import InstallScope
from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
from apm_cli.integration.hook_file_routing import filter_hook_files_for_target
from apm_cli.integration.hook_native_formats import (
    _to_antigravity_hook_entries,
    _to_claude_hook_entries,
    _to_gemini_hook_entries,
)
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.console import _rich_warning
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from apm_cli.utils.paths import portable_relpath

_log = logging.getLogger(__name__)

# Testability seam: tests can patch deprecated filename routing without
# replacing the imported helper for every call site.
_filter_hook_files_for_target = filter_hook_files_for_target


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
    schema_strict: bool = True  # Ownership always lives outside native files.
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


# Per-target hook event name mapping.  Packages are authored with
# Copilot (camelCase) or Claude (PascalCase) names; targets that use
# different conventions get their events renamed during merge.
_HOOK_EVENT_MAP: dict[str, dict[str, str]] = {
    "copilot": {
        # Claude PascalCase -> Copilot camelCase
        "PreToolUse": "preToolUse",
        "preToolUse": "preToolUse",
        "PostToolUse": "postToolUse",
        "postToolUse": "postToolUse",
        "UserPromptSubmit": "userPromptSubmit",
        "userPromptSubmit": "userPromptSubmit",
        "Stop": "stop",
        "stop": "stop",
        "AgentStop": "agentStop",
        "agentStop": "agentStop",
        "PreTaskExecution": "preTaskExecution",
        "preTaskExecution": "preTaskExecution",
        "PostTaskExecution": "postTaskExecution",
        "postTaskExecution": "postTaskExecution",
    },
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
        # Portable and legacy spellings -> Kiro v1 PascalCase triggers.
        "PreToolUse": "PreToolUse",
        "preToolUse": "PreToolUse",
        "PostToolUse": "PostToolUse",
        "postToolUse": "PostToolUse",
        "UserPromptSubmit": "UserPromptSubmit",
        "userPromptSubmit": "UserPromptSubmit",
        "promptSubmit": "UserPromptSubmit",
        "Stop": "Stop",
        "stop": "Stop",
        "AgentStop": "Stop",
        "agentStop": "Stop",
        "SessionStart": "SessionStart",
        "sessionStart": "SessionStart",
        "PreTaskExecution": "PreTaskExec",
        "preTaskExecution": "PreTaskExec",
        "PreTaskExec": "PreTaskExec",
        "PostTaskExecution": "PostTaskExec",
        "postTaskExecution": "PostTaskExec",
        "PostTaskExec": "PostTaskExec",
        "PostFileCreate": "PostFileCreate",
        "PostFileSave": "PostFileSave",
        "PostFileDelete": "PostFileDelete",
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
    "kiro": "PascalCase",
}


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


def _validate_copilot_payload(payload: dict) -> list[str]:
    """Return native payload shape errors before any filesystem mutation."""
    errors: list[str] = []
    if payload.get("version") != 1:
        errors.append("top-level version must equal 1")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return [*errors, "top-level hooks must be an object"]
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            errors.append(f"hook event {event!r} must contain a list")
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"hook event {event!r} entry {index} must be an object")
                continue
            handlers = entry.get("hooks")
            if handlers is not None and (
                not isinstance(handlers, list)
                or not all(isinstance(handler, dict) for handler in handlers)
            ):
                errors.append(f"hook event {event!r} entry {index} handlers must be objects")
    return errors


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

_APM_HOOKS_SIDECAR = "apm-hooks.json"


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
        import json
        from collections import deque

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


def _relative_hook_script_bases(
    package_path: Path,
    hook_file_dir: Path | None,
) -> list[Path]:
    """Return candidate bases for resolving a relative hook script path."""
    bases: list[Path] = []
    if hook_file_dir is not None:
        bases.append(hook_file_dir)
    if package_path not in bases:
        bases.append(package_path)
    return bases


def _resolve_relative_hook_script(
    package_path: Path,
    hook_file_dir: Path | None,
    rel_path: str,
) -> Path | None:
    """Resolve a relative hook script path without escaping the package."""
    last_candidate: Path | None = None
    for base in _relative_hook_script_bases(package_path, hook_file_dir):
        try:
            candidate = ensure_path_within(base / rel_path, package_path)
        except PathTraversalError:
            continue
        last_candidate = candidate
        if candidate.exists() and candidate.is_file():
            return candidate
    return last_candidate


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

    def __init__(self) -> None:
        """Initialize per-install hook integration state."""
        super().__init__()
        self._deprecated_hook_routing_warnings: set[str] = set()

    @staticmethod
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
                for key in HookIntegrator.HOOK_COMMAND_KEYS:
                    value = matcher.get(key)
                    if isinstance(value, str):
                        entries.append((event_name, {key: value}))
                nested_hooks = matcher.get("hooks", [])
                if not isinstance(nested_hooks, list):
                    continue
                for hook in nested_hooks:
                    if not isinstance(hook, dict):
                        continue
                    for key in HookIntegrator.HOOK_COMMAND_KEYS:
                        value = hook.get(key)
                        if isinstance(value, str):
                            entries.append((event_name, {key: value}))
        return entries

    @staticmethod
    def _summarize_command(entry: dict) -> str:
        """Return a human-readable summary for a single hook command entry."""
        command = ""
        for key in HookIntegrator.HOOK_COMMAND_KEYS:
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
        self,
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
        for event_name, entry in self._iter_hook_entries(rewritten):
            actions.append(
                {
                    "event": event_name,
                    "summary": self._summarize_command(entry),
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
        hook_files: list[Path] = []
        seen_stems: set[str] = set()

        # Search in .apm/hooks/ (APM convention)
        apm_hooks = package_path / ".apm" / "hooks"
        if apm_hooks.exists():
            for f in sorted(apm_hooks.glob("*.json")):
                if f.is_symlink():
                    continue
                stem_key = f.stem.lower()
                if stem_key not in seen_stems:
                    seen_stems.add(stem_key)
                    hook_files.append(f)

        # Search in hooks/ (Claude-native convention)
        hooks_dir = package_path / "hooks"
        if hooks_dir.exists():
            for f in sorted(hooks_dir.glob("*.json")):
                if f.is_symlink():
                    continue
                stem_key = f.stem.lower()
                if stem_key not in seen_stems:
                    seen_stems.add(stem_key)
                    hook_files.append(f)

        return hook_files

    def _parse_hook_json(self, hook_file: Path) -> dict | None:
        """Parse a hook JSON file and return the data dict.

        Accepts both the wrapped format (``{"hooks": {EventName: [...]}}``)
        and the "naked" Claude-settings hooks-slice format
        (``{EventName: [...], ...}`` with no outer ``"hooks":`` wrap).
        The naked shape is what Claude Code accepts inside its own
        ``settings.json`` and is a common authoring pattern -- silently
        dropping it produced the empty merge reported in microsoft/apm#1499.

        Args:
            hook_file: Path to the hook JSON file

        Returns:
            Optional[Dict]: Parsed JSON dict (always wrapped), or None if invalid
        """
        try:
            with open(hook_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            # Normalise naked-format files (no outer "hooks" key but
            # every top-level value is a list of matcher entries) into
            # the wrapped shape downstream code expects.  Only promote
            # when ALL values look like hook entry arrays -- a stray
            # scalar (e.g. "description") would mean this is malformed
            # rather than naked, so leave it alone.
            if "hooks" not in data and data and all(isinstance(v, list) for v in data.values()):
                _log.debug(
                    "Promoted naked-format hook file %s (top-level event keys: %s) to wrapped shape",
                    hook_file,
                    sorted(data.keys()),
                )
                data = {"hooks": data}
            # Fail closed on malformed shapes where "hooks" is present but not
            # a dict (e.g. {"hooks": []}).  Downstream code calls .items() on
            # this value and would otherwise raise AttributeError mid-merge.
            if "hooks" in data and not isinstance(data["hooks"], dict):
                _log.warning(
                    "Skipping malformed hook file %s: 'hooks' must be a dict, got %s",
                    hook_file,
                    type(data["hooks"]).__name__,
                )
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
        hook_file_dir: Path | None = None,
        root_dir: str | None = None,
        deploy_root: Path | None = None,
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
                    str((deploy_root / target_rel).resolve())
                    if deploy_root is not None
                    else target_rel
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
                _rich_warning(f"Hook script not found: {source_file}")
                if deploy_root is not None:
                    new_command = new_command.replace(full_var, str(source_file))

        # Handle relative ./path and .\path references (safe to run after
        # ${CLAUDE_PLUGIN_ROOT} substitution since replacements produce paths
        # like ".github/..." not "./" or ".\")
        # Match both forward-slash and backslash separators (Windows hook JSON
        # may use backslashes: .\scripts\scan.ps1)
        rel_pattern = r"(\.[\\/][^\s\"']+)"
        for match in re.finditer(rel_pattern, new_command):
            rel_ref = match.group(1)
            # Normalize to forward slashes for path resolution
            rel_path = rel_ref[2:].replace("\\", "/")

            source_file = _resolve_relative_hook_script(package_path, hook_file_dir, rel_path)
            if source_file is None:
                continue
            if source_file.exists() and source_file.is_file():
                target_rel = f"{scripts_base}/{rel_path}"
                scripts_to_copy.append((source_file, target_rel))
                resolved_cmd = (
                    str((deploy_root / target_rel).resolve())
                    if deploy_root is not None
                    else target_rel
                )
                new_command = new_command.replace(rel_ref, resolved_cmd)
            else:
                # File absent: always warn (see ${PLUGIN_ROOT} branch above
                # for the project-scope vs user-scope rationale).
                _rich_warning(f"Hook script not found: {source_file}")
                if deploy_root is not None:
                    new_command = new_command.replace(rel_ref, str(source_file))

        return new_command, scripts_to_copy

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
                # Rewrite script paths in the matcher dict itself
                # (GitHub Copilot flat format: bash/powershell/windows keys at this level)
                for key in self.HOOK_COMMAND_KEYS:
                    if key in matcher:
                        new_cmd, scripts = self._rewrite_command_for_target(
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
                    for key in self.HOOK_COMMAND_KEYS:
                        if key in hook:
                            new_cmd, scripts = self._rewrite_command_for_target(
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

    @staticmethod
    def _root_local_identity_root(package_info, project_root: Path | None) -> Path | None:
        """Return the project root used to identify root-local packages."""
        return getattr(package_info, "root_local_project_root", None) or project_root

    @staticmethod
    def _is_root_local_package(package_info, project_root: Path | None) -> bool:
        """Return True when *package_info* represents the project's own .apm content."""
        identity_root = HookIntegrator._root_local_identity_root(package_info, project_root)
        if identity_root is None:
            return False
        try:
            return Path(package_info.install_path).resolve() == Path(identity_root).resolve()
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _safe_source_name(value: str | None, fallback: str = "_local") -> str:
        """Return a stable source marker that is also safe for hook script paths."""
        if not isinstance(value, str) or not value:
            return fallback
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
        # Collapse any run of 2+ dots to a single dot before stripping edges.
        # Embedded sequences like "foo..bar" would otherwise pass through the
        # earlier guard and reach downstream Path joins as a parent-dir hop.
        safe = re.sub(r"\.{2,}", ".", safe).strip(".-_")
        if not safe or safe in {".", ".."}:
            return fallback
        return safe

    @staticmethod
    def _get_root_local_package_name(package_info, project_root: Path) -> str:
        """Get the stable source marker for root .apm content."""
        apm_yml = Path(project_root) / "apm.yml"
        if apm_yml.exists():
            try:
                from apm_cli.utils.yaml_io import load_yaml

                data = load_yaml(apm_yml)
                if isinstance(data, dict):
                    manifest_name = HookIntegrator._safe_source_name(data.get("name"))
                    if manifest_name != "_local":
                        return manifest_name
            except (OSError, ValueError, yaml.YAMLError) as exc:
                _log.debug(
                    "Hook integrator: apm.yml manifest unreadable for %s (%s: %s), "
                    "falling back to install_path basename",
                    project_root,
                    exc.__class__.__name__,
                    exc,
                )

        package = getattr(package_info, "package", None)
        package_name = HookIntegrator._safe_source_name(getattr(package, "name", None))
        if package_name != "_local":
            return package_name
        return "_local"

    def _get_package_name(self, package_info, project_root: Path | None = None) -> str:
        """Get a short package name for use in file/directory naming.

        Args:
            package_info: PackageInfo object
            project_root: When provided and the package is the project root,
                reads ``apm.yml`` ``name`` for a stable source marker instead
                of falling back to ``install_path.name`` (which drifts on
                directory renames and worktrees). See #1329.

        Returns:
            str: Package name used as hook source marker and script namespace
        """
        if self._is_root_local_package(package_info, project_root):
            identity_root = HookIntegrator._root_local_identity_root(package_info, project_root)
            return HookIntegrator._get_root_local_package_name(package_info, Path(identity_root))
        return package_info.install_path.name

    @staticmethod
    def _get_hook_source_marker(
        package_info,
        project_root: Path,
        package_name: str,
    ) -> str:
        """Get the marker stored in merged hook JSON for ownership cleanup."""
        if HookIntegrator._is_root_local_package(package_info, project_root):
            if package_name == "_local":
                return "_local"
            return f"_local/{package_name}"
        return package_name

    @staticmethod
    def _hook_entry_content_key(entry: dict) -> str:
        """Build a stable comparison key excluding APM ownership metadata."""
        comparable = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
        return json.dumps(comparable, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _dependency_hook_sources(project_root: Path) -> set[str]:
        """Return source markers that correspond to installed dependency dirs."""
        apm_modules = project_root / "apm_modules"
        if not apm_modules.is_dir():
            return set()

        lockfile_paths, lockfile_readable = HookIntegrator._lockfile_dependency_paths(project_root)
        if lockfile_readable:
            sources: set[str] = set()
            for rel_path in lockfile_paths:
                package_path = HookIntegrator._safe_dependency_path(apm_modules, rel_path)
                if package_path is None:
                    continue
                HookIntegrator._add_dependency_source(sources, package_path)
            return sources

        return HookIntegrator._bounded_dependency_hook_sources(apm_modules)

    @staticmethod
    def _lockfile_dependency_paths(project_root: Path) -> tuple[list[str], bool]:
        """Return installed dependency paths from a readable lockfile, if present."""
        try:
            from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, LockFile, get_lockfile_path

            lockfile_path = get_lockfile_path(project_root)
            if not lockfile_path.exists():
                legacy_path = project_root / LEGACY_LOCKFILE_NAME
                if legacy_path.exists():
                    lockfile_path = legacy_path
            if not lockfile_path.exists():
                return [], False
            lockfile = LockFile.read(lockfile_path)
            if lockfile is None:
                return [], False
            return lockfile.get_installed_paths(project_root / "apm_modules"), True
        except (AttributeError, OSError, TypeError, ValueError, KeyError):
            return [], False

    @staticmethod
    def _safe_dependency_path(apm_modules: Path, rel_path: str) -> Path | None:
        """Return a lockfile dependency path without escaping apm_modules."""
        try:
            validate_path_segments(
                rel_path,
                context="lockfile dependency path",
                reject_empty=True,
            )
            package_path = apm_modules / Path(rel_path)
            ensure_path_within(package_path, apm_modules)
            if HookIntegrator._has_symlink_component(apm_modules, package_path):
                return None
            return package_path
        except (OSError, PathTraversalError, RuntimeError, TypeError):
            return None

    @staticmethod
    def _has_symlink_component(apm_modules: Path, package_path: Path) -> bool:
        """Return True when any component below apm_modules is a symlink."""
        try:
            relative = package_path.relative_to(apm_modules)
            current = apm_modules
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return True
            return False
        except (OSError, ValueError):
            return True

    @staticmethod
    def _is_dependency_package_dir(path: Path) -> bool:
        """Return True when *path* looks like an installed package root."""
        try:
            hooks = path / "hooks"
            apm_hooks = path / ".apm" / "hooks"
            apm_yml = path / "apm.yml"
            skill_md = path / "SKILL.md"
            return (
                (hooks.is_dir() and not hooks.is_symlink())
                or (apm_hooks.is_dir() and not apm_hooks.is_symlink())
                or (apm_yml.is_file() and not apm_yml.is_symlink())
                or (skill_md.is_file() and not skill_md.is_symlink())
            )
        except OSError:
            return False

    @staticmethod
    def _add_dependency_source(sources: set[str], package_path: Path) -> bool:
        """Add package_path.name to sources when package_path is a package root."""
        try:
            if (
                not package_path.is_dir()
                or package_path.is_symlink()
                or not HookIntegrator._is_dependency_package_dir(package_path)
            ):
                return False
        except OSError:
            return False
        sources.add(package_path.name)
        return True

    @staticmethod
    def _child_dependency_dirs(path: Path) -> list[Path]:
        """Return direct non-hidden child dirs without following symlink roots."""
        try:
            if path.is_symlink() or not path.is_dir():
                return []
            return sorted(
                [
                    child
                    for child in path.iterdir()
                    if not child.is_symlink() and child.is_dir() and not child.name.startswith(".")
                ],
                key=lambda child: child.name,
            )
        except OSError:
            return []

    @staticmethod
    def _collect_known_subdirectory_sources(sources: set[str], repo_root: Path) -> None:
        """Collect dependency sources from known virtual subdirectory layouts."""
        for namespace in ("collections", "skills"):
            for package_path in HookIntegrator._child_dependency_dirs(repo_root / namespace):
                HookIntegrator._add_dependency_source(sources, package_path)

        apm_dir = repo_root / ".apm"
        try:
            if apm_dir.is_symlink() or not apm_dir.is_dir():
                return
        except OSError:
            return
        for primitive in ("agents", "commands", "hooks", "instructions", "prompts", "skills"):
            for package_path in HookIntegrator._child_dependency_dirs(apm_dir / primitive):
                HookIntegrator._add_dependency_source(sources, package_path)

    @staticmethod
    def _collect_remote_dependency_sources(sources: set[str], namespace: Path) -> None:
        """Collect fallback sources from explicit remote install layouts."""
        if HookIntegrator._add_dependency_source(sources, namespace):
            return

        for repo_or_project in HookIntegrator._child_dependency_dirs(namespace):
            if HookIntegrator._add_dependency_source(sources, repo_or_project):
                continue

            HookIntegrator._collect_known_subdirectory_sources(sources, repo_or_project)

            for ado_repo in HookIntegrator._child_dependency_dirs(repo_or_project):
                if HookIntegrator._add_dependency_source(sources, ado_repo):
                    continue
                HookIntegrator._collect_known_subdirectory_sources(sources, ado_repo)

    @staticmethod
    def _collect_local_dependency_sources(sources: set[str], local_namespace: Path) -> None:
        """Collect apm_modules/_local/<name> package roots only."""
        for local_package in HookIntegrator._child_dependency_dirs(local_namespace):
            HookIntegrator._add_dependency_source(sources, local_package)

    @staticmethod
    def _bounded_dependency_hook_sources(apm_modules: Path) -> set[str]:
        """Fallback source scan limited to known apm_modules package layouts."""
        sources: set[str] = set()

        for package_root in HookIntegrator._child_dependency_dirs(apm_modules):
            if package_root.name == "_local":
                HookIntegrator._collect_local_dependency_sources(sources, package_root)
                continue

            HookIntegrator._collect_remote_dependency_sources(sources, package_root)
        return sources

    @staticmethod
    def _should_remove_prior_merged_entry(
        entry,
        *,
        source_marker: str,
        fresh_content_keys: set[str],
        heal_stale_root_source: bool,
        dependency_sources: set[str],
        remove_current_source: bool,
    ) -> bool:
        """Return True when an existing merged-hook entry should be replaced."""
        if not isinstance(entry, dict):
            return False
        source = entry.get("_apm_source")
        if remove_current_source and source == source_marker:
            return True
        if not heal_stale_root_source or not source or source in dependency_sources:
            return False
        return HookIntegrator._hook_entry_content_key(entry) in fresh_content_keys

    def integrate_package_hooks(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        target=None,
        dep_targets_active: bool = False,
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
        package_name = self._get_package_name(package_info, project_root)
        if not dep_targets_active:
            hook_files = _filter_hook_files_for_target(
                hook_files,
                "copilot",
                package_name=package_name,
                warned_packages=self._deprecated_hook_routing_warnings,
                package_identity=package_info.get_canonical_dependency_string(),
            )

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

        hooks_integrated = 0
        scripts_copied = 0
        scripts_adopted = 0
        target_paths: list[Path] = []
        display_payloads: list = []
        materializations: list[MaterializationResult] = []

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            # Rewrite script paths for VSCode target
            rewritten, scripts = self._rewrite_hooks_data(
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

            hooks = rewritten.get("hooks", {})
            event_map = _HOOK_EVENT_MAP.get("copilot", {})
            _emit_hook_event_diagnostics(list(hooks.keys()), "copilot", event_map)
            if isinstance(hooks, dict):
                renamed_hooks = {}
                for raw_event_name, entries in hooks.items():
                    event_name = event_map.get(raw_event_name, raw_event_name)
                    if event_name in renamed_hooks and isinstance(renamed_hooks[event_name], list):
                        if isinstance(entries, list):
                            renamed_hooks[event_name].extend(entries)
                            continue
                    renamed_hooks[event_name] = entries
                rewritten["hooks"] = renamed_hooks

            rewritten.setdefault("version", 1)
            errors = _validate_copilot_payload(rewritten)
            validation = NativePayloadValidation(
                valid=not errors,
                contract="copilot-hooks-v1",
                errors=tuple(errors),
            )
            if not validation.valid:
                if diagnostics is not None:
                    diagnostics.error(
                        f"Invalid Copilot hook payload for {rel_path}",
                        package=package_name,
                        detail="; ".join(validation.errors),
                    )
                from apm_cli.integration.targets import KNOWN_TARGETS

                materializations.append(
                    MaterializationResult(
                        locator=DeploymentLedgerCodec.locator_for_path(
                            target_path,
                            project_root=project_root,
                            target=KNOWN_TARGETS["copilot"],
                            scope=InstallScope.PROJECT,
                        ),
                        owners=frozenset({package_info.get_canonical_dependency_string()}),
                        status=MaterializationStatus.FAILED,
                        content_hash=None,
                        validation=validation,
                    )
                )
                continue

            # Write rewritten JSON
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(rewritten, f, indent=2)
                f.write("\n")

            hooks_integrated += 1
            target_paths.append(target_path)
            from apm_cli.integration.targets import KNOWN_TARGETS
            from apm_cli.utils.content_hash import compute_file_hash

            materializations.append(
                MaterializationResult(
                    locator=DeploymentLedgerCodec.locator_for_path(
                        target_path,
                        project_root=project_root,
                        target=KNOWN_TARGETS["copilot"],
                        scope=InstallScope.PROJECT,
                    ),
                    owners=frozenset({package_info.get_canonical_dependency_string()}),
                    status=(
                        MaterializationStatus.WRITTEN
                        if validation.valid
                        else MaterializationStatus.FAILED
                    ),
                    content_hash=compute_file_hash(target_path),
                    validation=validation,
                )
            )
            display_payloads.append(
                self._build_display_payload(
                    f"{root_dir}/hooks/",
                    target_filename,
                    hook_file,
                    rewritten,
                )
            )

            copy_result = copy_deployed_hook_bundle(
                self,
                package_path=package_info.install_path,
                hook_file_dir=hook_file.parent,
                project_root=project_root,
                scripts=scripts,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
                hook_descriptor_files=set(hook_files),
            )
            scripts_copied += copy_result.scripts_copied
            scripts_adopted += copy_result.files_adopted

        return HookIntegrationResult(
            files_integrated=hooks_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            scripts_copied=scripts_copied,
            files_adopted=scripts_adopted,
            display_payloads=display_payloads,
            materializations=tuple(materializations),
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
        dep_targets_active: bool = False,
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
        container = config.event_container_key

        # Opt-in check: some targets only deploy when their dir exists
        if config.require_dir and not target_dir.exists():
            return _empty

        # Absolutize hook commands only for user-scope deploys.  Claude
        # Code (and the Codex/Cursor/Gemini equivalents) reads
        # ``~/.claude/settings.json`` without a fixed cwd and does not
        # expand ``${CLAUDE_PLUGIN_ROOT}`` in that file (see #1310 / #1354),
        # so user-scope deploys must write absolute paths.  Project-scope
        # ``<repo>/.claude/settings.json`` is typically checked in and runs
        # with cwd at the repo root, where repo-relative paths resolve
        # correctly -- baking absolute machine paths into checked-in config
        # breaks portability across clones, contributors, and CI (#1394).
        # ``user_scope`` is threaded from the caller's ``InstallScope`` so
        # the gate is explicit rather than inferred from deploy-root shape.
        _deploy_root_for_rewrite = project_root if user_scope else None

        hook_files = self.find_hook_files(package_info.install_path)
        package_name = self._get_package_name(package_info, project_root)
        if not dep_targets_active:
            hook_files = _filter_hook_files_for_target(
                hook_files,
                config.target_key,
                package_name=package_name,
                warned_packages=self._deprecated_hook_routing_warnings,
                package_identity=package_info.get_canonical_dependency_string(),
            )
        if not hook_files:
            return _empty

        source_marker = self._get_hook_source_marker(package_info, project_root, package_name)
        heal_stale_root_source = self._is_root_local_package(package_info, project_root)
        dependency_sources = (
            self._dependency_hook_sources(project_root) if heal_stale_root_source else set()
        )
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
        # Events whose prior-owned entries have already been cleared on
        # this install run. Packages can contribute to the same event
        # from multiple hook files -- we must only strip once so earlier
        # files' fresh entries aren't wiped by later iterations.
        cleared_events: set = set()

        # Read existing JSON config
        json_path = target_dir / config.config_filename
        json_config: dict = {}
        if json_path.exists():
            try:
                with open(json_path, encoding="utf-8") as f:
                    json_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                json_config = {}

        # Load external ownership metadata before reconciling native entries.
        sidecar_path = target_dir / _APM_HOOKS_SIDECAR
        sidecar_data: dict = {}
        if config.schema_strict and sidecar_path.exists():
            try:
                with open(sidecar_path, encoding="utf-8") as f:
                    _raw = json.load(f)
                if isinstance(_raw, dict):
                    sidecar_data = _raw
                else:
                    _log.warning(
                        "Sidecar file %s contains non-dict JSON; treating as empty.",
                        sidecar_path,
                    )
                    sidecar_data = {}
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Failed to read sidecar %s: %s; treating as empty.", sidecar_path, exc)
                sidecar_data = {}

            # Re-inject _apm_source from sidecar into matching in-memory entries
            if sidecar_data and container in json_config:
                _reinject_apm_source_from_sidecar(json_config[container], sidecar_data)

        # Top-level container key for the merged event map.  Most targets
        # use "hooks"; Antigravity nests its events under the reserved
        # hook-name "apm" so sibling user hook-names are preserved.  Only
        # the container key is created so non-"hooks" targets never gain a
        # stray empty "hooks" object in their native file.
        if container not in json_config:
            json_config[container] = {}
            _log.debug("Seeded hook container '%s' in %s", container, config.config_filename)

        # Inject any target-specific top-level defaults (e.g. "version": 1 for
        # Cursor) that are absent from the existing file.  Existing values are
        # never overwritten so a user-set "version" is preserved across reinstalls.
        injected_keys: list[str] = []
        for key, value in config.top_level_defaults.items():
            if key not in json_config:
                json_config[key] = value
                injected_keys.append(key)
        if injected_keys:
            _log.debug(
                "Injected top_level_defaults into %s: %s",
                config.config_filename,
                injected_keys,
            )

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            # Rewrite script paths for the target
            rewritten, scripts = self._rewrite_hooks_data(
                data,
                package_info.install_path,
                package_name,
                config.target_key,
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
                deploy_root=_deploy_root_for_rewrite,
            )

            # Merge hooks into config (additive)
            hooks = rewritten.get("hooks", {})
            event_map = _HOOK_EVENT_MAP.get(config.target_key, {})

            _emit_hook_event_diagnostics(list(hooks.keys()), config.target_key, event_map)

            # Build reverse map: normalised name -> set of source aliases
            reverse_map: dict[str, set[str]] = {}
            for source_name, norm_name in event_map.items():
                reverse_map.setdefault(norm_name, set()).add(source_name)

            entries_appended_for_file = False
            file_event_entries: dict = {}
            for raw_event_name, entries in hooks.items():
                if not isinstance(entries, list) or not entries:
                    continue
                event_name = event_map.get(raw_event_name, raw_event_name)
                if event_name not in json_config[container]:
                    json_config[container][event_name] = []

                # Transform flat Copilot entries to the target's nested /
                # native hook shape.
                if config.target_key == "claude":
                    entries = _to_claude_hook_entries(entries)
                elif config.target_key == "gemini":
                    entries = _to_gemini_hook_entries(entries)
                elif config.target_key == "antigravity":
                    entries = _to_antigravity_hook_entries(entries, event_name)

                # Mark each entry with APM source for sync/cleanup
                for entry in entries:
                    if isinstance(entry, dict):
                        entry["_apm_source"] = source_marker
                fresh_content_keys = {
                    self._hook_entry_content_key(entry)
                    for entry in entries
                    if isinstance(entry, dict)
                }

                # Idempotent upsert: drop any prior entries owned by this
                # package before appending fresh ones. Without this, every
                # `apm install` re-run duplicates the package's hooks
                # because `.extend()` is unconditional. See microsoft/apm#708.
                # Only strip once per event per install run -- a package
                # with multiple hook files targeting the same event
                # contributes each file's entries in turn, and stripping
                # on every iteration would erase earlier files' work.
                remove_current_source = event_name not in cleared_events
                if remove_current_source or heal_stale_root_source:
                    # Clear from the normalised event
                    prior_entries = json_config[container][event_name]
                    kept_entries = [
                        e
                        for e in prior_entries
                        if not self._should_remove_prior_merged_entry(
                            e,
                            source_marker=source_marker,
                            fresh_content_keys=fresh_content_keys,
                            heal_stale_root_source=heal_stale_root_source,
                            dependency_sources=dependency_sources,
                            remove_current_source=remove_current_source,
                        )
                    ]
                    if heal_stale_root_source:
                        kept_ids = {id(e) for e in kept_entries}
                        healed = sum(
                            1
                            for e in prior_entries
                            if isinstance(e, dict)
                            and e.get("_apm_source")
                            and e.get("_apm_source") != source_marker
                            and e.get("_apm_source") not in dependency_sources
                            and id(e) not in kept_ids
                        )
                        if healed:
                            _log.debug(
                                "Hook integrator: healed %d stale same-content "
                                "merged hook entries for source %s in event %s",
                                healed,
                                source_marker,
                                event_name,
                            )
                    json_config[container][event_name] = kept_entries
                    # Also clear from any alias events that map to
                    # this normalised name (handles migration from
                    # corrupted installs with mixed-case event keys).
                    for alias in reverse_map.get(event_name, set()):
                        if alias != event_name and alias in json_config[container]:
                            json_config[container][alias] = [
                                e
                                for e in json_config[container][alias]
                                if not self._should_remove_prior_merged_entry(
                                    e,
                                    source_marker=source_marker,
                                    fresh_content_keys=fresh_content_keys,
                                    heal_stale_root_source=heal_stale_root_source,
                                    dependency_sources=dependency_sources,
                                    remove_current_source=remove_current_source,
                                )
                            ]
                            # Remove the alias key entirely if now empty
                            if not json_config[container][alias]:
                                del json_config[container][alias]
                    cleared_events.add(event_name)
                json_config[container][event_name].extend(entries)

                # Deduplicate same-package entries by content.
                # Safety net for edge cases where multiple source files
                # produce semantically identical entries.
                import json as _json

                seen_keys: set[str] = set()
                deduped: list = []
                for entry in json_config[container][event_name]:
                    if not isinstance(entry, dict):
                        deduped.append(entry)
                        continue
                    cmp = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
                    source = entry.get("_apm_source")
                    dedup_key = _json.dumps({"s": source, "c": cmp}, sort_keys=True)
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        deduped.append(entry)
                json_config[container][event_name] = deduped
                entries_appended_for_file = True
                # Capture the actual entry objects this file contributed to
                # the merged config. They are the same dict references that
                # the schema-strict strip mutates in place below, so building
                # the display payload from them after finalization yields
                # rendered_json that matches the on-disk/executed content
                # (Gemini-transformed, _apm_source stripped where required).
                file_event_entries.setdefault(event_name, []).extend(
                    e for e in entries if isinstance(e, dict)
                )

            if entries_appended_for_file:
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
                # Diagnostic for the fail-closed silent-skip path introduced
                # by the integrated-counter fix (microsoft/apm#1499): a hook
                # file that parsed cleanly but contributed zero entries (all
                # events empty / non-list) used to bump the counter and lie
                # to the user.  Now we skip it -- emit a user-visible warning
                # (the original #1499 symptom was that authors saw nothing
                # bad AND nothing good, so a structured-logger-only message
                # would re-introduce the silent-failure UX) and a parallel
                # _log.warning for operators consuming structured logs.
                rel_hook = hook_file.name
                _rich_warning(
                    f"Hook file {rel_hook} contributed no entries to "
                    f"{config.target_key} settings; skipped."
                )
                _log.warning(
                    "Hook file %s contributed no entries to %s settings "
                    "(all events empty or non-list); skipping.",
                    hook_file,
                    config.target_key,
                )

            copy_result = copy_deployed_hook_bundle(
                self,
                package_path=package_info.install_path,
                hook_file_dir=hook_file.parent,
                project_root=project_root,
                scripts=scripts,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
                hook_descriptor_files=set(hook_files),
            )
            scripts_copied += copy_result.scripts_copied
            scripts_adopted += copy_result.files_adopted

        # Write JSON config back
        # Don't track the config file in target_paths -- it's a shared
        # file cleaned via _apm_source markers, not file-level deletion
        json_path.parent.mkdir(parents=True, exist_ok=True)

        if config.schema_strict:
            # Build sidecar from entries that have _apm_source
            sidecar_out: dict = {}
            for event_name, entries_list in json_config.get(container, {}).items():
                if not isinstance(entries_list, list):
                    continue
                owned = [e for e in entries_list if isinstance(e, dict) and "_apm_source" in e]
                if owned:
                    sidecar_out[event_name] = [dict(e) for e in owned]

            # Strip _apm_source from entries before writing to disk
            for entries_list in json_config.get(container, {}).values():
                if isinstance(entries_list, list):
                    for entry in entries_list:
                        if isinstance(entry, dict):
                            entry.pop("_apm_source", None)

            # Write sidecar
            sidecar_path = target_dir / _APM_HOOKS_SIDECAR
            if sidecar_out:
                atomic_write_text(
                    sidecar_path,
                    json.dumps(sidecar_out, indent=2) + "\n",
                )
            elif sidecar_path.exists():
                sidecar_path.unlink()

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

        # Write the (now schema-clean) config
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_config, f, indent=2)
            f.write("\n")

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
        dep_targets_active: bool = False,
        allowed_targets: set[str] | None = None,
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
        if dep_targets_active and (not allowed_targets or target.name not in allowed_targets):
            raise AssertionError(f"BUG: target {target.name} bypassed chokepoint filter")

        if target.name == "copilot":
            return self.integrate_package_hooks(
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                dep_targets_active=dep_targets_active,
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
                dep_targets_active=dep_targets_active,
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
                dep_targets_active=dep_targets_active,
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

        # Clean APM entries from merged-hook JSON configs using external ownership.
        for t in source:
            config = _MERGE_HOOK_TARGETS.get(t.name)
            if config is not None:
                json_path = project_root / t.root_dir / config.config_filename
                self._clean_apm_entries_from_json(
                    json_path,
                    stats,
                    container=config.event_container_key,
                    sidecar_path=json_path.parent / _APM_HOOKS_SIDECAR,
                )

        return stats

    @staticmethod
    def _clean_apm_entries_from_json(
        json_path: Path,
        stats: dict[str, int],
        container: str = "hooks",
        sidecar_path: Path | None = None,
    ) -> None:
        """Remove externally-owned entries from a native hooks JSON file.

        Filters out entries with ``_apm_source`` markers and cleans up
        empty event arrays and the *container* key itself.  *container*
        defaults to ``"hooks"``; Antigravity passes ``"apm"`` (its reserved
        hook-name container) so sibling user hook-names are left intact.
        """
        if not json_path.exists():
            return
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)

            if container not in data:
                if sidecar_path is not None and sidecar_path.exists():
                    sidecar_path.unlink()
                return

            if sidecar_path is not None and sidecar_path.exists():
                with open(sidecar_path, encoding="utf-8") as f:
                    sidecar_data = json.load(f)
                if isinstance(sidecar_data, dict):
                    _reinject_apm_source_from_sidecar(data[container], sidecar_data)

            modified = False
            for event_name in list(data[container].keys()):
                entries = data[container][event_name]
                if isinstance(entries, list):
                    filtered = [
                        e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)
                    ]
                    if len(filtered) != len(entries):
                        modified = True
                    data[container][event_name] = filtered
                    if not filtered:
                        del data[container][event_name]

            if not data[container]:
                del data[container]

            if modified:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                stats["files_removed"] += 1
            if sidecar_path is not None and sidecar_path.exists():
                sidecar_path.unlink()
        except (json.JSONDecodeError, OSError):
            stats["errors"] += 1
