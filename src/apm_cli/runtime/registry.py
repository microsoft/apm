"""Canonical runtime descriptors consumed by every runtime surface."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from .codex_runtime import CodexRuntime
from .copilot_runtime import CopilotRuntime
from .llm_runtime import LLMRuntime

if TYPE_CHECKING:
    from .base import RuntimeAdapter


@dataclass(frozen=True)
class RuntimeDescriptor:
    """Describe one runtime adapter, installer, and command capability."""

    name: str
    binary: str
    description: str
    preference: int
    setup_script: str
    adapter: type[RuntimeAdapter] | None = None
    npm_package: str | None = None
    script_builder: str | None = None
    content_argument: str = "positional"
    default_command: str | None = None


def _build_registry(
    descriptors: tuple[RuntimeDescriptor, ...],
) -> MappingProxyType[str, RuntimeDescriptor]:
    """Validate and freeze runtime descriptors."""
    registry: dict[str, RuntimeDescriptor] = {}
    preferences: set[int] = set()
    for descriptor in descriptors:
        if not descriptor.name or descriptor.name in registry:
            raise ValueError(f"Duplicate or empty runtime name: {descriptor.name!r}")
        if descriptor.preference in preferences:
            raise ValueError(f"Duplicate runtime preference: {descriptor.preference}")
        if descriptor.adapter is not None:
            adapter_name = descriptor.adapter.get_runtime_name()
            if adapter_name != descriptor.name:
                raise ValueError(
                    f"Runtime adapter {adapter_name!r} does not match descriptor "
                    f"{descriptor.name!r}"
                )
        registry[descriptor.name] = descriptor
        preferences.add(descriptor.preference)
    return MappingProxyType(registry)


RUNTIME_DESCRIPTORS = _build_registry(
    (
        RuntimeDescriptor(
            name="copilot",
            binary="copilot",
            description="GitHub Copilot CLI with native MCP integration",
            preference=10,
            setup_script="setup-copilot",
            adapter=CopilotRuntime,
            npm_package="@github/copilot",
            script_builder="_build_copilot_command",
            content_argument="prompt_flag",
            default_command=(
                "copilot --log-level all --log-dir copilot-logs --allow-all-tools -p {prompt_file}"
            ),
        ),
        RuntimeDescriptor(
            name="codex",
            binary="codex",
            description="OpenAI Codex CLI with GitHub Models support",
            preference=20,
            setup_script="setup-codex",
            adapter=CodexRuntime,
            script_builder="_build_codex_command",
            default_command=("codex -s workspace-write --skip-git-repo-check {prompt_file}"),
        ),
        RuntimeDescriptor(
            name="gemini",
            binary="gemini",
            description="Google Gemini CLI with MCP integration",
            preference=30,
            setup_script="setup-gemini",
            npm_package="@google/gemini-cli",
            script_builder="_build_gemini_command",
            content_argument="prompt_flag",
            default_command="gemini -p {prompt_file}",
        ),
        RuntimeDescriptor(
            name="llm",
            binary="llm",
            description="Simon Willison's LLM library with multiple providers",
            preference=40,
            setup_script="setup-llm",
            adapter=LLMRuntime,
            script_builder="_build_llm_command",
        ),
    )
)


def runtime_descriptors() -> tuple[RuntimeDescriptor, ...]:
    """Return descriptors in canonical preference order."""
    return tuple(sorted(RUNTIME_DESCRIPTORS.values(), key=lambda item: item.preference))


def runtime_names() -> tuple[str, ...]:
    """Return every managed runtime name in preference order."""
    return tuple(descriptor.name for descriptor in runtime_descriptors())


def adapter_descriptors() -> tuple[RuntimeDescriptor, ...]:
    """Return descriptors that expose a programmatic runtime adapter."""
    return tuple(descriptor for descriptor in runtime_descriptors() if descriptor.adapter)


def get_runtime_descriptor(name: str) -> RuntimeDescriptor:
    """Return one descriptor or raise a stable unknown-runtime error."""
    try:
        return RUNTIME_DESCRIPTORS[name]
    except KeyError:
        raise ValueError(f"Unknown runtime: {name}") from None
