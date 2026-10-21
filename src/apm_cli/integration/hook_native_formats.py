"""Native hook schema adapters around the vendor-neutral hook IR."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from apm_cli.hook_contract import (
    HOOK_COMMAND_KEYS,
    HookContractError,
    HookDocument,
    HookHandler,
    _entries_to_ir,
    _handler_to_ir,
    _thaw,
    hook_handlers,
    parse_hook_source,
)
from apm_cli.utils.diagnostics import printable_ascii_text

_ANTIGRAVITY_NESTED_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})

# https://cursor.com/docs/hooks (native events, not case-derived aliases).
CURSOR_NATIVE_EVENTS = frozenset(
    {
        "sessionStart",
        "sessionEnd",
        "preToolUse",
        "postToolUse",
        "postToolUseFailure",
        "subagentStart",
        "subagentStop",
        "beforeShellExecution",
        "afterShellExecution",
        "beforeMCPExecution",
        "afterMCPExecution",
        "beforeReadFile",
        "afterFileEdit",
        "beforeSubmitPrompt",
        "preCompact",
        "stop",
        "afterAgentResponse",
        "afterAgentThought",
        "beforeTabFileRead",
        "afterTabFileEdit",
        "workspaceOpen",
    }
)

# https://cursor.com/docs/hooks - the only keys Cursor's loader recognizes.
CURSOR_CONFIG_TOP_LEVEL_KEYS = frozenset({"version", "hooks"})

# Primary contracts retrieved 2026-10-21:
# https://code.claude.com/docs/en/hooks
# github/docs content/copilot/reference/hooks-reference.md blob
# 0be7a2c689786017602bd8201cb38620b717843b. Copilot casing selects
# payload and matcher semantics, so native aliases must not be renamed.
CLAUDE_NATIVE_EVENTS = frozenset(
    {
        "SessionStart",
        "Setup",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "DirectoryAdded",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "Elicitation",
        "ElicitationResult",
        "SessionEnd",
    }
)
COPILOT_NATIVE_EVENTS = frozenset(
    {
        "sessionStart",
        "SessionStart",
        "sessionEnd",
        "SessionEnd",
        "userPromptSubmitted",
        "UserPromptSubmit",
        "userPromptTransformed",
        "preToolUse",
        "PreToolUse",
        "postToolUse",
        "PostToolUse",
        "postToolUseFailure",
        "PostToolUseFailure",
        "agentStop",
        "Stop",
        "subagentStart",
        "subagentStop",
        "SubagentStop",
        "errorOccurred",
        "ErrorOccurred",
        "preCompact",
        "PreCompact",
        "permissionRequest",
        "PermissionRequest",
        "notification",
        "Notification",
    }
)
_CLAUDE_TOOL_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"}
)
_CLAUDE_UNMATCHED_EVENTS = frozenset(
    {
        "CwdChanged",
        "UserPromptSubmit",
        "PostToolBatch",
        "Stop",
        "TeammateIdle",
        "TaskCreated",
        "TaskCompleted",
        "WorktreeCreate",
        "WorktreeRemove",
        "MessageDisplay",
    }
)
_CLAUDE_EVALUATION_EVENTS = frozenset(
    {
        "PermissionDenied",
        "PostToolBatch",
        "PostToolUse",
        "PostToolUseFailure",
        "PreToolUse",
        "Stop",
        "SubagentStop",
        "TaskCompleted",
        "TaskCreated",
        "TeammateIdle",
        "UserPromptExpansion",
        "UserPromptSubmit",
    }
)


def _validate_native_handler(
    handler: dict,
    target: str,
    event: str,
    placement: Literal["settings", "skill", "agent"],
) -> None:
    """Reject invocation fields that the selected native contract cannot preserve."""
    kind = handler.get("type", "command")
    common = {"type", "timeout", "_apm_source"}
    if target == "claude":
        common |= {"statusMessage", "if", "once"}
        fields = {
            "command": {"command", "args", "async", "asyncRewake", "shell", "onFailure"},
            "prompt": {"prompt", "model"},
            "agent": {"prompt", "model"},
            "http": {"url", "headers", "allowedEnvVars", "onFailure"},
            "mcp_tool": {"server", "tool", "input"},
        }
    else:
        common |= {"timeoutSec", "matcher"}
        fields = {
            "command": {"command", "bash", "powershell", "cwd", "env"},
            "http": {"url", "headers", "allowedEnvVars"},
            "prompt": {"prompt"},
        }
    if not isinstance(kind, str) or kind not in fields:
        raise HookContractError(f"unsupported {target} hook handler type {kind!r}")
    unknown = handler.keys() - common - fields[kind]
    if unknown:
        raise HookContractError(
            f"unsupported {target} handler fields {sorted(unknown)!r}; no conditions were discarded"
        )
    required = {
        "command": ("command",) if target == "claude" else ("command", "bash", "powershell"),
        "prompt": ("prompt",),
        "agent": ("prompt",),
        "http": ("url",),
        "mcp_tool": ("server",),
    }[kind]
    if not any(isinstance(handler.get(key), str) and handler[key].strip() for key in required):
        raise HookContractError(f"{target} {kind} hook requires a nonempty {required!r}")
    for key in (*required, "matcher", "if", "shell", "model", "statusMessage", "cwd", "onFailure"):
        if key in handler and not isinstance(handler[key], str):
            raise HookContractError(f"{target} hook {key} must be a string")
    for key in ("timeout", "timeoutSec"):
        if key in handler and (
            type(handler[key]) not in (int, float)
            or not math.isfinite(handler[key])
            or handler[key] <= 0
        ):
            raise HookContractError(f"{target} hook {key} must be finite positive seconds")
    for key in ("once", "async", "asyncRewake"):
        if key in handler and type(handler[key]) is not bool:
            raise HookContractError(f"{target} hook {key} must be a boolean")
    for key in ("env", "headers"):
        if key in handler and (
            not isinstance(handler[key], dict)
            or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in handler[key].items()
            )
        ):
            raise HookContractError(f"{target} hook {key} must map strings to strings")
    for key in ("args", "allowedEnvVars"):
        if key in handler and (
            not isinstance(handler[key], list)
            or not all(isinstance(value, str) for value in handler[key])
        ):
            raise HookContractError(f"{target} hook {key} must be an array of strings")
    if "args" in handler and any(
        "/" in value or "\\" in value or "$" in value
        for value in [handler.get("command", ""), *handler["args"]]
    ):
        raise HookContractError(
            "hook args with resource paths cannot be relocated faithfully; "
            "use a package script in a command hook instead"
        )
    if "args" in handler and any(char.isspace() for char in handler["command"]):
        raise HookContractError("exec-form command must be one executable, not a shell expression")
    if kind == "http":
        try:
            url = urlparse(handler["url"])
        except ValueError as exc:
            raise HookContractError("HTTP hook URL is malformed") from exc
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise HookContractError("HTTP hook URL must have an http(s) scheme and host")
        if target == "copilot" and url.scheme != "https":
            raise HookContractError(
                "Copilot HTTP hooks require HTTPS; runtime-only localhost opt-ins are not projected"
            )
    if kind == "mcp_tool":
        if not isinstance(handler.get("tool"), str) or not handler["tool"].strip():
            raise HookContractError("MCP hook requires a nonempty tool name")
        if "input" in handler and not isinstance(handler["input"], dict):
            raise HookContractError("MCP hook input must be an object")
    if target == "claude":
        if kind in {"prompt", "agent"} and event not in _CLAUDE_EVALUATION_EVENTS:
            if kind != "prompt" or event != "PermissionRequest":
                raise HookContractError(f"Claude {event} does not support {kind} handlers")
        if event in {"SessionStart", "Setup"} and kind == "http":
            raise HookContractError(f"Claude {event} does not support HTTP handlers")
        if event == "Setup" and kind == "mcp_tool":
            raise HookContractError("Claude Setup runs before MCP servers are available")
        if (handler.get("once") and placement != "skill") or (
            "if" in handler and event not in _CLAUDE_TOOL_EVENTS
        ):
            raise HookContractError("Claude settings cannot preserve this once/if condition")
        if "shell" in handler and handler["shell"] not in {"bash", "powershell"}:
            raise HookContractError("unsupported Claude hook shell")
        if "onFailure" in handler and handler["onFailure"] not in {"continue", "block"}:
            raise HookContractError("unsupported Claude hook onFailure")
    elif kind == "prompt" and event not in {"sessionStart", "SessionStart"}:
        raise HookContractError("Copilot prompt handlers require sessionStart")


def render_selected_hook_document(
    document: dict,
    target: str,
    *,
    placement: Literal["settings", "skill", "agent"] = "settings",
) -> dict:
    """Render selected Claude/Copilot sources without guessing native semantics."""
    if target not in {"claude", "copilot"}:
        raise HookContractError(f"no selected hook contract for {target!r}")
    if placement not in {"settings", "skill", "agent"} or (
        target != "claude" and placement != "settings"
    ):
        raise HookContractError(f"unsupported {target} hook placement {placement!r}")
    parse_hook_source(document)
    if document.keys() - {"version", "hooks", "description"}:
        raise HookContractError(f"unsupported {target} hook document fields")
    if "version" in document and (type(document["version"]) is not int or document["version"] != 1):
        raise HookContractError(f"{target} hook document requires version 1")
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise HookContractError("selected hook document requires a hooks object")
    result = copy.deepcopy(document)
    native_events = CLAUDE_NATIVE_EVENTS if target == "claude" else COPILOT_NATIVE_EVENTS
    rendered: dict[str, list[dict]] = {}
    for event, entries in hooks.items():
        if event not in native_events:
            raise HookContractError(
                f"unsupported {target} event {event!r}; use that client's native event "
                "and payload contract in a target-specific package"
            )
        flat: list[dict] = []
        for entry in entries:
            if "hooks" not in entry:
                candidates = [copy.deepcopy(entry)]
            else:
                if entry.keys() - {"hooks", "matcher", "_apm_source"}:
                    raise HookContractError(f"unsupported {target} matcher-group fields")
                candidates = []
                for handler in entry["hooks"]:
                    if target == "claude" and "type" not in handler:
                        raise HookContractError("nested Claude handlers require an explicit type")
                    candidate = copy.deepcopy(handler)
                    if "matcher" in entry:
                        if "matcher" in candidate:
                            raise HookContractError("cannot combine nested and outer matchers")
                        candidate["matcher"] = entry["matcher"]
                    if "_apm_source" in entry:
                        candidate["_apm_source"] = entry["_apm_source"]
                    candidates.append(candidate)
            for candidate in candidates:
                matcher = candidate.get("matcher")
                if "matcher" in candidate and not isinstance(matcher, str):
                    raise HookContractError(f"{target} matcher must be a string")
                if target == "claude":
                    candidate.pop("matcher", None)
                    if event in _CLAUDE_UNMATCHED_EVENTS and matcher not in (None, "", "*"):
                        raise HookContractError(f"Claude {event} does not support a matcher")
                _validate_native_handler(candidate, target, event, placement)
                candidate.setdefault("type", "command")
                if target == "claude":
                    outer = {"matcher": matcher or "*", "hooks": [candidate]}
                    if "_apm_source" in candidate:
                        outer["_apm_source"] = candidate.pop("_apm_source")
                    flat.append(outer)
                else:
                    candidate.pop("_apm_source", None)
                    flat.append(candidate)
        rendered[event] = flat
    result["hooks"] = rendered
    if target == "copilot":
        result.setdefault("version", 1)
    return result


def render_selected_hook_source(data: dict | None, path: Path, target: str) -> dict:
    """Attach the selected descriptor identity and recovery action to native failures."""
    try:
        if data is None:
            raise HookContractError("invalid hook JSON")
        return render_selected_hook_document(data, target)
    except HookContractError as exc:
        raise HookContractError(
            f"Cannot install {target} hooks from {printable_ascii_text(path.name)}: "
            f"{printable_ascii_text(str(exc))}. No settings were replaced. "
            "Use supported native definitions in a target-specific package and reinstall."
        ) from exc


def _cursor_matcher(matcher: str | None, event: str, *, foreign: bool) -> str | None:
    """Translate only explicitly representable Claude tool-name alternatives."""
    if matcher is not None and not isinstance(matcher, str):
        raise HookContractError("Cursor matcher must be a string")
    if not foreign or matcher in (None, "", "*", ".*"):
        return matcher
    if event not in {"preToolUse", "postToolUse"}:
        raise HookContractError("Claude event matcher has no verified Cursor equivalent")
    names = matcher.split("|")
    mapping = {
        "Bash": "Shell",
        "Read": "Read",
        "Edit": "Write",
        "Write": "Write",
        "Grep": "Grep",
        "Task": "Task",
        "WebFetch": "WebFetch",
        "WebSearch": "WebSearch",
    }
    if any(name not in mapping for name in names):
        if all(name.strip() in mapping for name in names):
            raise HookContractError(
                "Claude matcher alternatives contain whitespace; use literal alternatives "
                "such as 'Bash|Read' instead of 'Bash | Read'"
            )
        raise HookContractError(
            "Cursor cannot preserve this Claude matcher; regex, Glob and server-qualified "
            "MCP translations are not supported"
        )
    # Cursor combines file creation and editing under Write. Mapping either
    # Claude tool alone would broaden the original restriction.
    if set(names) & {"Edit", "Write"} and not {"Edit", "Write"} <= set(names):
        raise HookContractError("Cursor Write requires both Claude Edit and Write alternatives")
    return "|".join(dict.fromkeys(mapping[name] for name in names))


def _check_cursor_field_types(entry: dict[str, Any]) -> None:
    """Keep source and rendered field predicates identical across IR conversion."""
    if "matcher" in entry and not isinstance(entry["matcher"], str):
        raise HookContractError("Cursor matcher must be a string")
    for key in ("timeout", "timeoutSec"):
        if key in entry and (
            type(entry[key]) not in (int, float) or not math.isfinite(entry[key]) or entry[key] <= 0
        ):
            raise HookContractError("Cursor timeout must be a finite positive number of seconds")
    if "failClosed" in entry and type(entry["failClosed"]) is not bool:
        raise HookContractError("Cursor failClosed must be a boolean")


def _validate_cursor_handler(entry: dict[str, Any], event: str) -> None:
    """Validate native output; source-only checks live in _to_cursor_hook_entries."""
    allowed = {
        "type",
        "command",
        "prompt",
        "model",
        "timeout",
        "matcher",
        "loop_limit",
        "failClosed",
        "_apm_source",
    }
    if entry.keys() - allowed:
        raise HookContractError(
            f"unsupported Cursor handler fields {sorted(entry.keys() - allowed)!r}; "
            "no fields were discarded"
        )
    _check_cursor_field_types(entry)
    if "_apm_source" in entry and not isinstance(entry["_apm_source"], str):
        raise HookContractError("invalid Cursor hook ownership metadata")
    kind = entry.get("type", "command")
    if not isinstance(kind, str) or kind not in {"command", "prompt"}:
        raise HookContractError("Cursor supports only command and prompt handlers")
    content_key = "prompt" if kind == "prompt" else "command"
    incompatible = {"command"} if kind == "prompt" else {"prompt", "model"}
    if incompatible & entry.keys() or not isinstance(entry.get(content_key), str):
        raise HookContractError(f"Cursor {kind} handler requires a string {content_key}")
    if "model" in entry and not isinstance(entry["model"], str):
        raise HookContractError("Cursor prompt model must be a string")
    if "loop_limit" in entry:
        limit = entry["loop_limit"]
        if event not in {"stop", "subagentStop"} or (
            limit is not None and (type(limit) is not int or limit < 0)
        ):
            raise HookContractError(
                "Cursor loop_limit requires stop/subagentStop and integer or null"
            )


def _to_cursor_hook_entries(
    entries: list, event_name: str, *, foreign: bool = False
) -> list[dict[str, Any]]:
    """Render the neutral IR as strict native Cursor flat handlers.

    Claude import mappings are documented at
    https://cursor.com/docs/reference/third-party-hooks. The bounded adapter
    rejects mappings that lose restrictions instead of imitating lossy import.
    Check source-only restrictions before IR normalization loses them, then
    validate rendered output through _validate_cursor_handler.
    """
    if event_name not in CURSOR_NATIVE_EVENTS:
        raise HookContractError(f"unsupported Cursor event {event_name!r}")
    for declaration in hook_handlers({"hooks": {event_name: entries}}):
        raw = declaration.value
        _check_cursor_field_types(raw)
        if "timeout" in raw and "timeoutSec" in raw:
            raise HookContractError("Cursor handler must declare only one timeout")
        if (
            foreign
            and "/hooks/" in declaration.json_pointer.removeprefix("/hooks/")
            and "matcher" in raw
        ):
            raise HookContractError(
                "Claude handler-level matcher has no verified Cursor equivalent"
            )
    result: list[dict[str, Any]] = []
    for binding in _entries_to_ir(entries, event_name).bindings:
        if binding.metadata:
            raise HookContractError(
                f"unsupported Cursor matcher-group fields {sorted(binding.metadata)!r}"
            )
        matcher = _cursor_matcher(binding.matcher, event_name, foreign=foreign)
        for handler in binding.handlers:
            if handler.platform != "all":
                raise HookContractError(
                    "Cursor cannot preserve platform-specific command restrictions"
                )
            rendered = _handler_from_ir(handler, timeout_milliseconds=False)
            if matcher is not None:
                if "matcher" in rendered:
                    raise HookContractError("Cursor cannot combine nested and outer matchers")
                rendered["matcher"] = matcher
            if foreign and event_name in {"stop", "subagentStop"}:
                rendered.setdefault("loop_limit", None)
            _validate_cursor_handler(rendered, event_name)
            result.append(rendered)
    return result


def validate_cursor_config(document: object) -> None:
    """Reject a native file that Cursor would not load, without repairing user data."""
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
        raise HookContractError("Cursor config requires a hooks object")
    unknown_keys = document.keys() - CURSOR_CONFIG_TOP_LEVEL_KEYS
    if unknown_keys:
        raise HookContractError(
            f"Cursor config has unsupported top-level keys {sorted(unknown_keys)!r}"
        )
    if type(document.get("version")) is not int or document["version"] != 1:
        raise HookContractError("Cursor config requires version 1")
    for event, entries in document["hooks"].items():
        if event not in CURSOR_NATIVE_EVENTS or not isinstance(entries, list):
            raise HookContractError(f"unsupported Cursor event {event!r} or non-array entries")
        for entry in entries:
            if not isinstance(entry, dict):
                raise HookContractError("Cursor native handlers must be objects")
            _validate_cursor_handler(entry, event)


@dataclass(frozen=True)
class HookContentEntry:
    """Discovered native hook content, independent of ownership and safety."""

    pointer: str
    prompt: str | None = None
    error: str | None = None


def _inspect_handler(
    value: dict[str, Any],
    pointer: str,
    prompt_types: tuple[str, ...],
    command_keys: tuple[str, ...] = ("command",),
) -> HookContentEntry:
    """Classify one documented handler without inspecting command text."""
    kind = value.get("type", "command")
    if kind in prompt_types:
        prompt = value.get("prompt")
        if isinstance(prompt, str):
            return HookContentEntry(f"{pointer}/prompt", prompt=prompt)
        return HookContentEntry(pointer, error="prompt handler requires a string prompt")
    if kind == "command":
        commands = [value[key] for key in command_keys if key in value]
        if commands and all(isinstance(command, str) for command in commands):
            return HookContentEntry(pointer)
        return HookContentEntry(pointer, error="command handler requires string command fields")
    return HookContentEntry(pointer, error="unsupported native hook handler type")


def inspect_native_hooks(
    document: object,
    format_id: str,
    *,
    container: str = "hooks",
    prompt_types: tuple[str, ...] = (),
    named_containers: bool = False,
    shared: bool = False,
    nested_handlers: bool | None = None,
) -> tuple[HookContentEntry, ...]:
    """Read only documented native hook containers and prompt-bearing fields."""
    if not isinstance(document, dict):
        return (HookContentEntry("", error="hook document must be an object"),)
    if format_id == "kiro_hooks":
        if document.get("version") != "v1" or not isinstance(document.get("hooks"), list):
            return (HookContentEntry("", error="unsupported Kiro hook document"),)
        result = []
        for index, hook in enumerate(document["hooks"]):
            pointer = f"/hooks/{index}"
            if (
                not isinstance(hook, dict)
                or not isinstance(hook.get("trigger"), str)
                or not isinstance(hook.get("action"), dict)
            ):
                result.append(HookContentEntry(pointer, error="invalid Kiro hook action"))
            else:
                result.append(_inspect_handler(hook["action"], f"{pointer}/action", ("agent",)))
        return tuple(result)
    if format_id not in {
        "github_hooks",
        "claude_hooks",
        "cursor_hooks",
        "codex_hooks",
        "gemini_hooks",
        "antigravity_hooks",
        "windsurf_hooks",
    }:
        return (HookContentEntry("", error="unsupported native hook format"),)
    if format_id in {"github_hooks", "cursor_hooks"}:
        version = document.get("version", 1)
        if type(version) is not int or version != 1:
            return (HookContentEntry("", error="unsupported native hook version"),)
    if named_containers:
        containers = [
            (f"/{name.replace('~', '~0').replace('/', '~1')}", value)
            for name, value in document.items()
            if name != "version"
        ]
    elif container in document:
        containers = [(f"/{container}", document[container])]
    elif shared:
        return ()
    else:
        return (HookContentEntry("", error="missing native hook container"),)
    result: list[HookContentEntry] = []
    for prefix, events in containers:
        try:
            handlers = hook_handlers({"hooks": events})
        except HookContractError as exc:
            # Shape errors can include user-controlled event names. The diagnostic
            # identifies the container instead of echoing its contents.
            result.append(
                HookContentEntry(prefix, error=f"invalid hook container ({type(exc).__name__})")
            )
            continue
        for handler in handlers:
            nested = (
                handler.event in _ANTIGRAVITY_NESTED_EVENTS if named_containers else nested_handlers
            )
            is_child = "/hooks/" in handler.json_pointer.removeprefix("/hooks/")
            if not is_child and nested is True and not isinstance(handler.value.get("hooks"), list):
                result.append(
                    HookContentEntry(
                        prefix + handler.json_pointer.removeprefix("/hooks"),
                        error="native hook event requires nested handlers",
                    )
                )
                continue
            if not is_child and nested is False and "hooks" in handler.value:
                result.append(
                    HookContentEntry(
                        prefix + handler.json_pointer.removeprefix("/hooks"),
                        error="native hook event requires flat handlers",
                    )
                )
                continue
            if is_child and nested is False:
                continue
            if is_child and "hooks" in handler.value:
                result.append(
                    HookContentEntry(
                        prefix + handler.json_pointer.removeprefix("/hooks"),
                        error="unsupported additional hook nesting",
                    )
                )
                continue
            if isinstance(handler.value.get("hooks"), list):
                continue
            pointer = prefix + handler.json_pointer.removeprefix("/hooks")
            result.append(
                _inspect_handler(
                    dict(handler.value),
                    pointer,
                    prompt_types,
                    HOOK_COMMAND_KEYS if format_id == "github_hooks" else ("command",),
                )
            )
    return tuple(result)


def _handler_from_ir(handler: HookHandler, *, timeout_milliseconds: bool) -> dict[str, Any]:
    """Render a portable handler into one native command object."""
    result = _thaw(handler.metadata)
    if handler.command is not None:
        result["command"] = handler.command
    if handler.timeout_seconds is not None:
        result["timeout"] = (
            handler.timeout_seconds * 1000 if timeout_milliseconds else handler.timeout_seconds
        )
    if handler.provenance:
        result["_apm_source"] = handler.provenance
    return result


def _render_nested_document(
    document: HookDocument,
    *,
    timeout_milliseconds: bool,
    default_matcher: str | None = None,
) -> list:
    """Render neutral bindings into a matcher plus nested-handlers schema."""
    result: list = []
    for binding in document.bindings:
        if "raw_entry" in binding.metadata:
            result.append(_thaw(binding.metadata["raw_entry"]))
            continue
        outer = _thaw(binding.metadata)
        if binding.matcher is not None or default_matcher is not None:
            outer["matcher"] = binding.matcher or default_matcher
        outer["hooks"] = [
            _handler_from_ir(handler, timeout_milliseconds=timeout_milliseconds)
            for handler in binding.handlers
        ]
        provenance = binding.provenance or next(
            (handler.provenance for handler in binding.handlers if handler.provenance is not None),
            None,
        )
        if provenance:
            outer["_apm_source"] = provenance
            for handler in outer["hooks"]:
                handler.pop("_apm_source", None)
        result.append(outer)
    return result


def _copilot_keys_to_gemini(hook: dict) -> None:
    """Compatibility edge helper backed by the neutral handler model."""
    rendered = _handler_from_ir(
        _handler_to_ir(hook, None),
        timeout_milliseconds=True,
    )
    hook.clear()
    hook.update(rendered)


def _to_gemini_hook_entries(entries: list) -> list:
    """Render portable bindings in Gemini's nested millisecond schema."""
    return _render_nested_document(
        _entries_to_ir(entries),
        timeout_milliseconds=True,
    )


