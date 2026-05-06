"""Target detection and integrator initialization phase.

Reads ``ctx.target_override``, ``ctx.apm_package``, ``ctx.scope``,
``ctx.project_root``; populates ``ctx.targets`` (list of
:class:`~apm_cli.integration.targets.TargetProfile`) and
``ctx.integrators`` (dict of per-primitive-type integrator instances).

This is the second phase of the install pipeline, running after resolve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _read_yaml_targets(ctx) -> list[str] | None:
    """Read targets/target from raw apm.yml using v2 parser.

    Returns a list of canonical target names, or None if neither key
    is present.  Raises ConflictingTargetsError if both keys appear.
    """
    if ctx.apm_package is None:
        return None
    apm_yml_path = getattr(ctx.apm_package, "package_path", None)
    if apm_yml_path is None:
        return None
    manifest = apm_yml_path / "apm.yml"
    if not manifest.exists():
        return None
    try:
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    from apm_cli.core.apm_yml import parse_targets_field

    result = parse_targets_field(data)
    return result if result else None


def run(ctx: InstallContext) -> None:
    """Execute the targets phase.

    On return ``ctx.targets`` and ``ctx.integrators`` are populated.
    """

    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import (
        detect_target,
        format_provenance,
    )
    from apm_cli.core.target_detection import (
        resolve_targets as _resolve_targets_v2,
    )
    from apm_cli.integration import AgentIntegrator, PromptIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.copilot_cowork_paths import CoworkResolutionError
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import (
        KNOWN_TARGETS,
    )
    from apm_cli.integration.targets import (
        resolve_targets as _resolve_targets_legacy,
    )

    # Get config target from apm.yml if available
    config_target = ctx.apm_package.target

    # Resolve effective explicit target: CLI --target wins, then apm.yml
    _explicit = ctx.target_override or config_target or None

    # ------------------------------------------------------------------
    # Deprecation warning for legacy '--target agents' alias (cli-review §1)
    # Driven by the raw-token flag set in parse_target_field() so that
    # multi-token inputs like "--target copilot,agents" still surface the
    # warning even after alias resolution collapses "agents" away.
    # ------------------------------------------------------------------
    from apm_cli.core.target_detection import agents_alias_was_detected

    if agents_alias_was_detected():
        if ctx.logger:
            ctx.logger.warning(
                "'--target agents' is deprecated -- it maps to 'copilot' (.github/), "
                "not '.agents/'. Use '--target copilot' or '--target agent-skills' "
                "(.agents/skills/). Removal in v1.0."
            )

    _is_user = ctx.scope is InstallScope.USER

    # Determine active targets using the legacy resolver first.
    # This preserves backward compatibility (cowork, user-scope, etc.)
    # while v2 adds provenance and stricter error checking.
    try:
        _targets = _resolve_targets_legacy(
            ctx.project_root,
            user_scope=_is_user,
            explicit_target=_explicit,
        )
    except CoworkResolutionError as exc:
        if ctx.logger:
            ctx.logger.error(str(exc), symbol="cross")
        raise SystemExit(1) from exc

    # ------------------------------------------------------------------
    # Fix 2: explicit --target copilot-cowork with flag OFF must error.
    # Fix 3: explicit --target copilot-cowork with flag ON but unresolvable
    #         OneDrive must error.
    # Only fire when the user explicitly asked for cowork. Auto-detect
    # silently omits cowork when unavailable.
    # ------------------------------------------------------------------
    _user_asked_cowork = False
    if _explicit:
        if isinstance(_explicit, list):
            _user_asked_cowork = "copilot-cowork" in _explicit
        else:
            _user_asked_cowork = _explicit == "copilot-cowork"

    if _user_asked_cowork:
        _cowork_resolved = any(t.name == "copilot-cowork" for t in _targets)
        if not _cowork_resolved:
            from apm_cli.core.experimental import is_enabled as _is_flag_on

            if not _is_flag_on("copilot_cowork"):
                if ctx.logger:
                    ctx.logger.progress(
                        "The 'copilot-cowork' target requires an experimental flag. "
                        "Run: apm experimental enable copilot-cowork",
                        symbol="info",
                    )
            else:
                import sys as _sys

                if _sys.platform.startswith("linux"):
                    _cowork_msg = (
                        "Cowork has no auto-detection on Linux.\n"
                        "Set APM_COPILOT_COWORK_SKILLS_DIR or run: "
                        "apm config set copilot-cowork-skills-dir <path>"
                    )
                else:
                    _cowork_msg = (
                        "Cowork: no OneDrive path detected.\n"
                        "Set APM_COPILOT_COWORK_SKILLS_DIR or run: "
                        "apm config set copilot-cowork-skills-dir <path>"
                    )
                if ctx.logger:
                    ctx.logger.error(_cowork_msg, symbol="cross")
                raise SystemExit(1)

    # ------------------------------------------------------------------
    # Amendment 5: project-scope gate for cowork target.
    # `--target copilot-cowork` without `--global` is an error -- cowork is
    # user-scope only.  Abort before any filesystem activity.
    # ------------------------------------------------------------------
    if not _is_user:
        _cowork_in_set = any(t.name == "copilot-cowork" for t in _targets)
        if _cowork_in_set:
            if ctx.logger:
                ctx.logger.error(
                    "The 'copilot-cowork' target requires --global (user scope). "
                    "Run: apm install --target copilot-cowork --global"
                )
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # v2 resolution (#1154): signal-based provenance and strict errors.
    # Runs AFTER the legacy resolver and cowork gates so existing
    # behavior is preserved.  The v2 resolver validates signals and
    # emits provenance; its target list REPLACES the legacy list for
    # project-scope installs (three-guard collapse).
    # ------------------------------------------------------------------
    if not _is_user:
        # Build flag from CLI --target override, filtering to canonical
        # targets only. Non-canonical targets (copilot-cowork) are handled
        # exclusively by the legacy resolver + gates above.
        from apm_cli.core.apm_yml import CANONICAL_TARGETS as _CANONICAL

        _v2_flag: str | list[str] | None = None
        if ctx.target_override:
            raw_override = ctx.target_override
            if isinstance(raw_override, str):
                parts = [t.strip() for t in raw_override.split(",") if t.strip()]
            else:
                parts = list(raw_override)
            # Keep only canonical targets for v2
            parts = [p for p in parts if p in _CANONICAL]
            if len(parts) == 1:
                _v2_flag = parts[0]
            elif len(parts) > 1:
                _v2_flag = parts
            # If no canonical targets remain, skip v2 entirely
            # (all targets were non-canonical like copilot-cowork)

        # Read targets from apm.yml (supports both target: and targets:)
        _v2_yaml: list[str] | None = None
        if _v2_flag is None and not ctx.target_override:
            import click as _click

            try:
                _v2_yaml = _read_yaml_targets(ctx)
            except _click.UsageError as exc:
                # ConflictingTargetsError (both target: and targets: in
                # apm.yml) is a user error -- surface with exit code 2.
                if ctx.logger:
                    ctx.logger.error(str(exc))
                raise SystemExit(2) from exc

        # Skip v2 entirely when all override targets were non-canonical
        # (e.g. copilot-cowork only).  Those are fully handled by the
        # legacy resolver + cowork gates.
        _skip_v2 = _v2_flag is None and _v2_yaml is None and ctx.target_override is not None

        if not _skip_v2:
            # Resolve: raises click.UsageError on no-harness, ambiguous,
            # unknown target, or schema conflict.  When the legacy resolver
            # already found targets and the v2 auto-detect disagrees (e.g.
            # because the legacy fallback-to-copilot is disabled in v2),
            # the v2 error takes precedence -- EXCEPT when the legacy
            # targets include non-canonical entries (e.g. copilot-cowork)
            # that v2 does not handle.
            import click as _click

            try:
                _resolved = _resolve_targets_v2(
                    ctx.project_root,
                    flag=_v2_flag,
                    yaml_targets=_v2_yaml,
                )
            except _click.UsageError as exc:
                # v2 target-resolution errors (NoHarnessError,
                # AmbiguousHarnessError, etc.) are intentionally
                # STRICTER than the legacy resolver.  They always
                # take precedence -- the whole point of the overhaul
                # is to stop silently falling back to copilot.
                #
                # The ONLY exception: if ALL legacy targets are
                # non-canonical (e.g. copilot-cowork) and v2 was
                # invoked without any canonical flag/yaml, the error
                # is a false positive because v2 does not handle
                # non-canonical targets.  That case is already
                # guarded by ``_skip_v2`` above, so it never reaches
                # this except block.
                if ctx.logger:
                    ctx.logger.error(str(exc))
                raise SystemExit(2) from exc

            # Emit provenance BEFORE any mutation. Route via _rich_info so
            # the line picks up consistent symbol + color treatment and so
            # automated tests can rely on the canonical "[i] Targets: ..."
            # rendering (convergence item 1).
            from apm_cli.utils.console import _rich_info

            _provenance_msg = format_provenance(_resolved)
            _rich_info(_provenance_msg, symbol="info")

            # Map resolved v2 target names to TargetProfile objects,
            # materializing deploy directories (three-guard collapse:
            # auto_create unconditionally post-resolution).
            _v2_targets = []
            for _tname in _resolved.targets:
                _profile = KNOWN_TARGETS.get(_tname)
                if _profile is None:
                    continue
                _target_dir = ctx.project_root / _profile.root_dir
                if not _target_dir.exists():
                    try:
                        _target_dir.mkdir(parents=True, exist_ok=True)
                    except PermissionError:
                        if ctx.logger:
                            ctx.logger.error(
                                f"Cannot create {_profile.root_dir}/ -- permission denied. "
                                f"Check directory permissions or use a different --target."
                            )
                        raise SystemExit(1) from None
                    if ctx.logger:
                        ctx.logger.verbose_detail(f"Created {_profile.root_dir}/ ({_tname} target)")
                # NOTE: do NOT set resolved_deploy_root on static targets.
                # That field is reserved for dynamic-root targets (cowork)
                # and is treated as the final deploy destination by
                # skill_integrator and base_integrator. Static targets must
                # follow the standard primitive-mapping path so that
                # ``deploy_root`` (e.g. .agents) and ``subdir`` (e.g. skills)
                # are honored.
                _v2_targets.append(_profile)

            # Replace legacy targets with v2 targets for project-scope.
            # Keep any legacy-only targets (e.g. copilot-cowork) that v2
            # doesn't handle.
            _v2_names = {t.name for t in _v2_targets}
            _legacy_only = [t for t in _targets if t.name not in _v2_names]
            _targets = _v2_targets + _legacy_only

    else:
        # User-scope: legacy target directory creation and logging.
        if ctx.logger:
            if _targets:
                _target_names = ", ".join(f"{t.name} (~/{t.root_dir}/)" for t in _targets)
                ctx.logger.verbose_detail(f"Active global targets: {_target_names}")
                from apm_cli.deps.lockfile import get_lockfile_path

                ctx.logger.verbose_detail(f"Lockfile: {get_lockfile_path(ctx.apm_dir)}")
            else:
                ctx.logger.warning(
                    "No global targets resolved -- nothing will be "
                    "deployed. Check 'target:' in apm.yml or use --target."
                )

        for _t in _targets:
            if not _t.auto_create and not _explicit:
                continue
            if _t.resolved_deploy_root is not None:
                continue
            _root = _t.root_dir
            _target_dir = ctx.project_root / _root
            if not _target_dir.exists():
                try:
                    _target_dir.mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    if ctx.logger:
                        _display_root = f"~/{_root}/"
                        ctx.logger.error(
                            f"Cannot create {_display_root} -- permission denied. "
                            f"Check directory permissions or use a different --target."
                        )
                    raise SystemExit(1) from None
                if ctx.logger:
                    ctx.logger.verbose_detail(f"Created {_root}/ ({_t.name} target)")

    # Legacy detect_target call -- return values are not consumed by any
    # downstream code but the call is preserved for behaviour parity with
    # the pre-refactor mega-function.
    detect_target(
        project_root=ctx.project_root,
        explicit_target=_explicit,
        config_target=config_target,
    )

    # ------------------------------------------------------------------
    # Legacy skill paths opt-out (convergence §3)
    # When --legacy-skill-paths is set (or APM_LEGACY_SKILL_PATHS env),
    # reset deploy_root on skills primitives so they fall back to the
    # per-client root_dir instead of the converged .agents/ directory.
    # ------------------------------------------------------------------
    if ctx.legacy_skill_paths:
        from apm_cli.integration.targets import apply_legacy_skill_paths

        _targets = apply_legacy_skill_paths(_targets)

    # ------------------------------------------------------------------
    # Initialize integrators
    # ------------------------------------------------------------------
    ctx.targets = _targets
    ctx.integrators = {
        "prompt": PromptIntegrator(),
        "agent": AgentIntegrator(),
        "skill": SkillIntegrator(),
        "command": CommandIntegrator(),
        "hook": HookIntegrator(),
        "instruction": InstructionIntegrator(),
    }


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
