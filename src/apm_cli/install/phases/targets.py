"""Target detection and integrator initialization phase.

Reads ``ctx.target_override``, ``ctx.apm_package``, ``ctx.scope``,
``ctx.project_root``; populates ``ctx.targets`` (list of
:class:`~apm_cli.integration.targets.TargetProfile`) and
``ctx.integrators`` (dict of per-primitive-type integrator instances).

This is the second phase of the install pipeline, running after resolve.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.integration.targets import TargetProfile


def _package_field(apm_package: Any, name: str) -> Any:
    """Return a real APMPackage field without treating MagicMock attrs as set."""
    if apm_package is None:
        return None
    try:
        attrs = vars(apm_package)
    except TypeError:
        attrs = {}
    if name in attrs:
        return attrs[name]
    value = getattr(apm_package, name, None)
    if type(value).__module__ == "unittest.mock":
        return None
    return value


def _package_target_value(apm_package: Any) -> str | list[str] | None:
    """Read singular target or plural targets from the parsed package model."""
    from apm_cli.core.apm_yml import parse_targets_field

    target = _package_field(apm_package, "target")
    targets = _package_field(apm_package, "targets")
    if target is not None and targets is not None:
        parse_targets_field({"target": target, "targets": targets})
    if targets is not None:
        parsed = parse_targets_field({"targets": targets})
        return parsed if parsed else None
    return target


def _raise_target_usage_error(ctx: Any, exc: Exception) -> None:
    """Render target field user errors consistently before exiting."""
    if ctx.logger:
        ctx.logger.error(str(exc), symbol="")
    raise SystemExit(2) from exc


def _as_yaml_targets(value: str | list[str] | None) -> list[str] | None:
    """Normalize a package target value to the v2 yaml_targets shape."""
    if value is None:
        return None
    if isinstance(value, str):
        parts = [t.strip() for t in value.split(",") if t.strip()]
    else:
        parts = [str(t).strip() for t in value if str(t).strip()]
    return parts or None


def _normalize_runtime_target_aliases(tokens: Iterable[str]) -> list[str]:
    """Map runtime aliases to canonical target names in first-seen order."""
    from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

    normalized: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        raw = str(token).strip()
        if not raw:
            continue
        canonical = RUNTIME_TO_CANONICAL_TARGET.get(raw, raw)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def _read_yaml_targets(ctx) -> list[str] | None:
    """Read targets/target from raw apm.yml using v2 parser.

    Returns a list of canonical target names, or None if neither key
    is present.  Raises ConflictingTargetsError if both keys appear.
    """
    if ctx.apm_package is None:
        return None
    apm_yml_path = getattr(ctx.apm_package, "package_path", None)
    if apm_yml_path is None:
        return _as_yaml_targets(_package_target_value(ctx.apm_package))
    manifest = apm_yml_path / "apm.yml"
    if not manifest.exists():
        return _as_yaml_targets(_package_target_value(ctx.apm_package))
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


def _create_target_dirs(
    targets: Iterable[TargetProfile],
    project_root: Path,
    explicit: str | None,
    logger: Any = None,
) -> list[Path]:
    """Create root_dir for each target when auto_create=True or explicit is set.

    Targets that resolve to an external deploy root (``resolved_deploy_root``)
    are skipped: their directories live outside the project tree and are
    created by the integrator's deploy logic, not here.

    Returns the list of directories actually created.
    """
    created: list[Path] = []
    for _t in targets:
        if not _t.auto_create and not explicit:
            continue
        if _t.resolved_deploy_root is not None:
            continue
        _root = _t.root_dir
        _target_dir = project_root / _root
        if not _target_dir.exists():
            try:
                _target_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                if logger:
                    _display_root = f"~/{_root}/"
                    logger.error(
                        f"Cannot create {_display_root} -- permission denied. "
                        f"Check directory permissions or use a different --target."
                    )
                raise SystemExit(1) from None
            created.append(_target_dir)
            if logger:
                logger.verbose_detail(f"Created {_root}/ ({_t.name} target)")
    return created


def _check_openclaw_flag_gate(
    explicit: str | list[str] | None,
    targets: list,
    ctx: InstallContext,
) -> None:
    """Emit an enable-hint when the user asks for openclaw but the flag is OFF."""
    _check_experimental_target_hint(
        explicit, targets, ctx, target_name="openclaw", flag_name="openclaw"
    )


def _check_hermes_flag_gate(
    explicit: str | list[str] | None,
    targets: list,
    ctx: InstallContext,
) -> None:
    """Emit an enable-hint when the user asks for hermes but the flag is OFF."""
    _check_experimental_target_hint(
        explicit, targets, ctx, target_name="hermes", flag_name="hermes"
    )


def _check_experimental_target_hint(
    explicit: str | list[str] | None,
    targets: list,
    ctx: InstallContext,
    *,
    target_name: str,
    flag_name: str,
) -> None:
    """Emit an enable-hint when *target_name* is requested but its flag is OFF.

    Shared by the simple experimental targets whose only gate is the
    experimental flag (no extra environment requirement).
    """
    user_asked = False
    if explicit:
        if isinstance(explicit, list):
            user_asked = target_name in explicit
        else:
            user_asked = explicit == target_name
    if not user_asked:
        return

    resolved = any(t.name == target_name for t in targets)
    if resolved:
        return

    from apm_cli.core.experimental import is_enabled

    if not is_enabled(flag_name):
        if ctx.logger:
            ctx.logger.progress(
                f"The '{target_name}' target requires an experimental flag. "
                f"Run: apm experimental enable {flag_name}",
                symbol="info",
            )


def _gate_cowork_target(
    ctx: InstallContext,
    targets: list,
    explicit: str | list[str] | None,
    is_user: bool,
) -> None:
    """Apply cowork-target gating rules.

    Checks whether the user explicitly requested copilot-cowork and, if so,
    whether the experimental flag is enabled and the target resolved.
    Also enforces the project-scope gate (cowork requires ``--global``).
    May call ``raise SystemExit(1)`` when a gate condition is violated.
    """
    user_asked_cowork = False
    if explicit:
        if isinstance(explicit, list):
            user_asked_cowork = "copilot-cowork" in explicit
        else:
            user_asked_cowork = explicit == "copilot-cowork"

    if user_asked_cowork:
        _cowork_resolved = any(t.name == "copilot-cowork" for t in targets)
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

    # Amendment 5: project-scope gate for cowork target.
    if not is_user:
        _cowork_in_set = any(t.name == "copilot-cowork" for t in targets)
        if _cowork_in_set:
            if ctx.logger:
                ctx.logger.error(
                    "The 'copilot-cowork' target requires --global (user scope). "
                    "Run: apm install --target copilot-cowork --global"
                )
            raise SystemExit(1)


def _gate_copilot_app_target(
    ctx: InstallContext,
    targets: list,
    explicit: str | list[str] | None,
) -> None:
    """Apply copilot-app target gating rules.

    Checks whether the user explicitly requested copilot-app and, if so,
    whether the experimental flag is enabled and the app is installed.
    May call ``raise SystemExit(1)`` when a gate condition is violated.
    """
    user_asked_copilot_app = False
    if explicit:
        if isinstance(explicit, list):
            user_asked_copilot_app = "copilot-app" in explicit
        else:
            user_asked_copilot_app = explicit == "copilot-app"

    if not user_asked_copilot_app:
        return

    _copilot_app_resolved = any(t.name == "copilot-app" for t in targets)
    if _copilot_app_resolved:
        return

    from apm_cli.core.experimental import is_enabled as _is_flag_on

    if not _is_flag_on("copilot_app"):
        if ctx.logger:
            ctx.logger.progress(
                "The 'copilot-app' target requires an experimental flag. "
                "Run: apm experimental enable copilot-app",
                symbol="info",
            )
    else:
        _app_msg = (
            "GitHub Copilot desktop App not detected.\n"
            "Expected ~/.copilot/data.db but the file is missing.\n"
            "Install the app, or omit '--target copilot-app'."
        )
        if ctx.logger:
            ctx.logger.error(_app_msg, symbol="cross")
        raise SystemExit(1)


def _resolve_targets_by_scope(
    ctx: InstallContext,
    targets: list,
    explicit: str | list[str] | None,
    is_user: bool,
) -> list:
    """Resolve targets for either project scope (v2) or user scope (legacy).

    For project scope, applies the v2 resolution algorithm with signal-based
    provenance, replacing the legacy target list with the v2 list while
    preserving any non-canonical targets (e.g. copilot-cowork).

    For user scope, emits diagnostic logging and creates target directories
    via :func:`_create_target_dirs`.

    Returns the effective ``_targets`` list.
    """
    import click as _click

    from apm_cli.core.target_detection import format_provenance
    from apm_cli.core.target_detection import resolve_targets as _resolve_targets_v2

    if is_user:
        # User-scope: legacy target directory creation and logging.
        if ctx.logger:
            if targets:

                def _fmt_target(t: Any) -> str:
                    if t.resolved_deploy_root is not None:
                        return f"{t.name} ({t.resolved_deploy_root})"
                    return f"{t.name} (~/{t.root_dir}/)"

                _target_names = ", ".join(_fmt_target(t) for t in targets)
                ctx.logger.verbose_detail(f"Active global targets: {_target_names}")
                from apm_cli.deps.lockfile import get_lockfile_path

                ctx.logger.verbose_detail(f"Lockfile: {get_lockfile_path(ctx.apm_dir)}")
            else:
                ctx.logger.warning(
                    "No global targets resolved -- nothing will be "
                    "deployed. Check 'target:' in apm.yml or use --target."
                )
        _create_target_dirs(targets, ctx.project_root, explicit, ctx.logger)
        return targets

    # Project scope: v2 resolution.
    from apm_cli.core.apm_yml import CANONICAL_TARGETS as _CANONICAL
    from apm_cli.integration.targets import KNOWN_TARGETS

    _v2_flag: str | list[str] | None = None
    if ctx.target_override:
        raw_override = ctx.target_override
        if isinstance(raw_override, str):
            parts = [t.strip() for t in raw_override.split(",") if t.strip()]
        else:
            parts = list(raw_override)
        # Multi-token CLI parsing returns runtime aliases; convert them before filtering.
        parts = _normalize_runtime_target_aliases(parts)
        parts = [p for p in parts if p in _CANONICAL]
        if len(parts) == 1:
            _v2_flag = parts[0]
        elif len(parts) > 1:
            _v2_flag = parts

    _v2_yaml: list[str] | None = None
    if _v2_flag is None and not ctx.target_override:
        try:
            _v2_yaml = _read_yaml_targets(ctx)
        except _click.UsageError as exc:
            _raise_target_usage_error(ctx, exc)

    _skip_v2 = _v2_flag is None and _v2_yaml is None and ctx.target_override is not None
    if _skip_v2:
        return targets

    try:
        _resolved = _resolve_targets_v2(
            ctx.project_root,
            flag=_v2_flag,
            yaml_targets=_v2_yaml,
            flag_source=getattr(ctx, "target_override_source", None) or "--target flag",
        )
    except _click.UsageError as exc:
        if ctx.logger:
            ctx.logger.error(str(exc), symbol="")
        raise SystemExit(2) from exc

    from apm_cli.utils.console import _rich_info

    _provenance_msg = format_provenance(_resolved)
    _rich_info(_provenance_msg, symbol="info")

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
        _v2_targets.append(_profile)

    _v2_names = {t.name for t in _v2_targets}
    _legacy_only = [t for t in targets if t.name not in _v2_names and t.name not in _CANONICAL]
    return _v2_targets + _legacy_only


def run(ctx: InstallContext) -> None:
    """Execute the targets phase.

    On return ``ctx.targets`` and ``ctx.integrators`` are populated.
    """

    import click as _click

    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import (
        detect_target,
    )
    from apm_cli.integration import AgentIntegrator, PromptIntegrator
    from apm_cli.integration.canvas_integrator import CanvasIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.copilot_cowork_paths import CoworkResolutionError
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import (
        resolve_targets as _resolve_targets_legacy,
    )

    # Get config target from apm.yml if available.
    try:
        config_target = _package_target_value(ctx.apm_package)
    except _click.UsageError as exc:
        _raise_target_usage_error(ctx, exc)

    default_target = None
    if ctx.target_override is None and config_target is None:
        from apm_cli.config import get_install_target

        default_target = get_install_target()
        if default_target is not None:
            # Treat configured default target exactly like an explicit selector
            # for this invocation so downstream phases and policy checks see
            # the same effective value. Record the provenance separately so the
            # resolution output does not misattribute it to a CLI --target flag.
            ctx.target_override = default_target
            ctx.target_override_source = "apm config target"

    # Resolve effective explicit target: CLI --target wins, then apm.yml,
    # then user-scoped config default target.
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

    # Target gating: cowork, copilot-app, openclaw.
    _gate_cowork_target(ctx, _targets, _explicit, _is_user)
    _gate_copilot_app_target(ctx, _targets, _explicit)
    _check_openclaw_flag_gate(_explicit, _targets, ctx)
    _check_hermes_flag_gate(_explicit, _targets, ctx)

    # Resolve v2 targets for project scope, or set up user-scope dirs.
    _targets = _resolve_targets_by_scope(ctx, _targets, _explicit, _is_user)

    # Legacy detect_target call -- return values are not consumed by any
    # downstream code but the call is preserved for behaviour parity with
    # the pre-refactor mega-function.
    detect_target(
        project_root=ctx.project_root,
        explicit_target=_explicit if isinstance(_explicit, str) else None,
        config_target=config_target if isinstance(config_target, str) else None,
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
        "canvas": CanvasIntegrator(),
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

    import click as _click

    from apm_cli.core.target_detection import resolve_targets
    from apm_cli.integration.targets import KNOWN_TARGETS

    project_root = Path(ctx.project_root)

    # Determine target override from ctx
    flag: str | list[str] | None = None
    if ctx.target_override:
        if isinstance(ctx.target_override, str):
            # Handle CSV form
            parts = [t.strip() for t in ctx.target_override.split(",") if t.strip()]
        else:
            parts = list(ctx.target_override)
        parts = _normalize_runtime_target_aliases(parts)
        flag = parts if len(parts) > 1 else parts[0] if parts else None

    # Get yaml_targets from apm_package.
    try:
        yaml_targets = _as_yaml_targets(_package_target_value(ctx.apm_package))
    except _click.UsageError as exc:
        _raise_target_usage_error(ctx, exc)

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
