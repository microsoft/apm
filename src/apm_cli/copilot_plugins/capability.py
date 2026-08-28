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
    copilot_targeted: bool = False

    def require(self) -> None:
        """Raise the canonical boundary error when registration is unavailable."""
        if self.supported:
            return
        from ..agent_plugins.errors import AgentPluginDeploymentBoundaryError

        raise AgentPluginDeploymentBoundaryError(self.reason or "")


class _LazyNativeRegistrationCapability(NativeRegistrationCapability):
    """Defer the client version probe until a capability field is first read.

    Resolving the capability can spawn ``copilot --version`` (hundreds of
    milliseconds). Publishing this lazy holder at the phase seam lets a command
    that never admits a native Agent Plugin -- the overwhelmingly common case --
    skip the probe entirely: :func:`admits_native_plugin` short-circuits on
    ``package_type`` before reading any field here, and the registration phase
    returns before consulting the capability when nothing is registrable. The
    first real read resolves once and memoizes, so repeated reads -- and the
    shared publish/resync reuse -- never re-probe.

    Subclasses :class:`NativeRegistrationCapability` so every ``isinstance`` and
    duck-typed reader keeps working unchanged.
    """

    __slots__ = ("_names", "_probe", "_resolved")

    def __init__(self, names: tuple[str, ...], probe: Callable[[], str | None] | None) -> None:
        object.__setattr__(self, "_names", names)
        object.__setattr__(self, "_probe", probe)
        object.__setattr__(self, "_resolved", None)
        # Cheap, probe-free: known from the selected target names alone.
        object.__setattr__(self, "copilot_targeted", COPILOT_TARGET_NAME in names)

    def _resolve(self) -> NativeRegistrationCapability:
        resolved = object.__getattribute__(self, "_resolved")
        if resolved is None:
            resolved = _resolve_capability_now(self._names, probe=self._probe)
            object.__setattr__(self, "_resolved", resolved)
        return resolved

    @property
    def supported(self) -> bool:  # type: ignore[override]
        return self._resolve().supported

    @property
    def reason(self) -> str | None:  # type: ignore[override]
        return self._resolve().reason

    @property
    def detected_version(self) -> str | None:  # type: ignore[override]
        return self._resolve().detected_version

    @property
    def target(self) -> str | None:  # type: ignore[override]
        return self._resolve().target


def minimum_client_version() -> SemVer:
    """Return the parsed minimum qualified Copilot CLI version."""
    parsed = parse_semver(COPILOT_LIVE_PLUGIN_MIN_VERSION)
    if parsed is None:  # pragma: no cover - constant is validated by tests
        raise ValueError(f"Invalid minimum version constant: {COPILOT_LIVE_PLUGIN_MIN_VERSION}")
    return parsed


def human_floor() -> str:
    """Return the human-facing ``major.minor.patch`` version floor.

    Derived from :data:`COPILOT_LIVE_PLUGIN_MIN_VERSION` so a future bump of the
    constant updates every user-facing "upgrade to X" sentence at once, instead
    of drifting against a hardcoded literal.
    """
    parsed = minimum_client_version()
    return f"{parsed.major}.{parsed.minor}.{parsed.patch}"


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
            env=_probe_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return normalize_client_version(completed.stdout or completed.stderr)


def _probe_environment() -> dict[str, str]:
    """Return a minimal env for the read-only version probe.

    The probe is a third-party binary invocation, so it must never inherit
    APM's credential surface (``GITHUB_APM_PAT`` / ``GITHUB_TOKEN`` /
    ``ADO_APM_PAT`` and any cloud tokens). Only the variables the binary needs
    to locate itself and resolve a home directory are forwarded.
    """
    allowed = ("PATH", "HOME", "SYSTEMROOT", "SystemRoot")
    env: dict[str, str] = {}
    for name in allowed:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


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


def undetected_client_reason() -> str:
    """Return the error text for a Copilot CLI that is absent from PATH.

    A missing binary is an installation problem, not a version-floor
    problem, so its recovery action names installation instead of an
    upgrade.
    """
    return (
        "GitHub Copilot CLI was not found on PATH, so APM cannot register "
        "Agent Plugins v1.0.0 packages natively. Install the GitHub Copilot "
        f"CLI ({human_floor()} or newer), then re-run 'apm install --target copilot'."
    )


def unqualified_client_reason(detected_version: str) -> str:
    """Return the precise error text for an unqualified Copilot CLI."""
    return (
        "Agent Plugins v1.0.0 packages need GitHub Copilot CLI "
        f">={COPILOT_LIVE_PLUGIN_MIN_VERSION}, which loads a directory "
        "marketplace live from its real directory; detected "
        f"{detected_version}. Older clients copy the plugin into private Copilot "
        "state, so APM refuses to install it there. Upgrade the GitHub "
        f"Copilot CLI to {human_floor()} or newer."
    )


def is_qualified_client_version(version: str | None) -> bool:
    """Return ``True`` when *version* reaches the live-directory floor."""
    parsed = parse_semver(version) if version else None
    if parsed is None:
        return False
    return parsed >= minimum_client_version()


def _resolve_capability_now(
    names: tuple[str, ...],
    *,
    probe: Callable[[], str | None] | None = None,
) -> NativeRegistrationCapability:
    """Resolve the capability eagerly, probing the client when copilot is targeted."""
    copilot_targeted = COPILOT_TARGET_NAME in names
    if not copilot_targeted:
        return NativeRegistrationCapability(
            supported=False,
            reason=unsupported_target_reason(names),
            copilot_targeted=False,
        )
    detected = (probe or probe_copilot_cli_version)()
    if detected is None:
        return NativeRegistrationCapability(
            supported=False,
            reason=undetected_client_reason(),
            copilot_targeted=True,
        )
    if not is_qualified_client_version(detected):
        return NativeRegistrationCapability(
            supported=False,
            reason=unqualified_client_reason(detected),
            detected_version=detected,
            copilot_targeted=True,
        )
    return NativeRegistrationCapability(
        supported=True,
        detected_version=detected,
        target=COPILOT_TARGET_NAME,
        copilot_targeted=True,
    )


def resolve_native_registration_capability(
    targets: Iterable[Any] | None,
    *,
    probe: Callable[[], str | None] | None = None,
) -> NativeRegistrationCapability:
    """Decide whether native Copilot plugin registration is available.

    Returns a lazy holder: the ``copilot --version`` probe fires on the first
    read of ``supported``/``reason``/``detected_version``/``target``, not here.
    Commands that never admit a native Agent Plugin therefore spawn no
    subprocess. The result is duck-type compatible with
    :class:`NativeRegistrationCapability`.
    """
    names = _target_names(targets)
    return _LazyNativeRegistrationCapability(names, probe)


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
