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