def _legacy_hook_entries(entries: list, target: str) -> list:
    """Render old native shapes for ownership matching, never for deployment."""
    if target == "claude":
        return _to_claude_hook_entries(entries, default_handler_type=False)
    # Codex installs before native nesting wrote the original flat entries.
    return entries if target == "codex" else []


def _with_claude_default_handler_type(entry: object) -> object:
    """Supply Claude's required ``type`` for an untyped flat command entry.

    Only flat entries that the neutral grammar reads as a command handler are
    defaulted; explicit types, nested groups, and non-command entries pass
    through unchanged.
    """
    if not isinstance(entry, dict) or "type" in entry or isinstance(entry.get("hooks"), list):
        return entry
    if not isinstance(_handler_to_ir(entry, None).command, str):
        return entry
    return {"type": "command", **entry}


def _to_claude_hook_entries(entries: list, *, default_handler_type: bool = True) -> list:
    """Render portable bindings in Claude's nested matcher schema.

    ``default_handler_type=False`` reproduces the output of installs before
    untyped flat command entries were typed, so owned entries they wrote can
    still be matched on reinstall.
    """
    if default_handler_type:
        entries = [_with_claude_default_handler_type(entry) for entry in entries]
    return _render_nested_document(
        _entries_to_ir(entries),
        timeout_milliseconds=False,
        default_matcher="*",
    )


def _to_codex_hook_entries(entries: list) -> list:
    """Render portable bindings in Codex's nested hook schema."""
    return _render_nested_document(
        _entries_to_ir(entries),
        timeout_milliseconds=False,
    )


def _to_antigravity_hook_entries(entries: list, event_name: str) -> list:
    """Render portable bindings in Antigravity's event-dependent schema."""
    document = _entries_to_ir(entries, event_name)
    if event_name in _ANTIGRAVITY_NESTED_EVENTS:
        return _render_nested_document(
            document,
            timeout_milliseconds=False,
            default_matcher="*",
        )

    flat: list[dict[str, Any]] = []
    for binding in document.bindings:
        if "raw_entry" in binding.metadata:
            flat.append(_thaw(binding.metadata["raw_entry"]))
            continue
        for handler in binding.handlers:
            rendered = _handler_from_ir(handler, timeout_milliseconds=False)
            if binding.provenance and "_apm_source" not in rendered:
                rendered["_apm_source"] = binding.provenance
            flat.append(rendered)
    return flat
