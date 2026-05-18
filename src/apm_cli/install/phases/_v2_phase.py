"""v2 target-phase entry point (test-only thin wrapper)."""

from __future__ import annotations


def run_targets_phase(ctx) -> None:
    """v2 targets phase entry point using the new resolution algorithm (#1154).

    @internal: Test-only thin wrapper around ``resolve_targets()`` +
    deploy-dir materialization. Production install pipelines go through
    :func:`run` above, which composes legacy and v2 resolution in a single
    pass and emits the provenance line. Do not call this from production
    code paths -- it exists so unit tests can exercise the v2 mapping
    without the legacy ``run()`` setup overhead.

    Uses ``resolve_targets()`` from ``core.target_detection`` to determine
    effective targets, then materializes deploy directories and populates
    ``ctx.targets``.

    This is the three-guard collapse: every resolved target always materializes
    its deploy directory (auto_create=True unconditionally post-resolution).
    """
    from pathlib import Path

    from apm_cli.core.target_detection import resolve_targets
    from apm_cli.integration.targets import KNOWN_TARGETS

    project_root = Path(ctx.project_root)

    # Determine target override from ctx
    flag: str | list[str] | None = None
    if ctx.target_override:
        if isinstance(ctx.target_override, str):
            # Handle CSV form
            parts = [t.strip() for t in ctx.target_override.split(",") if t.strip()]
            flag = parts if len(parts) > 1 else parts[0] if parts else None
        else:
            flag = ctx.target_override

    # Get yaml_targets from apm_package
    yaml_targets: list[str] | None = None
    if ctx.apm_package and ctx.apm_package.target:
        raw = ctx.apm_package.target
        if isinstance(raw, str):
            yaml_targets = [t.strip() for t in raw.split(",") if t.strip()]
        elif isinstance(raw, list):
            yaml_targets = raw

    # Resolve targets
    resolved = resolve_targets(project_root, flag=flag, yaml_targets=yaml_targets)

    # Map resolved target names to TargetProfile objects and materialize dirs
    profiles: list = []
    for target_name in resolved.targets:
        profile = KNOWN_TARGETS.get(target_name)
        if profile is None:
            continue

        target_dir = project_root / profile.root_dir
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

        # NOTE: do NOT set resolved_deploy_root on static targets.
        # That field is reserved for dynamic-root targets (cowork) and is
        # treated as the final deploy destination by downstream integrators.
        # Static targets must follow the standard primitive-mapping path so
        # that ``deploy_root`` (e.g. .agents) and ``subdir`` (e.g. skills)
        # are honored.
        profiles.append(profile)

    ctx.targets = profiles
