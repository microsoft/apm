"""Legacy multi-target agent integration helper.

Extracted from :meth:`AgentIntegrator.integrate_package_agents` to keep
``agent_integrator.py`` under 500 lines.  Not part of the public API.

The function here reproduces the deprecated multi-target auto-copy behaviour
(copilot + claude + cursor simultaneously) that predates scope-aware
target-driven dispatch.  New code should call
:meth:`AgentIntegrator.integrate_agents_for_target` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath


@dataclass(frozen=True, slots=True)
class LegacyIntegrationOpts:
    """Optional arguments for legacy multi-target agent integration."""

    force: bool = False
    managed_files: set | None = None
    diagnostics: object | None = None


@dataclass(frozen=True, slots=True)
class _LegacyTargetSpec:
    """Deployment parameters for one legacy target."""

    key: str
    agents_dir: Path
    count_integrated: bool
    count_skipped: bool
    count_links: bool
    warning_label: str


@dataclass(frozen=True, slots=True)
class _LegacyIntegrationContext:
    """Shared context for one legacy source-file integration pass."""

    integrator: object
    package_name: str
    project_root: Path
    known_targets: object
    opts: LegacyIntegrationOpts


def _warn_rejected_target_path(diagnostics, package_name: str, label: str, exc: Exception) -> None:
    """Emit a path-rejection warning when diagnostics are available."""
    if diagnostics is None:
        return
    diagnostics.warn(
        message=f"Rejected {label} agent target path: {exc}",
        package=package_name,
    )


def _optional_agents_dir(project_root: Path, root_name: str) -> Path | None:
    """Return an optional legacy agents dir when its owning root exists."""
    root_dir = project_root / root_name
    if not root_dir.is_dir():
        return None
    agents_dir = root_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir


def _build_legacy_target_specs(project_root: Path, known_targets) -> list[_LegacyTargetSpec]:
    """Return the ordered legacy target deployment specs."""
    primary_agents_dir = project_root / ".github" / "agents"
    primary_agents_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        _LegacyTargetSpec(
            key="copilot",
            agents_dir=primary_agents_dir,
            count_integrated=True,
            count_skipped=True,
            count_links=True,
            warning_label="agent",
        )
    ]
    for key in ("claude", "cursor"):
        agents_dir = _optional_agents_dir(project_root, known_targets[key].root_dir)
        if agents_dir is None:
            continue
        specs.append(
            _LegacyTargetSpec(
                key=key,
                agents_dir=agents_dir,
                count_integrated=False,
                count_skipped=False,
                count_links=False,
                warning_label=f"{key} agent",
            )
        )
    return specs


def _integrate_source_for_target(
    ctx: _LegacyIntegrationContext,
    source_file: Path,
    spec: _LegacyTargetSpec,
) -> tuple[int, int, int, int, list[Path], bool]:
    """Integrate one source file for one legacy target."""
    target = ctx.known_targets[spec.key]
    target_filename = ctx.integrator.get_target_filename_for_target(
        source_file,
        ctx.package_name,
        target,
    )
    target_path = spec.agents_dir / target_filename
    try:
        ensure_path_within(target_path, spec.agents_dir)
    except PathTraversalError as exc:
        _warn_rejected_target_path(
            ctx.opts.diagnostics,
            ctx.package_name,
            spec.warning_label,
            exc,
        )
        skipped = 1 if spec.count_skipped else 0
        return 0, skipped, 0, 0, [], False

    rel_path = portable_relpath(target_path, ctx.project_root)
    if ctx.integrator.is_content_identical_to_source(target_path, source_file):
        return 0, 0, 1, 0, [target_path], True

    if ctx.integrator.check_collision(
        target_path,
        rel_path,
        ctx.opts.managed_files,
        ctx.opts.force,
        diagnostics=ctx.opts.diagnostics,
    ):
        skipped = 1 if spec.count_skipped else 0
        return 0, skipped, 0, 0, [], False

    links_resolved = ctx.integrator.copy_agent(source_file, target_path)
    integrated = 1 if spec.count_integrated else 0
    link_count = links_resolved if spec.count_links else 0
    return integrated, 0, 0, link_count, [target_path], True


def run_legacy_multi_target_integration(
    integrator,  # AgentIntegrator instance -- not typed to avoid circular import
    package_info,
    project_root: Path,
    opts: LegacyIntegrationOpts | None = None,
) -> IntegrationResult:
    """Execute the deprecated multi-target auto-copy logic."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    resolved_opts = opts or LegacyIntegrationOpts()
    package_name = package_info.package.name
    files_integrated = 0
    files_skipped = 0
    files_adopted = 0
    target_paths: list[Path] = []
    total_links_resolved = 0
    specs = _build_legacy_target_specs(project_root, KNOWN_TARGETS)

    ctx = _LegacyIntegrationContext(
        integrator=integrator,
        package_name=package_name,
        project_root=project_root,
        known_targets=KNOWN_TARGETS,
        opts=resolved_opts,
    )
    for source_file in integrator.find_agent_files(package_info.install_path):
        for spec in specs:
            integrated, skipped, adopted, links, paths, should_continue = (
                _integrate_source_for_target(
                    ctx,
                    source_file,
                    spec,
                )
            )
            files_integrated += integrated
            files_skipped += skipped
            files_adopted += adopted
            total_links_resolved += links
            target_paths.extend(paths)
            if spec.key == "copilot" and not should_continue:
                break

    return IntegrationResult(
        files_integrated=files_integrated,
        files_updated=0,
        files_skipped=files_skipped,
        target_paths=target_paths,
        links_resolved=total_links_resolved,
        files_adopted=files_adopted,
    )
