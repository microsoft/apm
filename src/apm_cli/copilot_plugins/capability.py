"""Canonical owner of native Agent Plugin registration admission for Copilot.

One decision lives here: *may* APM hand a verified Agent Plugins 1.0 package to
GitHub Copilot as a live, natively loaded plugin instead of blocking it at the
deployment boundary?

The answer depends on exactly two facts:

1. the effective install targets include the ``copilot`` target;
2. the package carries canonical Agent Plugin IR (checked at the deployment
   boundary, not here).

Admission never depends on whether a Copilot binary exists on ``PATH``, nor on
which version it reports: APM materializes and projects any canonical Agent
Plugin whenever the ``copilot`` target is effectively selected and the
canonical IR / security / executable gates pass. Resolving this capability is
a pure function of the resolved target names -- it never shells out, reads an
environment override, or parses a version string. Every other call site asks
this module instead of re-deriving the answer.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .constants import COPILOT_TARGET_NAME

_ACTIVE: contextvars.ContextVar[NativeRegistrationCapability | None] = contextvars.ContextVar(
    "apm_copilot_native_registration", default=None
)


@dataclass(frozen=True, slots=True)
class NativeRegistrationCapability:
    """Whether native Copilot plugin registration is available for one command."""

    supported: bool
    reason: str | None = None
    target: str | None = None

    def require(self) -> None:
        """Raise the canonical boundary error when registration is unavailable."""
        if self.supported:
            return
        from ..agent_plugins.errors import AgentPluginDeploymentBoundaryError

        raise AgentPluginDeploymentBoundaryError(self.reason or "")


def _target_names(targets: Iterable[Any] | None) -> tuple[str, ...]:
    """Return canonical target names for a resolved target collection."""
    if not targets:
        return ()
    names: list[str] = []
    for target in targets:
        name = getattr(target, "name", target)
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def unsupported_target_reason(target_names: Iterable[str]) -> str:
    """Return the precise error text for a non-Copilot target selection."""
    selected = ", ".join(sorted(set(target_names))) or "none"
    return (
        "Agent Plugins v1.0.0 packages install natively only for the "
        f"'{COPILOT_TARGET_NAME}' target; selected target(s): {selected}. "
        "Re-run with --target copilot."
    )


def resolve_native_registration_capability(
    targets: Iterable[Any] | None,
) -> NativeRegistrationCapability:
    """Decide whether native Copilot plugin registration is available.

    Admission is a pure function of the resolved target names: supported
    exactly when the ``copilot`` target is among them. It never probes,
    shells out to, or otherwise depends on whether a Copilot binary exists on
    ``PATH`` or which version it reports -- resolving it spawns no subprocess
    and reads no environment override.
    """
    names = _target_names(targets)
    if COPILOT_TARGET_NAME not in names:
        return NativeRegistrationCapability(
            supported=False,
            reason=unsupported_target_reason(names),
        )
    return NativeRegistrationCapability(supported=True, target=COPILOT_TARGET_NAME)


def activate_native_registration(
    capability: NativeRegistrationCapability,
) -> contextvars.Token:
    """Publish the resolved capability for the duration of one command."""
    return _ACTIVE.set(capability)


@contextlib.contextmanager
def native_registration_scope(
    targets: Iterable[Any] | None,
) -> Iterator[NativeRegistrationCapability]:
    """Publish the capability for one command body, then retire it."""
    capability = resolve_native_registration_capability(targets)
    token = activate_native_registration(capability)
    try:
        yield capability
    finally:
        reset_native_registration(token)


def reset_native_registration(token: contextvars.Token) -> None:
    """Restore the capability that was active before :func:`activate`."""
    _ACTIVE.reset(token)


def current_native_registration() -> NativeRegistrationCapability | None:
    """Return the capability published for the running command, if any."""
    return _ACTIVE.get()


def admits_native_plugin(package_info: Any | None) -> bool:
    """Return ``True`` when *package_info* is an admitted native plugin.

    Admission requires canonical Agent Plugin IR AND an active, supported
    registration capability. Everything else stays fail-closed.
    """
    if package_info is None:
        return False
    from ..models.validation import PackageType

    if getattr(package_info, "package_type", None) is not PackageType.AGENT_PLUGIN:
        return False
    package = getattr(package_info, "package", None)
    if package is None or getattr(package, "agent_plugin", None) is None:
        return False
    capability = current_native_registration()
    return capability is not None and capability.supported
