"""Projection API for canonical capabilities registered by harness adapters."""

from __future__ import annotations

from collections.abc import Mapping

from apm_cli.harnesses.registry import get_harness_registry
from apm_cli.harnesses.types import TargetCapability


class _CapabilityProjection(Mapping[str, TargetCapability]):
    def __getitem__(self, name: str) -> TargetCapability:
        return get_harness_registry().capabilities[name]

    def __iter__(self):
        return iter(get_harness_registry().capabilities)

    def __len__(self) -> int:
        return len(get_harness_registry().capabilities)


TARGET_CAPABILITIES: Mapping[str, TargetCapability] = _CapabilityProjection()


def get_target_capability(name_or_alias: str) -> TargetCapability:
    """Return the capability selected by a canonical name or alias."""
    capability = TARGET_CAPABILITIES.get(name_or_alias)
    if capability is not None:
        return capability
    for candidate in TARGET_CAPABILITIES.values():
        if name_or_alias in candidate.aliases:
            return candidate
    raise KeyError(name_or_alias)


def accepted_target_values(command: str | None = None) -> frozenset[str]:
    """Return every target spelling accepted by a command."""
    capabilities = (
        TARGET_CAPABILITIES.values()
        if command is None
        else (
            capability
            for capability in TARGET_CAPABILITIES.values()
            if command in capability.commands
        )
    )
    values = {
        value for capability in capabilities for value in (capability.name, *capability.aliases)
    }
    values.add("all")
    return frozenset(values)


def manifest_target_names() -> frozenset[str]:
    """Return target identifiers accepted in ``apm.yml``."""
    names = {
        capability.name
        for capability in TARGET_CAPABILITIES.values()
        if capability.experimental_flag is None and not capability.mcp_only
    }
    names.add("vscode")
    return frozenset(names)


def normalize_target_name(name_or_alias: str) -> str:
    """Normalize a target spelling to its native capability name."""
    if name_or_alias == "all":
        return name_or_alias
    return get_target_capability(name_or_alias).name


def expand_all(command: str) -> tuple[str, ...]:
    """Return the stable targets selected by ``all`` for a command."""
    expanded = []
    for capability in TARGET_CAPABILITIES.values():
        if not capability.in_all or command not in capability.commands:
            continue
        legacy_selector = (
            capability.compile_family
            if capability.compile_family in capability.aliases
            else capability.name
        )
        expanded.append(legacy_selector)
    return tuple(sorted(expanded))


def target_help_fragment(command: str) -> str:
    """Return the generated accepted-values fragment for command help."""
    return f"Values: {', '.join(sorted(accepted_target_values(command)))}."


def target_all_exclusion_help() -> str:
    """Return the generated 'all' exclusion clause for command help strings."""
    explicit_non_experimental = sorted(
        cap.name
        for cap in TARGET_CAPABILITIES.values()
        if cap.explicit_only and cap.experimental_flag is None
    )
    mcp_only_names = sorted(cap.name for cap in TARGET_CAPABILITIES.values() if cap.mcp_only)
    parts = [*explicit_non_experimental, "experimental targets", *mcp_only_names]
    if len(parts) == 1:
        return f"'all' excludes {parts[0]}"
    return f"'all' excludes {', '.join(parts[:-1])}, and {parts[-1]}"


def target_error_values(command: str) -> tuple[str, ...]:
    """Return accepted target values in deterministic error-display order."""
    return tuple(sorted(accepted_target_values(command)))
