"""Primitive dispatch registry.

Maps each APM primitive type to its integrator class and methods.
Both ``install.py`` and ``uninstall/engine.py`` import from this
module so the mapping is defined exactly once.

Skills are marked ``multi_target=True`` because ``SkillIntegrator``
receives all targets at once and routes internally.  All other
primitives are dispatched per-target in the outer loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Type  # noqa: F401, UP035

from apm_cli.integration.base_integrator import BaseIntegrator


@dataclass(frozen=True)
class PrimitiveDispatch:
    """How to integrate a single primitive type."""

    integrator_class: type[BaseIntegrator]
    """Integrator class to instantiate."""

    integrate_method: str
    """Method name for install (called per-target or with all targets)."""

    sync_method: str
    """Method name for uninstall removal."""

    counter_key: str
    """Key in the result counters dict (e.g., ``"agents"``)."""

    multi_target: bool = False
    """When True, the integrator receives all targets at once
    (used by SkillIntegrator)."""


def _build_dispatch() -> dict[str, PrimitiveDispatch]:
    """Build the dispatch table.

    Deferred import to avoid circular dependencies at module level.
    """
    from apm_cli.integration.agent_integrator import AgentIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.prompt_integrator import PromptIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator

    return {
        "prompts": PrimitiveDispatch(
            PromptIntegrator, "integrate_prompts_for_target", "sync_for_target", "prompts"
        ),
        "agents": PrimitiveDispatch(
            AgentIntegrator, "integrate_agents_for_target", "sync_for_target", "agents"
        ),
        "commands": PrimitiveDispatch(
            CommandIntegrator, "integrate_commands_for_target", "sync_for_target", "commands"
        ),
        "instructions": PrimitiveDispatch(
            InstructionIntegrator,
            "integrate_instructions_for_target",
            "sync_for_target",
            "instructions",
        ),
        "hooks": PrimitiveDispatch(
            HookIntegrator, "integrate_hooks_for_target", "sync_integration", "hooks"
        ),
        "skills": PrimitiveDispatch(
            SkillIntegrator,
            "integrate_package_skill",
            "sync_integration",
            "skills",
            multi_target=True,
        ),
    }


# Lazily initialized on first access
_DISPATCH: dict[str, PrimitiveDispatch] | None = None


def get_dispatch_table() -> dict[str, PrimitiveDispatch]:
    """Return the primitive dispatch table (lazily built)."""
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _build_dispatch()
    return _DISPATCH
