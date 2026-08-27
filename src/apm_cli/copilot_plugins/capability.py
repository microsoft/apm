"""Canonical owner of native Agent Plugin registration admission for Copilot.

One decision lives here: *may* APM hand a verified Agent Plugins 1.0 package to
GitHub Copilot as a live, natively loaded plugin instead of blocking it at the
deployment boundary?

The answer depends on exactly three facts:

1. the effective install targets include the ``copilot`` target;
2. the Copilot CLI on PATH is at least
   :data:`~apm_cli.copilot_plugins.constants.COPILOT_LIVE_PLUGIN_MIN_VERSION`
   (the first release that loads directory-marketplace plugins live, without
   copying them into Copilot's private state);
3. the package carries canonical Agent Plugin IR.

Every other call site asks this module instead of re-deriving the answer.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from ..marketplace.semver import SemVer, parse_semver
from .constants import COPILOT_LIVE_PLUGIN_MIN_VERSION, COPILOT_TARGET_NAME

VERSION_OVERRIDE_ENV = "APM_COPILOT_CLI_VERSION"
"""Escape hatch that supplies the client version without probing the binary."""

_VERSION_TOKEN = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")

_ACTIVE: contextvars.ContextVar[NativeRegistrationCapability | None] = contextvars.ContextVar(
    "apm_copilot_native_registration", default=None
)


@dataclass(frozen=True, slots=True)
class NativeRegistrationCapability:
    """Whether native Copilot plugin registration is available for one command."""

    supported: bool
    reason: str | None = None
    detected_version: str | None = None
    target: str | None = None

    def require(self) -> None:
        """Raise the canonical boundary error when registration is unavailable."""
        if self.supported:
            return
        from ..agent_plugins.errors import AgentPluginDeploymentBoundaryError

        raise AgentPluginDeploymentBoundaryError(self.reason or "")


def minimum_client_version() -> SemVer:
    """Return the parsed minimum qualified Copilot CLI version."""
    parsed = parse_semver(COPILOT_LIVE_PLUGIN_MIN_VERSION)
    if parsed is None:  # pragma: no cover - constant is validated by tests
        raise ValueError(f"Invalid minimum version constant: {COPILOT_LIVE_PLUGIN_MIN_VERSION}")
    return parsed


def normalize_client_version(raw: str | None) -> str | None:
    """Extract the semver token from raw ``copilot --version`` output."""
    if not raw:
        return None
    match = _VERSION_TOKEN.search(raw)
    return match.group(0) if match else None


def probe_copilot_cli_version() -> str | None:
    """Return the installed Copilot CLI version, or ``None`` when unknown.

    The probe is a read-only capability question. APM never shells out to
    Copilot to perform an install, an update, or a plugin mutation.
    """
    override = os.environ.get(VERSION_OVERRIDE_ENV, "").strip()
    if override:
        return normalize_client_version(override)
    from ..runtime.utils import find_runtime_binary

    binary = find_runtime_binary(COPILOT_TARGET_NAME)
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return normalize_client_version(completed.stdout or completed.stderr)


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
    from ..agent_plugins.errors import AGENT_PLUGIN_RECOVERY

    selected = ", ".join(sorted(set(target_names))) or "none"
    return (
        "Agent Plugins v1.0.0 packages install natively only for the "
        f"'{COPILOT_TARGET_NAME}' target; selected target(s): {selected}. "
        "Re-run with --target copilot. " + AGENT_PLUGIN_RECOVERY
    )


def unqualified_client_reason(detected_version: str | None) -> str:
    """Return the precise error text for an unqualified Copilot CLI."""
    from ..agent_plugins.errors import AGENT_PLUGIN_RECOVERY

    detected = detected_version or "not detected"
    return (
        "Agent Plugins v1.0.0 packages need GitHub Copilot CLI "
        f">={COPILOT_LIVE_PLUGIN_MIN_VERSION}, which loads a directory "
        "marketplace live from its real directory; detected "
        f"{detected}. Older clients copy the plugin into private Copilot "
        "state, so APM refuses to install it there. Upgrade the Copilot CLI. "
        + AGENT_PLUGIN_RECOVERY
    )


def is_qualified_client_version(version: str | None) -> bool:
    """Return ``True`` when *version* reaches the live-directory floor."""
    parsed = parse_semver(version) if version else None
    if parsed is None:
        return False
    return parsed >= minimum_client_version()


def resolve_native_registration_capability(
    targets: Iterable[Any] | None,
    *,
    probe: Callable[[], str | None] | None = None,
) -> NativeRegistrationCapability:
    """Decide whether native Copilot plugin registration is available."""
    names = _target_names(targets)
    if COPILOT_TARGET_NAME not in names:
        return NativeRegistrationCapability(
            supported=False,
            reason=unsupported_target_reason(names),
        )
    detected = (probe or probe_copilot_cli_version)()
    if not is_qualified_client_version(detected):
        return NativeRegistrationCapability(
            supported=False,
            reason=unqualified_client_reason(detected),
            detected_version=detected,
        )
    return NativeRegistrationCapability(
        supported=True,
        detected_version=detected,
        target=COPILOT_TARGET_NAME,
    )


def activate_native_registration(
    capability: NativeRegistrationCapability,
) -> contextvars.Token:
    """Publish the resolved capability for the duration of one command."""
    return _ACTIVE.set(capability)


@contextlib.contextmanager
def native_registration_scope(
    targets: Iterable[Any] | None,
    *,
    probe: Callable[[], str | None] | None = None,
) -> Iterator[NativeRegistrationCapability]:
    """Publish the capability for one command body, then retire it."""
    capability = resolve_native_registration_capability(targets, probe=probe)
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
