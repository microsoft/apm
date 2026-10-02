"""Native hook schema adapters around the vendor-neutral hook IR."""

from __future__ import annotations

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
            flat.append(binding.metadata["raw_entry"])
            continue
        for handler in binding.handlers:
            rendered = _handler_from_ir(handler, timeout_milliseconds=False)
            if binding.provenance and "_apm_source" not in rendered:
                rendered["_apm_source"] = binding.provenance
            flat.append(rendered)
    return flat
