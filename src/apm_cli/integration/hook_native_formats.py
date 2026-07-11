"""Native hook-entry format converters for non-Copilot harness targets.

Converts APM's canonical (GitHub Copilot flat) hook entries into the native
entry shapes required by Gemini CLI and Antigravity CLI. These transforms are
the single owner of target-specific hook-entry rewriting; ``hook_integrator``
composes them from ``_integrate_merged_hooks``.

Vendor-neutral by construction: each target's native schema is emitted
directly, with no intermediate translation layer. ``timeout`` unit handling
differs per target (Gemini emits milliseconds; Antigravity emits seconds).
"""


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
