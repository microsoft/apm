"""Native hook schema adapters around the vendor-neutral hook IR."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from apm_cli.hook_contract import (
    HOOK_COMMAND_KEYS,
    HookContractError,
    HookDocument,
    HookHandler,
    _entries_to_ir,
    _handler_to_ir,
    hook_handlers,
)

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
        raise HookContractError(
            "Cursor cannot preserve this Claude matcher; regex, Glob and server-qualified "
            "MCP translations are not supported"
        )
    # Cursor combines file creation and editing under Write. Mapping either
    # Claude tool alone would broaden the original restriction.
    if set(names) & {"Edit", "Write"} and not {"Edit", "Write"} <= set(names):
        raise HookContractError("Cursor Write requires both Claude Edit and Write alternatives")
    return "|".join(dict.fromkeys(mapping[name] for name in names))


def _validate_cursor_handler(entry: dict[str, Any], event: str) -> None:
    """Validate documented native fields without dropping unsupported behavior."""
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
        raise HookContractError("unsupported Cursor handler fields; no fields were discarded")
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
    if "timeout" in entry and (
        type(entry["timeout"]) not in (int, float)
        or not math.isfinite(entry["timeout"])
        or entry["timeout"] <= 0
    ):
        raise HookContractError("Cursor timeout must be a finite positive number of seconds")
    if "failClosed" in entry and type(entry["failClosed"]) is not bool:
        raise HookContractError("Cursor failClosed must be a boolean")
    if "loop_limit" in entry:
        limit = entry["loop_limit"]
        if event not in {"stop", "subagentStop"} or (
            limit is not None and (type(limit) is not int or limit < 0)
        ):
            raise HookContractError(
                "Cursor loop_limit requires stop/subagentStop and integer or null"
            )
    if "matcher" in entry and not isinstance(entry["matcher"], str):
        raise HookContractError("Cursor matcher must be a string")


def _to_cursor_hook_entries(
    entries: list, event_name: str, *, foreign: bool = False
) -> list[dict[str, Any]]:
    """Render the neutral IR as strict native Cursor flat handlers.

    Claude import mappings are documented at
    https://cursor.com/docs/reference/third-party-hooks. The bounded adapter
    rejects mappings that lose restrictions instead of imitating lossy import.
    """
    if event_name not in CURSOR_NATIVE_EVENTS:
        raise HookContractError(f"unsupported Cursor event {event_name!r}")
    for declaration in hook_handlers({"hooks": {event_name: entries}}):
        raw = declaration.value
        if "matcher" in raw and not isinstance(raw["matcher"], str):
            raise HookContractError("Cursor matcher must be a string")
        if "timeout" in raw and "timeoutSec" in raw:
            raise HookContractError("Cursor handler must declare only one timeout")
        for key in ("timeout", "timeoutSec"):
            if key in raw and (
                type(raw[key]) not in (int, float) or not math.isfinite(raw[key]) or raw[key] <= 0
            ):
                raise HookContractError(
                    "Cursor timeout must be a finite positive number of seconds"
                )
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
            raise HookContractError("unsupported Cursor matcher-group fields")
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
    result = dict(handler.metadata)
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
            result.append(binding.metadata["raw_entry"])
            continue
        outer = dict(binding.metadata)
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


def _to_claude_hook_entries(entries: list) -> list:
    """Render portable bindings in Claude's nested matcher schema."""
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
            flat.append(binding.metadata["raw_entry"])
            continue
        for handler in binding.handlers:
            rendered = _handler_from_ir(handler, timeout_milliseconds=False)
            if binding.provenance and "_apm_source" not in rendered:
                rendered["_apm_source"] = binding.provenance
            flat.append(rendered)
    return flat
