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


def run(ctx: "InstallContext") -> None:
    """Execute the targets phase.

    On return ``ctx.targets`` and ``ctx.integrators`` are populated.
    """
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import (
        detect_target,
    )
    from apm_cli.integration import AgentIntegrator, PromptIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.cowork_paths import CoworkResolutionError
    from apm_cli.integration.targets import resolve_targets as _resolve_targets

    # Get config target from apm.yml if available
    config_target = ctx.apm_package.target

    # Resolve effective explicit target: CLI --target wins, then apm.yml
    _explicit = ctx.target_override or config_target or None

    # Determine active targets.  When --target or apm.yml target is set
    # the user's choice wins.  Otherwise auto-detect from existing dirs,
    # falling back to copilot when nothing is found.
    _is_user = ctx.scope is InstallScope.USER
    try:
        _targets = _resolve_targets(
            ctx.project_root,
            user_scope=_is_user,
            explicit_target=_explicit,
        )
    except CoworkResolutionError as exc:
        if ctx.logger:
            ctx.logger.error(str(exc), symbol="cross")
        raise SystemExit(1) from exc

    # ------------------------------------------------------------------
    # Fix 2: explicit --target cowork with flag OFF must error.
    # Fix 3: explicit --target cowork with flag ON but unresolvable
    #         OneDrive must error.
    # Only fire when the user explicitly asked for cowork. Auto-detect
    # silently omits cowork when unavailable.
    # ------------------------------------------------------------------
    _user_asked_cowork = False
    if _explicit:
        if isinstance(_explicit, list):
            _user_asked_cowork = "cowork" in _explicit
        else:
            _user_asked_cowork = _explicit == "cowork"

    if _user_asked_cowork:
        _cowork_resolved = any(t.name == "cowork" for t in _targets)
        if not _cowork_resolved:
            from apm_cli.core.experimental import is_enabled as _is_flag_on

            if not _is_flag_on("cowork"):
                # Flag is OFF — no-op with a targeted enable hint.
                if ctx.logger:
                    ctx.logger.progress(
                        "The 'cowork' target requires an experimental flag. "
                        "Run: apm experimental enable cowork",
                        symbol="info",
                    )
            else:
                # Fix 3: flag is ON but resolver returned None
                if ctx.logger:
                    ctx.logger.error(
                        "Cowork: no OneDrive path detected.\n"
                        "Set APM_COWORK_SKILLS_DIR or run: "
                        "apm config set cowork-skills-dir <path>",
                        symbol="cross",
                    )
                raise SystemExit(1)

    # ------------------------------------------------------------------
    # Amendment 5: project-scope gate for cowork target.
    # `--target cowork` without `--global` is an error -- cowork is
    # user-scope only.  Abort before any filesystem activity.
    # ------------------------------------------------------------------
    if not _is_user:
        _cowork_in_set = any(t.name == "cowork" for t in _targets)
        if _cowork_in_set:
            if ctx.logger:
                ctx.logger.error(
                    "The 'cowork' target requires --global (user scope). "
                    "Run: apm install --target cowork --global"
                )
            raise SystemExit(1)

    # Log target detection results
    if ctx.logger and _targets:
        _scope_label = "global" if _is_user else "project"
        _target_names = ", ".join(
            f"{t.name} (~/{t.root_dir}/)" if _is_user else t.name
            for t in _targets
        )
        ctx.logger.verbose_detail(
            f"Active {_scope_label} targets: {_target_names}"
        )
        if _is_user:
            from apm_cli.deps.lockfile import get_lockfile_path

            ctx.logger.verbose_detail(
                f"Lockfile: {get_lockfile_path(ctx.apm_dir)}"
            )

    for _t in _targets:
        # When the user passes --target (or apm.yml sets target=) we honour
        # the request even for targets that normally don't auto-create
        # their root dir (e.g. claude). Without this, `apm install --target
        # claude` would silently no-op when .claude/ doesn't exist (#763).
        if not _t.auto_create and not _explicit:
            continue
        # Dynamic-root targets (cowork): the integrator creates the
        # directory lazily via resolved_deploy_root.  Do not attempt to
        # create project_root / root_dir (the placeholder "cowork" dir).
        if _t.resolved_deploy_root is not None:
            continue
        _root = _t.root_dir
        _target_dir = ctx.project_root / _root
        if not _target_dir.exists():
            _target_dir.mkdir(parents=True, exist_ok=True)
            if ctx.logger:
                ctx.logger.verbose_detail(
                    f"Created {_root}/ ({_t.name} target)"
                )

    # Legacy detect_target call -- return values are not consumed by any
    # downstream code but the call is preserved for behaviour parity with
    # the pre-refactor mega-function.
    detect_target(
        project_root=ctx.project_root,
        explicit_target=_explicit,
        config_target=config_target,
    )

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
