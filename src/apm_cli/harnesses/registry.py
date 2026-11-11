"""Deterministic registry for built-in and installed target harnesses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import PurePosixPath
from threading import RLock
from types import MappingProxyType

from apm_cli.harnesses.types import (
    HarnessAdapter,
    PrimitiveMapping,
    TargetCapability,
    TargetProfile,
)

_ENTRY_POINT_GROUP = "apm.harnesses"
_RESERVED_NAMES = frozenset({"all", "minimal"})
_TARGET_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")
_PRIMITIVE_METHODS = {
    "prompts": ("integrate_prompts_for_target", "sync_for_target"),
    "agents": ("integrate_agents_for_target", "sync_for_target"),
    "commands": ("integrate_commands_for_target", "sync_for_target"),
    "instructions": ("integrate_instructions_for_target", "sync_for_target"),
    "hooks": ("integrate_hooks_for_target", "sync_integration"),
    "canvas": ("integrate_canvases_for_target", "sync_for_target"),
    "skills": ("integrate_package_skill", "sync_integration"),
}


class HarnessAdapterError(RuntimeError):
    """A harness entry point or descriptor failed registry validation."""


@dataclass(frozen=True)
class HarnessRegistry:
    """Read-only index of all registered harness descriptors."""

    adapters: Mapping[str, HarnessAdapter]
    capabilities: Mapping[str, TargetCapability]
    profiles: Mapping[str, TargetProfile]
    mcp_clients: Mapping[str, object]
    external_names: frozenset[str]
    external_mcp_clients: frozenset[str]


def _entry_point_label(entry_point) -> str:
    distribution = (
        getattr(getattr(entry_point, "dist", None), "name", None) or "unknown distribution"
    )
    return f"{distribution}:{entry_point.name}"


def _iter_entry_points():
    discovered = entry_points()
    selected = (
        discovered.select(group=_ENTRY_POINT_GROUP)
        if hasattr(discovered, "select")
        else discovered.get(_ENTRY_POINT_GROUP, ())
    )
    return sorted(selected, key=lambda ep: (_entry_point_label(ep).casefold(), ep.value))


def _validate_signal(signal: object, adapter_name: str) -> tuple[str, str]:
    if not isinstance(signal, tuple) or len(signal) != 2:
        raise ValueError(f"Harness '{adapter_name}' signals must be (kind, relative_path) tuples")
    kind, value = signal
    if kind not in {"dir", "file"} or not isinstance(value, str) or not value:
        raise ValueError(f"Harness '{adapter_name}' has invalid signal {signal!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(
            f"Harness '{adapter_name}' signal path must be relative and contained: {value!r}"
        )
    return kind, value


def _validate_adapter(adapter: object, source: str, *, external: bool) -> HarnessAdapter:
    if not isinstance(adapter, HarnessAdapter):
        raise TypeError(
            f"{source} factory must return HarnessAdapter, got {type(adapter).__name__}"
        )
    capability = adapter.capability
    if not isinstance(capability, TargetCapability):
        raise TypeError(f"{source} capability must be TargetCapability")
    name = capability.name
    if not isinstance(name, str) or _TARGET_IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"{source} has invalid canonical name {name!r}")
    if name in _RESERVED_NAMES:
        raise ValueError(f"{source} has reserved canonical name {name!r}")
    if not isinstance(capability.aliases, tuple) or not all(
        isinstance(alias, str) and _TARGET_IDENTIFIER.fullmatch(alias) is not None
        for alias in capability.aliases
    ):
        raise ValueError(f"{source} aliases must be a tuple of non-empty strings")
    if len({name, *capability.aliases}) != len(capability.aliases) + 1:
        raise ValueError(f"{source} has duplicate or self-referential aliases")
    if not isinstance(capability.runtimes, tuple) or not all(
        isinstance(runtime, str) and runtime for runtime in capability.runtimes
    ):
        raise ValueError(f"{source} runtimes must be a tuple of non-empty strings")
    if len(set(capability.runtimes)) != len(capability.runtimes):
        raise ValueError(f"{source} has duplicate MCP runtimes")
    if (
        not isinstance(capability.description, str)
        or not capability.description
        or not isinstance(capability.commands, frozenset)
        or not capability.commands
        or not all(
            isinstance(command, str) and command in {"compile", "install", "update"}
            for command in capability.commands
        )
    ):
        raise ValueError(f"{source} capability must have a description and supported commands")
    if capability.mcp_only and capability.primitive_profile is None:
        raise ValueError(f"{source} MCP-only capability must declare primitive_profile")
    if capability.in_all and (capability.experimental_flag is not None or capability.explicit_only):
        raise ValueError(f"{source} cannot set in_all with experimental_flag or explicit_only")
    if capability.compile_family not in {None, "agents", "vscode", "claude", "gemini"}:
        raise ValueError(f"{source} has unknown compiler family {capability.compile_family!r}")
    if external and adapter.profile is None and not capability.mcp_only:
        raise ValueError(f"{source} requires a target profile")
    if external and capability.mcp_only and not adapter.mcp_clients:
        raise ValueError(f"{source} MCP-only target requires an MCP client")
    known_primitives = set(_PRIMITIVE_METHODS)
    if not isinstance(adapter.signals, tuple):
        raise TypeError(f"{source} signals must be a tuple")
    if adapter.profile is not None and not isinstance(adapter.profile, TargetProfile):
        raise TypeError(f"{source} profile must be TargetProfile or None")
    if adapter.profile is not None:
        if not isinstance(adapter.profile.primitives, Mapping):
            raise TypeError(f"{source} profile primitives must be a mapping")
        profile_primitives = set(adapter.profile.primitives)
        override_primitives = set(adapter.profile.user_primitive_overrides or {})
        unsupported_user_primitives = set(adapter.profile.unsupported_user_primitives)
        unknown = (
            profile_primitives | override_primitives | unsupported_user_primitives
        ) - known_primitives
        if unknown:
            raise ValueError(f"{source} profile uses unknown primitive keys {sorted(unknown)}")
        for primitive_mapping in (
            *adapter.profile.primitives.values(),
            *(adapter.profile.user_primitive_overrides or {}).values(),
        ):
            if not isinstance(primitive_mapping, PrimitiveMapping):
                raise TypeError(f"{source} profile primitive values must be PrimitiveMapping")
        if adapter.profile.capability != capability or adapter.profile.name != name:
            raise ValueError(f"{source} profile capability must be its own canonical capability")
        if capability.primitive_profile != name:
            raise ValueError(f"{source} profile requires primitive_profile to match its name")
    elif capability.primitive_profile == name and not capability.mcp_only:
        raise ValueError(f"{source} capability requires a matching target profile")
    for mapping_name, mapping in (
        ("mcp_clients", adapter.mcp_clients),
        ("integrators", adapter.integrators),
    ):
        if not isinstance(mapping, Mapping):
            raise TypeError(f"{source} {mapping_name} must be a mapping")
    if adapter.display_signal is not None and not isinstance(adapter.display_signal, str):
        raise TypeError(f"{source} display_signal must be a string or None")
    for runtime, constructor in adapter.mcp_clients.items():
        if not isinstance(runtime, str) or not runtime or not callable(constructor):
            raise ValueError(f"{source} has invalid MCP runtime constructor {runtime!r}")
    if external and not adapter.signals and (not capability.explicit_only or capability.in_all):
        raise ValueError(
            f"{source} without detection signals must be explicit-only and excluded from all"
        )
    if external:
        if set(capability.runtimes) != set(adapter.mcp_clients):
            raise ValueError(f"{source} capability runtimes must exactly match its MCP client keys")
        allowed_runtimes = {name, *capability.aliases}
        unknown_runtimes = set(adapter.mcp_clients) - allowed_runtimes
        if unknown_runtimes:
            raise ValueError(
                f"{source} MCP runtime keys must be the canonical name or declared aliases: "
                f"{sorted(unknown_runtimes)}"
            )
    if adapter.profile is None and adapter.integrators:
        raise ValueError(f"{source} cannot register integrators without a target profile")
    for primitive in adapter.integrators:
        if primitive not in known_primitives:
            raise ValueError(f"{source} uses unknown primitive key {primitive!r}")
        if adapter.profile is None or primitive not in adapter.profile.primitives:
            raise ValueError(f"{source} integrator override {primitive!r} is not in its profile")
    integrators = adapter.integrators.items() if external else ()
    for primitive, integrator in integrators:
        methods = _PRIMITIVE_METHODS.get(primitive)
        if methods is None:
            raise ValueError(f"{source} uses unknown primitive key {primitive!r}")
        if not isinstance(integrator, type):
            raise TypeError(f"{source} integrator for {primitive!r} must be a class")
        missing = [method for method in methods if not callable(getattr(integrator, method, None))]
        if missing:
            raise ValueError(f"{source} integrator for {primitive!r} lacks methods {missing}")
    signals = tuple(_validate_signal(signal, name) for signal in adapter.signals)
    if adapter.display_signal is not None:
        _validate_signal(("file", adapter.display_signal), name)
    if signals != adapter.signals:
        raise ValueError(f"{source} has invalid signals")
    return adapter


def _build_registry(*, include_plugins: bool = True) -> HarnessRegistry:
    from apm_cli.harnesses.builtins import get_builtin_adapters

    adapters: dict[str, HarnessAdapter] = {}
    capabilities: dict[str, TargetCapability] = {}
    profiles: dict[str, TargetProfile] = {}
    mcp_clients: dict[str, object] = {}
    value_owners: dict[str, str] = {}
    runtime_owners: dict[str, str] = {}
    external_names: set[str] = set()
    external_mcp_clients: set[str] = set()
    sources: dict[str, str] = {}

    builtin_adapters = get_builtin_adapters()
    discovered: list[tuple[object, str, bool]] = [
        (adapter, f"built-in target '{adapter.capability.name}'", False)
        for adapter in builtin_adapters
    ]
    if include_plugins:
        try:
            entry_points_found = _iter_entry_points()
        except Exception as exc:
            raise HarnessAdapterError(f"Failed discovering harness entry points: {exc}") from exc
        for entry_point in entry_points_found:
            label = _entry_point_label(entry_point)
            try:
                factory = entry_point.load()
                if not callable(factory):
                    raise TypeError("entry point is not callable")
                adapter = factory()
                discovered.append((adapter, label, True))
            except Exception as exc:
                raise HarnessAdapterError(
                    f"Failed loading harness entry point {label}: {exc}"
                ) from exc

    for unvalidated, source, external in discovered:
        try:
            adapter = _validate_adapter(unvalidated, source, external=external)
            capability = adapter.capability
            name = capability.name
            if name in adapters:
                raise ValueError(
                    f"duplicate canonical harness name {name!r} (already from {sources[name]})"
                )
            for value in (name, *capability.aliases):
                owner = value_owners.get(value)
                runtime_owner = runtime_owners.get(value)
                if owner is not None:
                    raise ValueError(f"target name/alias {value!r} collides with {owner}")
                if runtime_owner is not None:
                    raise ValueError(
                        f"target name/alias {value!r} collides with MCP runtime {runtime_owner}"
                    )
                if value in _RESERVED_NAMES:
                    raise ValueError(f"target name/alias {value!r} is reserved")
            for runtime in (*capability.runtimes, *adapter.mcp_clients):
                owner = runtime_owners.get(runtime)
                value_owner = value_owners.get(runtime)
                if owner is not None and owner != name:
                    raise ValueError(f"MCP runtime {runtime!r} collides with {owner}")
                if value_owner is not None and value_owner != name:
                    raise ValueError(f"MCP runtime {runtime!r} collides with {value_owner}")
                if runtime in _RESERVED_NAMES:
                    raise ValueError(f"MCP runtime {runtime!r} is reserved")
            adapters[name] = adapter
            capabilities[name] = capability
            sources[name] = source
            for value in (name, *capability.aliases):
                value_owners[value] = name
            if adapter.profile is not None:
                profiles[name] = adapter.profile
            for runtime in (*capability.runtimes, *adapter.mcp_clients):
                runtime_owners[runtime] = name
            for runtime, constructor in adapter.mcp_clients.items():
                existing = mcp_clients.get(runtime)
                if existing is not None and runtime in adapter.mcp_clients:
                    owner = runtime_owners.get(runtime)
                    if owner != name:
                        raise ValueError(f"duplicate MCP runtime constructor {runtime!r}")
                mcp_clients[runtime] = constructor
                if external:
                    external_mcp_clients.add(runtime)
            if external:
                external_names.add(name)
        except Exception as exc:
            raise HarnessAdapterError(f"Invalid harness {source}: {exc}") from exc

    for name, adapter in adapters.items():
        capability = adapter.capability
        if capability.mcp_only and capability.primitive_profile not in profiles:
            raise HarnessAdapterError(
                f"Invalid harness {sources[name]}: MCP-only target '{name}' references "
                f"unknown primitive profile {capability.primitive_profile!r}"
            )

    return HarnessRegistry(
        adapters=MappingProxyType(adapters),
        capabilities=MappingProxyType(capabilities),
        profiles=MappingProxyType(profiles),
        mcp_clients=MappingProxyType(mcp_clients),
        external_names=frozenset(external_names),
        external_mcp_clients=frozenset(external_mcp_clients),
    )


_REGISTRY: HarnessRegistry | None = None

_REGISTRY_ERROR: HarnessAdapterError | None = None
_REGISTRY_LOCK = RLock()


def get_harness_registry() -> HarnessRegistry:
    """Return the process-cached harness registry, loading extensions once."""
    global _REGISTRY, _REGISTRY_ERROR
    with _REGISTRY_LOCK:
        if _REGISTRY_ERROR is not None:
            raise _REGISTRY_ERROR
        if _REGISTRY is None:
            # Publish built-ins before loading third-party code. A provider may
            # import target-facing modules at module scope; those imports must
            # resolve against this bootstrap registry instead of rediscovering.
            _REGISTRY = _build_registry(include_plugins=False)
            try:
                _REGISTRY = _build_registry()
            except HarnessAdapterError as exc:
                _REGISTRY_ERROR = exc
                raise
            except Exception:
                _REGISTRY = None
                raise
        return _REGISTRY
