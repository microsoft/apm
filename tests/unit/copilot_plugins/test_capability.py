"""Capability gate for native GitHub Copilot Agent Plugin registration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.copilot_plugins.capability import (
    COPILOT_LIVE_PLUGIN_MIN_VERSION,
    VERSION_OVERRIDE_ENV,
    NativeRegistrationCapability,
    admits_native_plugin,
    is_qualified_client_version,
    native_registration_scope,
    normalize_client_version,
    probe_copilot_cli_version,
    resolve_native_registration_capability,
)

pytestmark = pytest.mark.unit


def _targets(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in names]


@pytest.mark.parametrize(
    ("version", "qualified"),
    [
        ("1.0.80", False),
        ("1.0.81-0", False),
        ("1.0.81-5", False),
        ("1.0.81-8", True),
        ("1.0.81-14", True),
        ("1.0.82", True),
        ("1.1.0", True),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_live_directory_floor_is_prerelease_aware(version: str, qualified: bool) -> None:
    """1.0.81-8 is the exact floor; earlier prereleases stay unqualified."""
    assert is_qualified_client_version(version) is qualified


def test_minimum_version_constant_matches_documented_floor() -> None:
    """The pinned floor is the version the live-directory feature landed in."""
    assert COPILOT_LIVE_PLUGIN_MIN_VERSION == "1.0.81-8"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.0.81-14", "1.0.81-14"),
        ("GitHub Copilot CLI 1.0.81-14 (darwin-arm64)", "1.0.81-14"),
        ("\n1.0.80\n", "1.0.80"),
        ("unknown", None),
        (None, None),
    ],
)
def test_version_output_is_normalized_to_a_semver_token(raw, expected) -> None:
    """Raw ``copilot --version`` output is reduced to its semver token."""
    assert normalize_client_version(raw) == expected


def test_non_copilot_target_is_refused_with_a_precise_reason() -> None:
    """A non-Copilot target never reaches the version probe."""
    probed: list[int] = []

    def _probe() -> str:
        probed.append(1)
        return "1.0.81-14"

    capability = resolve_native_registration_capability(_targets("claude"), probe=_probe)

    assert capability.supported is False
    assert "copilot" in (capability.reason or "")
    assert "claude" in (capability.reason or "")
    assert probed == []


def test_unqualified_client_names_the_required_floor_and_detected_version() -> None:
    """The refusal is diagnosable: it names both versions."""
    capability = resolve_native_registration_capability(_targets("copilot"), probe=lambda: "1.0.80")

    assert capability.supported is False
    assert COPILOT_LIVE_PLUGIN_MIN_VERSION in (capability.reason or "")
    assert "1.0.80" in (capability.reason or "")
    assert capability.detected_version == "1.0.80"


def test_missing_client_is_refused_rather_than_assumed_present() -> None:
    """No Copilot CLI on PATH is fail-closed, not fail-open."""
    capability = resolve_native_registration_capability(_targets("copilot"), probe=lambda: None)

    assert capability.supported is False
    assert "not detected" in (capability.reason or "")


def test_qualified_copilot_target_is_admitted() -> None:
    """A qualified client plus the Copilot target admits native registration."""
    capability = resolve_native_registration_capability(
        _targets("copilot", "claude"), probe=lambda: "1.0.81-14"
    )

    assert capability.supported is True
    assert capability.target == "copilot"
    assert capability.reason is None


def test_probe_reads_the_environment_override_without_touching_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented override short-circuits any subprocess probe."""
    calls: list[str] = []
    monkeypatch.setattr(
        "apm_cli.runtime.utils.find_runtime_binary",
        lambda name: calls.append(name) or "copilot",
    )
    monkeypatch.setenv(VERSION_OVERRIDE_ENV, "1.0.81-14")

    assert probe_copilot_cli_version() == "1.0.81-14"
    assert calls == []


def test_admission_requires_canonical_ir_even_when_the_client_qualifies() -> None:
    """A native package with no canonical IR is never admitted."""
    from apm_cli.models.validation import PackageType

    package_info = SimpleNamespace(
        package_type=PackageType.AGENT_PLUGIN,
        package=SimpleNamespace(agent_plugin=None),
    )

    with native_registration_scope(_targets("copilot"), probe=lambda: "1.0.81-14"):
        assert admits_native_plugin(package_info) is False


def test_scope_retires_the_published_capability() -> None:
    """The capability never leaks past the command that published it."""
    from apm_cli.copilot_plugins.capability import current_native_registration

    with native_registration_scope(_targets("copilot"), probe=lambda: "1.0.81-14") as cap:
        assert isinstance(cap, NativeRegistrationCapability)
        assert current_native_registration() is cap

    assert current_native_registration() is None
