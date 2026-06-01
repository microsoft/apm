"""Unit tests for the install-time audit precedence resolver.

Covers :mod:`apm_cli.core.install_audit`:

* ``resolve_install_audit_mode`` -- the master-switch / base / floor ladder.
* ``resolve_audit_override_from_cli`` -- ``--audit`` / ``--no-audit`` collapse.
* ``decide_for_install`` -- end-to-end wiring of flag + config + policy + CLI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.core.install_audit import (
    InstallAuditDecision,
    decide_for_install,
    resolve_audit_override_from_cli,
    resolve_install_audit_mode,
)


class TestResolveInstallAuditMode:
    """Precedence ladder: flag master switch > policy floor > CLI > config."""

    def test_flag_disabled_forces_off(self):
        # Every other source screams "block" but the master switch wins.
        mode, source = resolve_install_audit_mode(
            flag_enabled=False,
            cli_override="block",
            policy_mode="block",
            config_mode="block",
        )
        assert mode == "off"
        assert "flag" in source.lower()

    def test_default_is_off(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override=None, policy_mode=None, config_mode=None
        )
        assert mode == "off"
        assert source == "default"

    def test_config_warn_is_used(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override=None, policy_mode=None, config_mode="warn"
        )
        assert mode == "warn"
        assert "config" in source.lower()

    def test_config_off_falls_through_to_default(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override=None, policy_mode=None, config_mode="off"
        )
        assert mode == "off"
        assert source == "default"

    def test_cli_override_beats_config(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override="warn", policy_mode=None, config_mode="block"
        )
        assert mode == "warn"
        assert "cli" in source.lower()

    def test_policy_floor_raises_over_config(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override=None, policy_mode="block", config_mode="warn"
        )
        assert mode == "block"
        assert "policy" in source.lower()

    def test_policy_floor_cannot_be_relaxed_by_cli_off(self):
        # --no-audit resolves to cli_override="off"; org policy block must hold.
        mode, _ = resolve_install_audit_mode(
            flag_enabled=True, cli_override="off", policy_mode="block", config_mode=None
        )
        assert mode == "block"

    def test_cli_can_tighten_above_policy(self):
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override="block", policy_mode="warn", config_mode=None
        )
        assert mode == "block"
        assert "cli" in source.lower()

    def test_policy_equal_to_base_attributes_to_policy(self):
        # Floor at the same level as base still attributes to governance.
        mode, source = resolve_install_audit_mode(
            flag_enabled=True, cli_override="warn", policy_mode="warn", config_mode=None
        )
        assert mode == "warn"
        assert "policy" in source.lower()


class TestResolveAuditOverrideFromCli:
    def test_none_when_no_flags(self):
        assert resolve_audit_override_from_cli(no_audit=False, audit_mode=None) is None

    def test_no_audit_yields_off(self):
        assert resolve_audit_override_from_cli(no_audit=True, audit_mode=None) == "off"

    def test_audit_mode_lowercased(self):
        assert resolve_audit_override_from_cli(no_audit=False, audit_mode="BLOCK") == "block"

    def test_mutually_exclusive_raises(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            resolve_audit_override_from_cli(no_audit=True, audit_mode="warn")


def _ctx(*, audit_override=None, no_policy=False, policy=None):
    """Minimal duck-typed InstallContext stand-in for decide_for_install."""
    fetch = SimpleNamespace(policy=policy) if policy is not None else None
    return SimpleNamespace(
        audit_override=audit_override,
        no_policy=no_policy,
        policy_fetch=fetch,
    )


def _policy_with_audit(on_install, external=None):
    audit = SimpleNamespace(on_install=on_install, external=tuple(external or ()))
    security = SimpleNamespace(audit=audit)
    return SimpleNamespace(security=security)


class TestDecideForInstall:
    def test_flag_off_is_off(self, monkeypatch):
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: False)
        monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "block")
        decision = decide_for_install(_ctx(policy=_policy_with_audit("block")))
        assert isinstance(decision, InstallAuditDecision)
        assert decision.mode == "off"
        assert decision.external == ()

    def test_config_drives_mode_when_flag_on(self, monkeypatch):
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: True)
        monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "warn")
        decision = decide_for_install(_ctx())
        assert decision.mode == "warn"

    def test_policy_floor_and_external_attached(self, monkeypatch):
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: True)
        monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "off")
        decision = decide_for_install(
            _ctx(policy=_policy_with_audit("block", external=["skillspector"]))
        )
        assert decision.mode == "block"
        assert decision.external == ("skillspector",)

    def test_no_policy_skips_floor(self, monkeypatch):
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: True)
        monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "off")
        decision = decide_for_install(
            _ctx(no_policy=True, policy=_policy_with_audit("block", external=["skillspector"]))
        )
        assert decision.mode == "off"
        assert decision.external == ()

    def test_external_dropped_when_mode_off(self, monkeypatch):
        # Policy lists external scanners but the effective mode is off
        # (policy on_install off) -> no external scanners run.
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: True)
        monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "off")
        decision = decide_for_install(
            _ctx(policy=_policy_with_audit("off", external=["skillspector"]))
        )
        assert decision.mode == "off"
        assert decision.external == ()
