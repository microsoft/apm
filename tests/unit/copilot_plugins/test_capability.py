"""Capability gate for native GitHub Copilot Agent Plugin registration.

Admission is a pure function of resolved target names: supported exactly
when the ``copilot`` target is among them. It never depends on whether a
Copilot binary exists on ``PATH`` or which version it reports.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.copilot_plugins.capability import (
    NativeRegistrationCapability,
    admits_native_plugin,
    current_native_registration,
    native_registration_scope,
    resolve_native_registration_capability,
    unsupported_target_reason,
)

pytestmark = pytest.mark.unit


def _targets(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in names]


def test_non_copilot_target_is_refused_with_a_precise_reason() -> None:
    """A non-Copilot target selection is refused, naming both sides."""
    capability = resolve_native_registration_capability(_targets("claude"))

    assert capability.supported is False
    assert "copilot" in (capability.reason or "")
    assert "claude" in (capability.reason or "")
    assert capability.target is None


def test_no_targets_is_refused_and_names_none_selected() -> None:
    """An empty/None target selection is refused, not admitted by default."""
    for targets in (None, [], _targets()):
        capability = resolve_native_registration_capability(targets)

        assert capability.supported is False
        assert "none" in (capability.reason or "")


def test_copilot_target_is_admitted_regardless_of_any_installed_binary() -> None:
    """Selecting the Copilot target alone admits native registration.

    There is no version or binary-presence gate: admission never depends on
    whether a Copilot binary exists on ``PATH`` or which version it reports.
    """
    capability = resolve_native_registration_capability(_targets("copilot"))

    assert capability.supported is True
    assert capability.target == "copilot"
    assert capability.reason is None


def test_copilot_target_among_others_is_still_admitted() -> None:
    """The Copilot target need not be the only selected target."""
    capability = resolve_native_registration_capability(_targets("copilot", "claude"))

    assert capability.supported is True
    assert capability.target == "copilot"


def test_unsupported_target_reason_lists_selected_targets_sorted() -> None:
    """The refusal reason is diagnosable: it names the selected targets."""
    reason = unsupported_target_reason(["claude", "vscode"])

    assert "claude, vscode" in reason
    assert "copilot" in reason
    assert "--target copilot" in reason


def test_require_raises_the_canonical_boundary_error_when_unsupported() -> None:
    """``require()`` raises the deployment boundary error, not a bespoke one."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError

    capability = resolve_native_registration_capability(_targets("claude"))

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        capability.require()


def test_require_is_a_no_op_when_supported() -> None:
    """``require()`` never raises for a supported capability."""
    capability = resolve_native_registration_capability(_targets("copilot"))

    capability.require()  # must not raise


def test_admission_requires_canonical_ir_even_when_the_target_qualifies() -> None:
    """A native package with no canonical IR is never admitted."""
    from apm_cli.models.validation import PackageType

    package_info = SimpleNamespace(
        package_type=PackageType.AGENT_PLUGIN,
        package=SimpleNamespace(agent_plugin=None),
    )

    with native_registration_scope(_targets("copilot")):
        assert admits_native_plugin(package_info) is False


def test_admits_native_plugin_is_fail_closed_with_no_published_capability() -> None:
    """Without an active capability scope, nothing is admitted."""
    from apm_cli.models.validation import PackageType

    package_info = SimpleNamespace(
        package_type=PackageType.AGENT_PLUGIN,
        package=SimpleNamespace(agent_plugin=object()),
    )

    assert current_native_registration() is None
    assert admits_native_plugin(package_info) is False


def test_scope_retires_the_published_capability() -> None:
    """The capability never leaks past the command that published it."""
    with native_registration_scope(_targets("copilot")) as cap:
        assert isinstance(cap, NativeRegistrationCapability)
        assert current_native_registration() is cap

    assert current_native_registration() is None
