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


def get_integrator_for_target(
    target,
    primitive: str,
    default_integrator: BaseIntegrator | None,
    *,
    default_class: type[BaseIntegrator],
    cache: dict[tuple[str, type], BaseIntegrator] | None = None,
) -> BaseIntegrator | None:
    """Resolve an adapter override, preserving the injected builtin instance."""
    from collections.abc import Mapping

    from apm_cli.harnesses.builtins import _LazyIntegrators
    from apm_cli.harnesses.registry import get_harness_registry

    adapter = get_harness_registry().adapters.get(target.name)
    overrides = getattr(adapter, "integrators", {}) if adapter is not None else {}
    if isinstance(overrides, _LazyIntegrators):
        return default_integrator
    if not isinstance(overrides, Mapping):
        raise TypeError(f"Harness {target.name!r} integrators must be a mapping")
    if primitive not in overrides:
        return default_integrator

    integrator_class = overrides[primitive]
    if integrator_class is default_class:
        return default_integrator
    if not isinstance(integrator_class, type) or not issubclass(integrator_class, default_class):
        expected = f"a subclass of {default_class.__module__}.{default_class.__qualname__}"
        raise TypeError(
            f"Harness {target.name!r} integrator override for {primitive!r} must be {expected}; "
            f"got {integrator_class!r}"
        )

    cache_key = (primitive, integrator_class)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    try:
        integrator = integrator_class()
    except Exception as exc:
        raise RuntimeError(
            f"Cannot construct harness {target.name!r} integrator override for "
            f"{primitive!r} ({integrator_class.__module__}.{integrator_class.__qualname__}); "
            "integrator classes must support no-argument construction"
        ) from exc
    if cache is not None:
        cache[cache_key] = integrator
    return integrator


def group_target_integrators(
    targets,
    primitive: str,
    default_integrator: BaseIntegrator | None,
    *,
    default_class: type[BaseIntegrator],
    cache: dict[tuple[str, type], BaseIntegrator] | None = None,
) -> list[tuple[BaseIntegrator, list]]:
    """Group targets by their selected integrator without duplicating instances."""
    if cache is None:
        cache = {}
    groups: list[tuple[BaseIntegrator, list]] = []
    group_indexes: dict[int, int] = {}
    for target in targets:
        integrator = get_integrator_for_target(
            target,
            primitive,
            default_integrator,
            default_class=default_class,
            cache=cache,
        )
        if integrator is None:
            continue
        index = group_indexes.get(id(integrator))
        if index is None:
            group_indexes[id(integrator)] = len(groups)
            groups.append((integrator, [target]))
        else:
            groups[index][1].append(target)
    return groups


# Lazily initialized on first access
def _build_dispatch() -> dict[str, PrimitiveDispatch]:
    """Build the dispatch table.

    Deferred import to avoid circular dependencies at module level.
    """
    from apm_cli.integration.agent_integrator import AgentIntegrator
    from apm_cli.integration.canvas_integrator import CanvasIntegrator
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
        "canvas": PrimitiveDispatch(
            CanvasIntegrator,
            "integrate_canvases_for_target",
            "sync_for_target",
            "canvases",
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
