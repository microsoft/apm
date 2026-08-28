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
from apm_cli.copilot_plugins.constants import COPILOT_LIVE_PLUGIN_MIN_VERSION
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
    _log_capability_decision(ctx, capability)
    setattr(ctx, _REGISTRATION_ATTR, activate_native_registration(capability))


def _log_capability_decision(ctx: InstallContext, capability) -> None:
    """Trace the native-registration verdict at verbose level, probe-free.

    Reads only ``copilot_targeted`` (known from the target names, no
    subprocess). When copilot IS targeted the verdict depends on the client
    probe, which is deferred until an Agent Plugin is actually integrated -- so a
    zero-plugin install spawns nothing here. When copilot is NOT targeted the
    client was never probed, so the line must not claim it was "not detected".
    """
    logger = getattr(ctx, "logger", None)
    if logger is None:
        return
    if not getattr(capability, "copilot_targeted", False):
        logger.verbose_detail(
            "Copilot native registration: unavailable (copilot target not "
            f"selected, floor {COPILOT_LIVE_PLUGIN_MIN_VERSION})"
        )
        return
    logger.verbose_detail(
        "Copilot native registration: client probe deferred until an Agent "
        f"Plugin is integrated (floor {COPILOT_LIVE_PLUGIN_MIN_VERSION})"
    )


class ActivatePhase:
    """Adapter so the activate step gets ``_run_phase`` timing like its siblings.

    ``activate`` publishes the capability and can block on a ``copilot
    --version`` subprocess, so it earns the same verbose ``Phase: ...`` timing
    line the registration ``run`` seam gets.
    """

    @staticmethod
    def run(ctx: InstallContext) -> None:
        activate(ctx)


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
    """Return the resolved dependency set the registration is built from.

    One derivation feeds every lifecycle command: canonical locked state via
    :func:`candidates_from_lockfile` (which filters the synthesized ``.``
    self-entry and threads direct/target-subset), overlaid by the freshly
    resolved ``deps_to_install`` entries so an in-flight install sees the
    manifest's current target narrowing. Candidates whose executables the trust
    gate denied or left pending are dropped so a natively registered plugin can
    never smuggle an unapproved MCP server or bin past the gate.
    """
    from apm_cli.copilot_plugins.registrar import candidates_from_lockfile

    modules_dir = ctx.apm_modules_dir
    if modules_dir is None:
        return []
    modules_dir = Path(modules_dir)
    lockfile = getattr(ctx, "lockfile", None) or getattr(ctx, "existing_lockfile", None)
    # candidates_from_lockfile already drops locked entries whose executables
    # the trust gate denied or gated, so the shared gate lives in ONE place.
    candidates: dict[str, ResolvedPluginCandidate] = {
        candidate.dependency_key: candidate
        for candidate in (
            candidates_from_lockfile(lockfile, modules_dir) if lockfile is not None else []
        )
    }
    # The in-flight overlay carries exec status not yet persisted to the
    # lockfile, so gate it here against the freshly captured trust state.
    blocked = _exec_blocked_keys(ctx)
    for dependency in getattr(ctx, "deps_to_install", None) or []:
        try:
            key = dependency.get_unique_key()
            install_path = dependency.get_install_path(modules_dir)
        except (AttributeError, ValueError):
            continue
        if key in blocked:
            # A denied/gated in-flight package must not slip in via a stale
            # lockfile candidate either.
            candidates.pop(key, None)
            continue
        target_subset = getattr(dependency, "target_subset", None)
        candidates[key] = ResolvedPluginCandidate(
            dependency_key=key,
            install_path=Path(install_path),
            direct=getattr(dependency, "declaring_parent", None) is None,
            target_subset=tuple(target_subset) if target_subset else None,
            exec_status=(getattr(ctx, "package_exec_status", None) or {}).get(key),
        )
    return list(candidates.values())


def _exec_blocked_keys(ctx: InstallContext) -> set[str]:
    """Return dependency keys whose executables the trust gate did not clear."""
    from apm_cli.security.executables import TRUST_DENIED, TRUST_GATED

    statuses = getattr(ctx, "package_exec_status", None) or {}
    return {key for key, status in statuses.items() if status in (TRUST_DENIED, TRUST_GATED)}


def run(ctx: InstallContext) -> None:
    """Rebuild APM's Copilot plugin registration from resolved state."""
    modules_dir = ctx.apm_modules_dir
    if modules_dir is None:
        return
    capability = getattr(ctx, "copilot_registration", None)
    try:
        result = synchronize_copilot_plugins(
            project_root=ctx.project_root,
            modules_dir=Path(modules_dir),
            scope=ctx.scope,
            candidates=resolved_candidates(ctx),
            capability=capability,
            logger=ctx.logger,
            dry_run=bool(getattr(ctx, "dry_run", False)),
        )
    except CopilotSettingsCollisionError:
        # The collision message is surfaced verbatim by the typed passthrough
        # in the pipeline; re-raise without a dead DiagnosticCollector entry
        # (the raise propagates past the finalize phase that renders it).
        raise
    if result.skipped_reason and ctx.logger is not None:
        ctx.logger.verbose_detail(f"Copilot native registration skipped: {result.skipped_reason}")
