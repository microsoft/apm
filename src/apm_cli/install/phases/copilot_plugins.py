"""Native GitHub Copilot Agent Plugin capability phase (issue #2703).

``activate`` publishes the resolved native-registration capability so the
Agent Plugin deployment boundary can admit a verified plugin during
integration. Declaring admitted plugins to Copilot belongs to the shared
native-plugin phase (``phases/claude_plugins.py``, #3921).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from apm_cli.copilot_plugins.capability import (
    activate_native_registration,
    reset_native_registration,
    resolve_native_registration_capability,
)

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
    """Trace the native-registration verdict at verbose level.

    Admission is a pure function of the resolved target names -- it never
    probes a Copilot binary -- so the verdict is known immediately, with
    nothing deferred and no subprocess spawned.
    """
    logger = getattr(ctx, "logger", None)
    if logger is None:
        return
    if capability.supported:
        logger.verbose_detail("Copilot native registration: available (copilot targeted)")
        return
    logger.verbose_detail("Copilot native registration: unavailable (copilot target not selected)")


class ActivatePhase:
    """Adapter so the activate step gets ``_run_phase`` timing like its siblings.

    ``activate`` publishes the capability at the phase seam so it earns the
    same verbose ``Phase: ...`` timing line the registration ``run`` seam
    gets, even though resolving it is now a pure, immediate computation with
    no subprocess involved.
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
