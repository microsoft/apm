"""Build security-checked source plans for hook survivor reintegration."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.exec_gate import check_executable_approval
from apm_cli.security.gate import BLOCK_POLICY
from apm_cli.utils.diagnostics import printable_ascii_text


def build_hook_reintegration_source_plan(
    dep_ref: Any,
    package_info: Any,
    targets: list[Any],
    allow_executables: dict[str, dict[str, bool]] | None,
) -> DeployableSourcePlan:
    """Return a scanned source plan before destructive hook reconciliation."""
    hooks_approved = check_executable_approval(
        dep_ref.get_identity(),
        package_info,
        allow_executables,
    )[0]
    source_plan = DeployableSourcePlan.create(
        package_info,
        targets,
        skill_subset=None,
        hooks_approved=hooks_approved,
        canvas_approved=False,
        skip_bin=True,
    )
    verdict = source_plan.scan_security(policy=BLOCK_POLICY)
    if verdict.should_block:
        raise ValueError(
            f"Refusing to rebuild hooks for {printable_ascii_text(dep_ref.get_identity())}: "
            "authorized source contains critical hidden characters"
        )
    return source_plan


def preflight_survivor_hooks(
    survivors: Iterable[tuple[Any, Any, list[Any]]],
    allow_executables: dict[str, dict[str, bool]] | None,
) -> None:
    """Refuse survivor hooks a rebuild would reject, before any destructive wipe.

    Uninstall clears APM-owned merged hook settings before it rebuilds
    survivors. A survivor that relied on retired filename routing, or that
    declares a Claude event without a native pair, would fail that rebuild
    after its working hooks were already gone (#3226). Each entry is
    ``(dep_ref, package_info, authorized_targets)``. Only the hook selection
    that ``DeployableSourcePlan`` would carry is checked; nothing is written,
    and the rebuild still builds and scans its full source plan.
    """
    from apm_cli.integration.hook_integrator import _HOOK_EVENT_MAP, HookIntegrator

    finder = HookIntegrator()
    for dep_ref, package_info, targets in survivors:
        hook_targets = [
            target.name for target in targets if "hooks" in getattr(target, "primitives", {})
        ]
        if not hook_targets:
            continue
        source_root = Path(package_info.install_path).resolve()
        hook_files = finder.find_hook_files(source_root)
        if not hook_files:
            continue
        if not check_executable_approval(dep_ref.get_identity(), package_info, allow_executables)[
            0
        ]:
            continue
        HookIntegrator.select_deployable_hook_sources(
            source_root, hook_targets, hook_files=hook_files
        ).require_deployable(package_info, _HOOK_EVENT_MAP)
