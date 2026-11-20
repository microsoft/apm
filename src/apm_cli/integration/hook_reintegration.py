"""Build security-checked source plans for hook survivor reintegration."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.exec_gate import check_executable_approval, hook_file_admission
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
    # Hooks only: this plan never deploys skills, so no skill selection applies.
    source_plan = DeployableSourcePlan.create(
        package_info,
        targets,
        skill_subset=None,
        hooks_approved=hooks_approved,
        hook_file_admitted=hook_file_admission(
            dep_ref.get_identity(), package_info, allow_executables
        ),
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
        if not check_executable_approval(dep_ref.get_identity(), package_info, allow_executables)[
            0
        ]:
            continue
        admitted = hook_file_admission(dep_ref.get_identity(), package_info, allow_executables)
        if admitted is not None:
            hook_files = [hook_file for hook_file in hook_files if admitted(hook_file)]
        if not hook_files:
            continue
        HookIntegrator.select_deployable_hook_sources(
            source_root, hook_targets, hook_files=hook_files
        ).require_deployable(package_info, _HOOK_EVENT_MAP)


def merged_hook_state(project_root: Path, targets: Iterable[Any]) -> dict[Path, bytes | None]:
    """Snapshot the merged hook configs and ownership sidecars a reconcile rewrites."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, native_hook_config

    state: dict[Path, bytes | None] = {}
    for target in targets:
        config = native_hook_config(target.name)
        if config is None:
            continue
        native = project_root / target.root_dir / config.config_filename
        for path in (native, native.parent / _APM_HOOKS_SIDECAR):
            state[path] = path.read_bytes() if path.is_file() else None
    return state
