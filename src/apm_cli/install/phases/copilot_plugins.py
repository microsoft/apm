"""Native GitHub Copilot Agent Plugin registration phase (issue #2703).

Two seams live here:

``activate``
    Publishes the resolved native-registration capability so the Agent Plugin
    deployment boundary can admit a verified plugin during integration.

``run``
    Rebuilds the APM-owned directory marketplace and the two namespaced
    Copilot settings entries from canonical locked state, after the lockfile
    has been written.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.copilot_plugins.capability import (
    activate_native_registration,
    reset_native_registration,
    resolve_native_registration_capability,
)
from apm_cli.copilot_plugins.registrar import (
    ResolvedPluginCandidate,
    synchronize_copilot_plugins,
)
from apm_cli.copilot_plugins.settings import CopilotSettingsCollisionError

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

_REGISTRATION_ATTR = "_copilot_registration_token"


def activate(ctx: InstallContext) -> None:
    """Resolve and publish the native registration capability for this install."""
    capability = resolve_native_registration_capability(getattr(ctx, "targets", None))
    ctx.copilot_registration = capability
    setattr(ctx, _REGISTRATION_ATTR, activate_native_registration(capability))


def deactivate(ctx: InstallContext) -> None:
    """Retire the published capability at the end of the install command."""
    token = getattr(ctx, _REGISTRATION_ATTR, None)
    if token is None:
        return
    setattr(ctx, _REGISTRATION_ATTR, None)
    # A token created in another context is not ours to reset.
    with contextlib.suppress(ValueError):
        reset_native_registration(token)


def resolved_candidates(ctx: InstallContext) -> list[ResolvedPluginCandidate]:
    """Return the resolved dependency set the registration is built from."""
    modules_dir = ctx.apm_modules_dir
    if modules_dir is None:
        return []
    candidates: dict[str, ResolvedPluginCandidate] = {}
    for dependency in getattr(ctx, "deps_to_install", None) or []:
        try:
            key = dependency.get_unique_key()
            install_path = dependency.get_install_path(modules_dir)
        except (AttributeError, ValueError):
            continue
        candidates[key] = ResolvedPluginCandidate(
            dependency_key=key, install_path=Path(install_path)
        )
    lockfile = getattr(ctx, "lockfile", None) or getattr(ctx, "existing_lockfile", None)
    for key, locked in getattr(lockfile, "dependencies", {}).items():
        if key in candidates:
            continue
        install_path = _locked_install_path(locked, modules_dir)
        if install_path is not None:
            candidates[key] = ResolvedPluginCandidate(dependency_key=key, install_path=install_path)
    return list(candidates.values())


def _locked_install_path(locked: object, modules_dir: Path) -> Path | None:
    """Return the materialization path recorded for one locked dependency."""
    to_reference = getattr(locked, "to_dependency_ref", None)
    if to_reference is None:
        return None
    try:
        return Path(to_reference().get_install_path(modules_dir))
    except (ValueError, AttributeError):
        return None


def run(ctx: InstallContext) -> None:
    """Rebuild APM's Copilot plugin registration from resolved state."""
    modules_dir = ctx.apm_modules_dir
    if modules_dir is None:
        return
    capability = getattr(ctx, "copilot_registration", None)
    try:
        synchronize_copilot_plugins(
            project_root=ctx.project_root,
            modules_dir=Path(modules_dir),
            scope=ctx.scope,
            candidates=resolved_candidates(ctx),
            capability=capability,
            logger=ctx.logger,
            dry_run=bool(getattr(ctx, "dry_run", False)),
        )
    except CopilotSettingsCollisionError as exc:
        if ctx.diagnostics is not None:
            ctx.diagnostics.error(str(exc), package="copilot-plugins")
        raise
