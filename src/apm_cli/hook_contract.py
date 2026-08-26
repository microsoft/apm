"""Canonical vendor-neutral hook source grammar and executable intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

HOOK_COMMAND_KEYS: tuple[str, ...] = (
    "command",
    "bash",
    "powershell",
    "windows",
    "linux",
    "osx",
)


class HookContractError(ValueError):
    """Raised when a hook source document does not match the neutral grammar."""


def _freeze(value: Any) -> Any:
    """Recursively snapshot portable metadata."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class HookHandler:
    """One portable command handler."""

    command: str | None
    platform: str = "all"
    timeout_seconds: float | None = None
    provenance: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class HookBinding:
    """Handlers bound to one event and optional matcher."""

    event: str
    handlers: tuple[HookHandler, ...]
    matcher: str | None = None
    provenance: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class HookDocument:
    """Portable hook bindings translated only by native edge adapters."""

    bindings: tuple[HookBinding, ...]


@dataclass(frozen=True)
class HookCommandDeclaration:
    """One command field located in a validated hook source document."""

    event: str
    key: str
    command: str
    json_pointer: str


@dataclass(frozen=True)
class HookSourceDocument:
    """Validated hook source shape and its executable declarations."""

    commands: tuple[HookCommandDeclaration, ...]


def parse_hook_source(document: object) -> HookSourceDocument:
    """Validate wrapped or naked hook event maps and locate command fields."""
    return HookSourceDocument(commands=_walk_hook_commands(document, strict=True))


def walk_hook_commands(document: object) -> tuple[HookCommandDeclaration, ...]:
    """Locate valid commands while tolerating unrelated malformed legacy entries."""
    return _walk_hook_commands(document, strict=False)


def _walk_hook_commands(
    document: object,
    *,
    strict: bool,
) -> tuple[HookCommandDeclaration, ...]:
    """Walk one hook source through the canonical strict or tolerant grammar."""
    if not isinstance(document, dict):
        if strict:
            raise HookContractError("document must be a JSON object")
        return ()
    if "hooks" in document:
        hooks = document["hooks"]
        pointer_prefix = "/hooks"
    elif all(isinstance(value, list) for value in document.values()):
        hooks = document
        pointer_prefix = ""
    else:
        if strict:
            raise HookContractError("document must be a wrapped or naked hook event mapping")
        return ()
    if not isinstance(hooks, dict):
        if strict:
            raise HookContractError("hooks must be a JSON object")
        return ()

    commands: list[HookCommandDeclaration] = []
    for event, entries in hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            if strict:
                raise HookContractError("every hook event must have a string name and array value")
            continue
        escaped_event = _escape_json_pointer(event)
        event_pointer = f"{pointer_prefix}/{escaped_event}"
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                if strict:
                    raise HookContractError(f"hook event {event!r} entries must be objects")
                continue
            entry_pointer = f"{event_pointer}/{entry_index}"
            commands.extend(_command_declarations(event, entry, entry_pointer, strict=strict))
            nested = entry.get("hooks")
            if nested is None:
                continue
            if not isinstance(nested, list):
                if strict:
                    raise HookContractError(
                        f"hook event {event!r} nested handlers must be an array"
                    )
                continue
            for handler_index, handler in enumerate(nested):
                if not isinstance(handler, dict):
                    if strict:
                        raise HookContractError(
                            f"hook event {event!r} nested handlers must be objects"
                        )
                    continue
                commands.extend(
                    _command_declarations(
                        event,
                        handler,
                        f"{entry_pointer}/hooks/{handler_index}",
                        strict=strict,
                    )
                )
    return tuple(commands)


def _command_declarations(
    event: str,
    candidate: dict[str, Any],
    pointer: str,
    *,
    strict: bool,
) -> list[HookCommandDeclaration]:
    """Validate and return command fields from one hook handler object."""
    declarations: list[HookCommandDeclaration] = []
    for key in HOOK_COMMAND_KEYS:
        if key not in candidate:
            continue
        command = candidate[key]
        if not isinstance(command, str):
            if strict:
                raise HookContractError(f"hook event {event!r} {key} command must be a string")
            continue
        declarations.append(
            HookCommandDeclaration(
                event=event,
                key=key,
                command=command,
                json_pointer=f"{pointer}/{_escape_json_pointer(key)}",
            )
        )
    return declarations


def _handler_to_ir(raw: dict[str, Any], inherited_source: str | None) -> HookHandler:
    """Translate one accepted source handler into portable intent."""
    data = dict(raw)
    command = data.pop("command", None)
    platform = "all"
    if command is None:
        for key, candidate_platform in (
            ("bash", "posix"),
            ("powershell", "windows"),
            ("windows", "windows"),
        ):
            if key in data:
                command = data.pop(key)
                platform = candidate_platform
                break
    timeout_seconds = data.pop("timeoutSec", None)
    if timeout_seconds is None and "timeout" in data:
        timeout_seconds = data.pop("timeout")
    provenance = data.pop("_apm_source", None) or inherited_source
    return HookHandler(
        command=command,
        platform=platform,
        timeout_seconds=timeout_seconds,
        provenance=provenance,
        metadata=data,
    )


def _entries_to_ir(entries: list, event: str = "") -> HookDocument:
    """Translate accepted source entries into neutral bindings."""
    bindings: list[HookBinding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            bindings.append(
                HookBinding(
                    event=event,
                    handlers=(),
                    metadata={"raw_entry": entry},
                )
            )
            continue
        data = dict(entry)
        nested = data.pop("hooks", None)
        matcher = data.pop("matcher", None)
        provenance = data.pop("_apm_source", None)
        if isinstance(nested, list):
            handlers = tuple(
                _handler_to_ir(handler, provenance)
                for handler in nested
                if isinstance(handler, dict)
            )
            bindings.append(
                HookBinding(
                    event=event,
                    handlers=handlers,
                    matcher=matcher,
                    provenance=provenance,
                    metadata=data,
                )
            )
            continue
        bindings.append(
            HookBinding(
                event=event,
                handlers=(_handler_to_ir(data, provenance),),
                matcher=matcher,
                provenance=provenance,
            )
        )
    return HookDocument(bindings=tuple(bindings))


def _escape_json_pointer(value: str) -> str:
    """Escape one RFC 6901 JSON pointer segment."""
    return value.replace("~", "~0").replace("/", "~1")
