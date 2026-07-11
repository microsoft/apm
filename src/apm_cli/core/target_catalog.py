"""Canonical capability metadata for accepted APM targets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class TargetCapability:
    """Command-facing capabilities of one native agent target."""

    name: str
    aliases: tuple[str, ...]
    description: str
    in_all: bool
    explicit_only: bool
    experimental_flag: str | None
    mcp_only: bool
    primitive_profile: str | None
    compile_family: str | None
    runtimes: tuple[str, ...]
    commands: frozenset[str]


_DEPLOY_COMMANDS = frozenset({"install", "update"})


def _capability(
    name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    in_all: bool = False,
    explicit_only: bool = False,
    experimental_flag: str | None = None,
    mcp_only: bool = False,
    primitive_profile: str | None = None,
    compile_family: str | None = None,
    compilable: bool = False,
    runtimes: tuple[str, ...] = (),
) -> TargetCapability:
    """Create catalog data shared by all target-selecting commands."""
    commands = _DEPLOY_COMMANDS | ({"compile"} if compilable else set())
    return TargetCapability(
        name=name,
        aliases=aliases,
        description=description,
        in_all=in_all,
        explicit_only=explicit_only,
        experimental_flag=experimental_flag,
        mcp_only=mcp_only,
        primitive_profile=primitive_profile,
        compile_family=compile_family,
        runtimes=runtimes,
        commands=frozenset(commands),
    )


def _build_target_catalog(
    capabilities: Iterable[TargetCapability],
) -> Mapping[str, TargetCapability]:
    """Validate and freeze target capability definitions."""
    catalog: dict[str, TargetCapability] = {}
    value_owners: dict[str, str] = {}
    runtime_owners: dict[str, str] = {}

    for capability in capabilities:
        if not capability.name:
            raise ValueError("Target capability name must not be empty")
        if not capability.description:
            raise ValueError(f"Target '{capability.name}' must have a description")
        if not capability.commands:
            raise ValueError(f"Target '{capability.name}' must declare at least one command")
        if capability.name in value_owners:
            raise ValueError(f"Duplicate target value '{capability.name}'")
        if capability.mcp_only and capability.primitive_profile is None:
            raise ValueError(f"MCP-only target '{capability.name}' must declare primitive_profile")
        if capability.in_all and (
            capability.experimental_flag is not None or capability.explicit_only
        ):
            raise ValueError(
                f"Target '{capability.name}' cannot set in_all with "
                "experimental_flag or explicit_only"
            )

        catalog[capability.name] = capability
        value_owners[capability.name] = capability.name
        for alias in capability.aliases:
            if alias in value_owners:
                raise ValueError(f"Duplicate target value '{alias}'")
            value_owners[alias] = capability.name
        for runtime in capability.runtimes:
            owner = runtime_owners.get(runtime)
            if owner is not None:
                raise ValueError(
                    f"Runtime '{runtime}' maps to multiple targets: "
                    f"'{owner}' and '{capability.name}'"
                )
            runtime_owners[runtime] = capability.name

    return MappingProxyType(catalog)


TARGET_CAPABILITIES: Mapping[str, TargetCapability] = _build_target_catalog(
    (
        _capability(
            "copilot",
            "GitHub Copilot native .github configuration",
            aliases=("vscode", "agents"),
            in_all=True,
            primitive_profile="copilot",
            compile_family="vscode",
            compilable=True,
            runtimes=("vscode", "agents"),
        ),
        _capability(
            "claude",
            "Claude Code native .claude configuration",
            in_all=True,
            primitive_profile="claude",
            compile_family="claude",
            compilable=True,
        ),
        _capability(
            "cursor",
            "Cursor native .cursor configuration",
            in_all=True,
            primitive_profile="cursor",
            compile_family="agents",
        ),
        _capability(
            "kiro",
            "Kiro native .kiro configuration",
            in_all=True,
            primitive_profile="kiro",
            compile_family="agents",
            compilable=True,
        ),
        _capability(
            "opencode",
            "OpenCode native .opencode configuration",
            in_all=True,
            primitive_profile="opencode",
            compile_family="agents",
            compilable=True,
        ),
        _capability(
            "gemini",
            "Gemini CLI native .gemini configuration",
            in_all=True,
            primitive_profile="gemini",
            compile_family="gemini",
            compilable=True,
        ),
        _capability(
            "antigravity",
            "Antigravity native .agents configuration",
            aliases=("agy",),
            explicit_only=True,
            primitive_profile="antigravity",
            compile_family="agents",
            compilable=True,
        ),
        _capability(
            "codex",
            "Codex native .codex and .agents configuration",
            in_all=True,
            primitive_profile="codex",
            compile_family="agents",
            compilable=True,
        ),
        _capability(
            "windsurf",
            "Windsurf native .windsurf and .agents configuration",
            in_all=True,
            primitive_profile="windsurf",
            compile_family="agents",
            compilable=True,
        ),
        _capability(
            "agent-skills",
            "Cross-client native .agents skills configuration",
            explicit_only=True,
            primitive_profile="agent-skills",
        ),
        _capability(
            "openclaw",
            "OpenClaw native skills configuration",
            experimental_flag="openclaw",
            primitive_profile="openclaw",
        ),
        _capability(
            "hermes",
            "Hermes native skills and MCP configuration",
            experimental_flag="hermes",
            primitive_profile="hermes",
            compile_family="agents",
        ),
        _capability(
            "copilot-cowork",
            "Microsoft 365 Copilot Cowork native skills configuration",
            experimental_flag="copilot_cowork",
            primitive_profile="copilot-cowork",
        ),
        _capability(
            "copilot-app",
            "GitHub Copilot desktop app native workflow configuration",
            experimental_flag="copilot_app",
            primitive_profile="copilot-app",
        ),
        _capability(
            "intellij",
            "IntelliJ MCP integration using the Copilot primitive profile",
            mcp_only=True,
            primitive_profile="copilot",
            runtimes=("intellij",),
        ),
    )
)


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


def target_error_values(command: str) -> tuple[str, ...]:
    """Return accepted target values in deterministic error-display order."""
    return tuple(sorted(accepted_target_values(command)))
