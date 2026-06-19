"""Unit tests for the optional install-time content audit phase.

Covers :mod:`apm_cli.install.phases.audit`:

* ``off`` mode is a hard no-op.
* ``warn`` records diagnostics without halting.
* ``block`` raises ``PolicyViolationError`` on critical findings.
* ``--force`` downgrades a block to a warn.
* A policy-required external scanner that is unavailable fails closed
  (clear ``PolicyViolationError``) rather than silently skipping.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.core.install_audit import InstallAuditDecision
from apm_cli.install.errors import PolicyViolationError
from apm_cli.install.phases import audit as audit_phase
from apm_cli.security.content_scanner import ScanFinding
from apm_cli.security.external.base import ExternalScanError


class _Diag:
    def __init__(self):
        self.security_calls = []

    def security(self, message, package="", detail="", severity="warning"):
        self.security_calls.append(
            {"message": message, "package": package, "detail": detail, "severity": severity}
        )


class _Logger:
    def __init__(self):
        self.details = []

    def verbose_detail(self, msg):
        self.details.append(msg)


def _ctx(*, force=False):
    return SimpleNamespace(
        logger=_Logger(),
        project_root="/tmp/project",
        force=force,
        diagnostics=_Diag(),
    )


def _critical_finding():
    return ScanFinding(
        file="skill.md",
        line=1,
        column=1,
        char="\u202e",
        codepoint="U+202E",
        severity="critical",
        category="bidi-override",
        description="Right-to-left override (RLO)",
    )


def _patch_decision(monkeypatch, decision):
    monkeypatch.setattr("apm_cli.core.install_audit.decide_for_install", lambda ctx: decision)


def test_off_mode_is_noop(monkeypatch):
    _patch_decision(monkeypatch, InstallAuditDecision(mode="off", external=(), source="default"))
    called = {"scan": False}

    def _scan(*a, **k):
        called["scan"] = True
        return {}, 0

    monkeypatch.setattr("apm_cli.security.file_scanner.scan_lockfile_packages", _scan)
    ctx = _ctx()
    audit_phase.run(ctx)
    assert called["scan"] is False
    assert ctx.diagnostics.security_calls == []


def test_warn_records_diagnostics_no_halt(monkeypatch):
    _patch_decision(
        monkeypatch, InstallAuditDecision(mode="warn", external=(), source="apm config")
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages",
        lambda *a, **k: ({"skill.md": [_critical_finding()]}, 1),
    )
    ctx = _ctx()
    audit_phase.run(ctx)  # must not raise
    assert len(ctx.diagnostics.security_calls) == 1
    assert ctx.diagnostics.security_calls[0]["severity"] == "critical"


def test_block_raises_on_critical(monkeypatch):
    _patch_decision(
        monkeypatch, InstallAuditDecision(mode="block", external=(), source="apm-policy.yml")
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages",
        lambda *a, **k: ({"skill.md": [_critical_finding()]}, 1),
    )
    ctx = _ctx()
    with pytest.raises(PolicyViolationError, match="Install-time audit blocked"):
        audit_phase.run(ctx)


def test_force_downgrades_block_to_warn(monkeypatch):
    _patch_decision(
        monkeypatch, InstallAuditDecision(mode="block", external=(), source="apm-policy.yml")
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages",
        lambda *a, **k: ({"skill.md": [_critical_finding()]}, 1),
    )
    ctx = _ctx(force=True)
    audit_phase.run(ctx)  # must not raise
    assert len(ctx.diagnostics.security_calls) == 1
    assert "--force" in ctx.diagnostics.security_calls[0]["detail"]


def test_no_findings_is_quiet(monkeypatch):
    _patch_decision(
        monkeypatch, InstallAuditDecision(mode="warn", external=(), source="apm config")
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages", lambda *a, **k: ({}, 0)
    )
    ctx = _ctx()
    audit_phase.run(ctx)
    assert ctx.diagnostics.security_calls == []


def test_unavailable_external_scanner_fails_closed(monkeypatch):
    _patch_decision(
        monkeypatch,
        InstallAuditDecision(mode="block", external=("skillspector",), source="apm-policy.yml"),
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages", lambda *a, **k: ({}, 0)
    )

    def _raise(*a, **k):
        raise ExternalScanError("skillspector not found on PATH")

    monkeypatch.setattr("apm_cli.security.external.runner.run_external_scanners", _raise)
    ctx = _ctx()
    with pytest.raises(PolicyViolationError, match="required external scanner"):
        audit_phase.run(ctx)


def test_external_findings_merged_and_block(monkeypatch):
    _patch_decision(
        monkeypatch,
        InstallAuditDecision(mode="block", external=("skillspector",), source="apm-policy.yml"),
    )
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages", lambda *a, **k: ({}, 0)
    )
    monkeypatch.setattr(
        "apm_cli.security.external.runner.run_external_scanners",
        lambda *a, **k: {"skill.md": [_critical_finding()]},
    )
    ctx = _ctx()
    with pytest.raises(PolicyViolationError, match="Install-time audit blocked"):
        audit_phase.run(ctx)
