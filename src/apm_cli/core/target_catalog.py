"""Canonical capability metadata for accepted APM targets."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .deprecations import deprecation_message


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
    meta_target: bool = False
    accepts_agent_skills_packages: bool = False


_TARGET_COMMANDS = frozenset({"compile", "install", "update"})

# OpenAPM req-mf-005 / req-tg-004: the ``x-<vendor>-<name>`` target form.
_VENDOR_TARGET_RE = re.compile(r"^x-[a-z][a-z0-9-]*-[a-z][a-z0-9-]*$")


def is_vendor_target_id(token: object) -> bool:
    """True for a vendor-extension target identifier (``x-<vendor>-<name>``).

    Such an identifier parses; APM registers no vendor handler, so every
    consumer reports it as unsupported instead of ignoring it (#4251).
    """
    return isinstance(token, str) and _VENDOR_TARGET_RE.fullmatch(token) is not None


# Deprecated target aliases, mapped to the target they select. ``vscode``
# (#3866) selects ``copilot`` exactly, including its project MCP destination
# (workspace-root ``.mcp.json``); it has no destination of its own. Every
# parser canonicalises it to the replacement, so lockfiles, policy and
# detection only ever see ``copilot``. The consumer manifest and the CLI flags
# warn once per process (:func:`deprecated_target_alias_message`).
DEPRECATED_TARGET_ALIASES: Mapping[str, str] = MappingProxyType({"vscode": "copilot"})


def deprecated_target_alias_message(
    token: str, *, where: str | None = "apm.yml", key: str = ""
) -> str:
    """Return the once-per-process warning for a deprecated target alias.

    *where* names the file (omitted for CLI flags); *key* the field holding it.
    """
    location = f" in {key}" if key else ""
    message = deprecation_message(
        "target-alias-vscode",
        token=token,
        replacement=DEPRECATED_TARGET_ALIASES[token],
        location=location,
    )
    return f"{where}: {message}" if where else message


def canonical_target_token(token: str) -> str:
    """Return *token* with a deprecated alias replaced by the target it selects."""
    return DEPRECATED_TARGET_ALIASES.get(token, token)


def first_deprecated_target_alias(tokens: Iterable[object]) -> str | None:
    """Return the first deprecated target alias in *tokens* (case-insensitive)."""
    for token in tokens:
        name = str(token).strip().lower()
        if name in DEPRECATED_TARGET_ALIASES:
            return name
    return None


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
    runtimes: tuple[str, ...] = (),
    meta_target: bool = False,
    accepts_agent_skills_packages: bool = False,
) -> TargetCapability:
    """Create catalog data shared by all target-selecting commands."""
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
        commands=_TARGET_COMMANDS,
        meta_target=meta_target,
        accepts_agent_skills_packages=accepts_agent_skills_packages,
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
            "agent-skills",
            "Cross-client native .agents skills configuration",
            explicit_only=True,
            primitive_profile="agent-skills",
            meta_target=True,
        ),
        _capability(
            "antigravity",
            "Antigravity native .agents configuration",
            aliases=("agy",),
            explicit_only=True,
            primitive_profile="antigravity",
            compile_family="agents",
        ),
        _capability(
            "claude",
            "Claude Code native .claude configuration",
            in_all=True,
            primitive_profile="claude",
            compile_family="claude",
        ),
        _capability(
            "codex",
            "Codex native .codex and .agents configuration",
            in_all=True,
            primitive_profile="codex",
            compile_family="agents",
        ),
        _capability(
            "copilot",
            "GitHub Copilot native .github configuration",
            aliases=("agents",),
            in_all=True,
            primitive_profile="copilot",
            compile_family="vscode",
            runtimes=("copilot", "agents"),
        ),
        _capability(
            "copilot-app",
            "GitHub Copilot desktop app native workflow configuration",
            experimental_flag="copilot_app",
            primitive_profile="copilot-app",
        ),
        _capability(
            "copilot-cowork",
            "Microsoft 365 Copilot Cowork native skills configuration",
            experimental_flag="copilot_cowork",
            primitive_profile="copilot-cowork",
        ),
        _capability(
            "cursor",
            "Cursor native .cursor configuration",
            in_all=True,
            primitive_profile="cursor",
            compile_family="agents",
        ),
        _capability(
            "gemini",
            "Gemini CLI native .gemini configuration",
            in_all=True,
            primitive_profile="gemini",
            compile_family="gemini",
        ),
        _capability(
            "grok-bot",
            "Grok Bot native agent-data/workflows skills configuration",
            explicit_only=True,
            primitive_profile="grok-bot",
            accepts_agent_skills_packages=True,
        ),
        _capability(
            "grok-build",
            "xAI Grok Build native .grok configuration",
            in_all=True,
            primitive_profile="grok-build",
            compile_family="agents",
        ),
        _capability(
            "grok-cloud",
            "xAI Grok Cloud native .grok/skills configuration",
            explicit_only=True,
            experimental_flag="grok_cloud",
            primitive_profile="grok-cloud",
        ),
        _capability(
            "hermes",
            "Hermes native skills and MCP configuration",
            explicit_only=True,
            primitive_profile="hermes",
            compile_family="agents",
        ),
        _capability(
            "intellij",
            "IntelliJ MCP integration using the Copilot primitive profile",
            mcp_only=True,
            primitive_profile="copilot",
            compile_family="agents",
            runtimes=("intellij",),
        ),
        _capability(
            "kiro",
            "Kiro native .kiro configuration",
            in_all=True,
            primitive_profile="kiro",
            compile_family="agents",
        ),
        _capability(
            "openclaw",
            "OpenClaw native skills configuration",
            experimental_flag="openclaw",
            primitive_profile="openclaw",
        ),
        _capability(
            "opencode",
            "OpenCode native .opencode configuration",
            in_all=True,
            primitive_profile="opencode",
            compile_family="agents",
        ),
        _capability(
            "windsurf",
            "Windsurf native .windsurf and .agents configuration",
            in_all=True,
            primitive_profile="windsurf",
            compile_family="agents",
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
    replacement = DEPRECATED_TARGET_ALIASES.get(name_or_alias)
    if replacement is not None:
        return TARGET_CAPABILITIES[replacement]
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
    values.update(DEPRECATED_TARGET_ALIASES)
    values.add("all")
    return frozenset(values)


def manifest_target_names() -> frozenset[str]:
    """Return target identifiers accepted in ``apm.yml``."""
    names = {
        capability.name
        for capability in TARGET_CAPABILITIES.values()
        if capability.experimental_flag is None and not capability.mcp_only
    }
    names.update(DEPRECATED_TARGET_ALIASES)
    return frozenset(names)


def advertised_manifest_target_names() -> frozenset[str]:
    """Return the ``apm.yml`` target names to show users: accepted minus deprecated aliases.

    ``manifest_target_names`` still accepts a deprecated alias (``vscode``) so
    old manifests keep working with a warning; help text, errors and generated
    manifests must not advertise it.
    """
    return manifest_target_names() - frozenset(DEPRECATED_TARGET_ALIASES)


def normalize_target_name(name_or_alias: str) -> str:
    """Normalize a target spelling to its native capability name."""
    if name_or_alias == "all" or is_vendor_target_id(name_or_alias):
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
    deprecated = "".join(
        f" '{alias}' is a deprecated alias of '{target}'."
        for alias, target in sorted(DEPRECATED_TARGET_ALIASES.items())
    )
    return f"Values: {', '.join(sorted(accepted_target_values(command)))}.{deprecated}"


def target_all_exclusion_help() -> str:
    """Return the generated 'all' exclusion clause for command help strings.

    Produces a sorted list of names excluded from 'all': explicit-only
    non-experimental targets, 'experimental targets' as a category, and
    mcp-only targets.  Adding a new catalog entry automatically appears here.
    """
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
    """Return the values ``--target`` accepts on *command*, in error-display order.

    A command no capability names (``pack``, ``lock``, ``init``, ...) accepts
    the whole vocabulary through the shared parser, so its error lists that
    vocabulary rather than only ``all`` and the deprecated aliases (RA-039).
    """
    served = any(command in capability.commands for capability in TARGET_CAPABILITIES.values())
    return tuple(sorted(accepted_target_values(command if served else None)))


def target_value_label(value: str) -> str:
    """Return *value* as an error lists it: an alias names the target it selects.

    A deprecated alias is marked deprecated, so no listing presents it as a
    target in its own right (RA-037).
    """
    replacement = DEPRECATED_TARGET_ALIASES.get(value)
    if replacement is not None:
        return f"{value} (deprecated alias of {replacement})"
    for capability in TARGET_CAPABILITIES.values():
        if value in capability.aliases:
            return f"{value} (alias of {capability.name})"
    return value
