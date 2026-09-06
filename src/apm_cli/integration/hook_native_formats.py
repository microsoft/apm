"""Native hook schema adapters around the vendor-neutral hook IR."""

from __future__ import annotations

from typing import Any

from apm_cli.hook_contract import (
    HookDocument,
    HookHandler,
    _entries_to_ir,
    _handler_to_ir,
)

_ANTIGRAVITY_NESTED_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})


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


# ---------------------------------------------------------------------------
# Inverse direction: native hook documents -> neutral bindings
# (used by ``apm init --discover --write``).
# ---------------------------------------------------------------------------

_GEMINI_TO_CANONICAL: dict[str, str] = {
    "BeforeTool": "PreToolUse",
    "AfterTool": "PostToolUse",
    "SessionEnd": "Stop",
}
_KIRO_TO_CANONICAL: dict[str, str] = {
    "PreTaskExec": "PreTaskExecution",
    "PostTaskExec": "PostTaskExecution",
}
_CURSOR_TO_CANONICAL: dict[str, str] = {
    "beforeSubmitPrompt": "UserPromptSubmit",
    "stop": "Stop",
    "sessionStart": "SessionStart",
}
_WINDSURF_TO_CANONICAL: dict[str, str] = {
    "pre_user_prompt": "UserPromptSubmit",
}


def _copilot_to_canonical() -> dict[str, str]:
    """camelCase Copilot event names -> Claude PascalCase canonical names."""
    from apm_cli.integration.hook_integrator import _HOOK_EVENT_MAP

    inverse: dict[str, str] = {}
    for source, target in _HOOK_EVENT_MAP["copilot"].items():
        if source[0].isupper():
            inverse.setdefault(target, source)
    return inverse


def _from_claude_hook_entries(entries: list, event: str) -> HookDocument:
    """Claude / Codex / Cursor / Windsurf entries are already the neutral shape."""
    return _entries_to_ir(entries, event)


def _from_gemini_hook_entries(entries: list, event: str) -> HookDocument:
    """Gemini stores millisecond timeouts and its own event vocabulary."""
    from dataclasses import replace

    document = _entries_to_ir(entries, _GEMINI_TO_CANONICAL.get(event, event))
    bindings = []
    for binding in document.bindings:
        handlers = tuple(
            replace(h, timeout_seconds=h.timeout_seconds / 1000)
            if isinstance(h.timeout_seconds, (int, float))
            else h
            for h in binding.handlers
        )
        bindings.append(replace(binding, handlers=handlers))
    return HookDocument(bindings=tuple(bindings))


def _from_antigravity_hook_entries(entries: list, event: str) -> HookDocument:
    """Antigravity nests Pre/PostToolUse and flattens every other event."""
    return _entries_to_ir(entries, event)


def _from_copilot_hook_file(payload: dict) -> dict[str, HookDocument]:
    """Copilot per-file hook document -> canonical event -> bindings."""
    inverse = _copilot_to_canonical()
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    result: dict[str, HookDocument] = {}
    if not isinstance(hooks, dict):
        return result
    for event, entries in hooks.items():
        if isinstance(entries, list):
            canonical = inverse.get(event, event)
            result[canonical] = _entries_to_ir(entries, canonical)
    return result


def _from_kiro_hook_docs(payload: dict) -> dict[str, HookDocument]:
    """Kiro v1 ``{"version": "v1", "hooks": [{trigger, matcher, action}]}`` -> bindings."""
    from apm_cli.hook_contract import HookBinding

    result: dict[str, list[HookBinding]] = {}
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if str(payload.get("version", "")).lower() != "v1" or not isinstance(hooks, list):
        return {}
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        trigger = str(hook.get("trigger", "")).strip()
        action = hook.get("action")
        if not trigger or not isinstance(action, dict):
            continue
        canonical = _KIRO_TO_CANONICAL.get(trigger, trigger)
        metadata: dict[str, Any] = {}
        if action.get("type") == "command" and action.get("command"):
            command = str(action["command"])
        else:
            command = None
            metadata = {"type": "prompt", "prompt": action.get("prompt", "")}
        handler = HookHandler(
            command=command,
            timeout_seconds=action.get("timeout"),
            metadata=metadata,
        )
        result.setdefault(canonical, []).append(
            HookBinding(event=canonical, handlers=(handler,), matcher=hook.get("matcher"))
        )
    return {event: HookDocument(bindings=tuple(b)) for event, b in result.items()}


def _from_cursor_hook_entries(entries: list, event: str) -> HookDocument:
    return _entries_to_ir(entries, _CURSOR_TO_CANONICAL.get(event, event))


def _from_windsurf_hook_entries(entries: list, event: str) -> HookDocument:
    return _entries_to_ir(entries, _WINDSURF_TO_CANONICAL.get(event, event))


def _document_to_entries(document: HookDocument) -> list:
    """Render neutral bindings as the wrapped Claude-shaped source grammar."""
    entries = _render_nested_document(document, timeout_milliseconds=False)
    for entry in entries:
        if isinstance(entry, dict):
            entry.pop("_apm_source", None)
            for handler in entry.get("hooks", []):
                if isinstance(handler, dict):
                    handler.pop("_apm_source", None)
    return entries
