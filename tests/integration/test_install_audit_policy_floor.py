"""Integration tests for install-time audit policy floor enforcement (#1642).

Exercises the path from a real ``ApmPolicy`` (with ``SecurityPolicy`` /
``AuditPolicy``) through :func:`~apm_cli.core.install_audit.decide_for_install`
and into :func:`~apm_cli.install.phases.audit.run`, verifying the policy floor
semantics described in :mod:`apm_cli.core.install_audit`:

  * ``security.audit.on_install: block`` prevents relaxation via ``--no-audit``.
  * ``security.audit.external: [skillspector]`` requires a scanner and
    fail-closes if unavailable.
  * ``--no-policy`` bypasses the floor entirely.
  * ``--force`` downgrades a block to a warn.

All I/O is mocked (native scanner, external scanner, experimental flag,
config) so no marker is required.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.install.phases import audit as audit_phase
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, AuditPolicy, SecurityPolicy
from apm_cli.security.content_scanner import ScanFinding
from apm_cli.security.external.base import ExternalScanError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeLogger:
    """Captures ``warning`` and ``verbose_detail`` calls."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.details: list[str] = []

    def warning(self, msg: str, *_args: Any, **_kw: Any) -> None:
        self.warnings.append(msg)

    def verbose_detail(self, msg: str) -> None:
        self.details.append(msg)


class _FakeDiagnostics:
    """Captures ``security`` diagnostic calls."""

    def __init__(self) -> None:
        self.security_calls: list[dict[str, str]] = []

    def security(
        self,
        message: str,
        package: str = "",
        detail: str = "",
        severity: str = "warning",
    ) -> None:
        self.security_calls.append(
            {"message": message, "package": package, "detail": detail, "severity": severity}
        )


def _critical_finding(*, file: str = "skill.md") -> ScanFinding:
    return ScanFinding(
        file=file,
        line=1,
        column=1,
        char="\u202e",
        codepoint="U+202E",
        severity="critical",
        category="bidi-override",
        description="Right-to-left override (RLO)",
    )


def _make_policy(
    *,
    on_install: str = "block",
    external: tuple[str, ...] | None = None,
) -> ApmPolicy:
    """Build an ``ApmPolicy`` with the given audit floor and external scanners."""
    audit = AuditPolicy(on_install=on_install, external=external)
    security = SecurityPolicy(audit=audit)
    return ApmPolicy(enforcement="warn", security=security)


def _make_fetch_result(policy: ApmPolicy) -> PolicyFetchResult:
    return PolicyFetchResult(
        policy=policy,
        source="org:test-org/.github",
        cached=False,
        error=None,
        cache_age_seconds=None,
        cache_stale=False,
        fetch_error=None,
        outcome="found",
    )


def _make_ctx(
    tmp_path: Path,
    *,
    policy: ApmPolicy | None = None,
    no_policy: bool = False,
    audit_override: str | None = None,
    force: bool = False,
) -> SimpleNamespace:
    """Build a minimal duck-typed ``InstallContext`` for the audit phase."""
    fetch = _make_fetch_result(policy) if policy is not None else None
    logger = _FakeLogger()
    diag = _FakeDiagnostics()
    return SimpleNamespace(
        project_root=tmp_path,
        logger=logger,
        diagnostics=diag,
        force=force,
        no_policy=no_policy,
        policy_fetch=fetch,
        audit_override=audit_override,
    )


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the ``external_scanners`` experimental flag only."""
    monkeypatch.setattr(
        "apm_cli.core.experimental.is_enabled",
        lambda name: name == "external_scanners",
    )


def _config_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set user config ``audit-on-install`` to ``off``."""
    monkeypatch.setattr("apm_cli.config.get_audit_on_install", lambda: "off")


def _patch_native_scan(monkeypatch: pytest.MonkeyPatch, findings: dict | None = None) -> None:
    """Mock the native content scanner to return given findings (or empty)."""
    result = findings if findings is not None else {}
    monkeypatch.setattr(
        "apm_cli.security.file_scanner.scan_lockfile_packages",
        lambda *_a, **_kw: (result, len(result)),
    )


def _patch_external_scanners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    findings: dict | None = None,
    error: str | None = None,
) -> None:
    """Mock the external scanner runner to return findings or raise."""
    if error is not None:

        def _raise(*_a: Any, **_kw: Any) -> None:
            raise ExternalScanError(error)

        monkeypatch.setattr("apm_cli.security.external.runner.run_external_scanners", _raise)
    else:
        result = findings if findings is not None else {}
        monkeypatch.setattr(
            "apm_cli.security.external.runner.run_external_scanners",
            lambda *_a, **_kw: result,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPolicyBlockFloor:
    """Policy ``on_install: block`` acts as a floor that cannot be relaxed."""

    def test_no_audit_flag_cannot_relax_block_floor(self, monkeypatch, tmp_path):
        """--no-audit (audit_override='off') must not bypass policy block."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch, {"skill.md": [_critical_finding()]})

        ctx = _make_ctx(tmp_path, policy=_make_policy(on_install="block"), audit_override="off")
        with pytest.raises(PolicyViolationError, match="Install-time audit blocked"):
            audit_phase.run(ctx)

    def test_no_audit_emits_override_warning(self, monkeypatch, tmp_path):
        """When policy overrides --no-audit, a warning must be logged."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch, {"skill.md": [_critical_finding()]})

        ctx = _make_ctx(tmp_path, policy=_make_policy(on_install="block"), audit_override="off")
        with pytest.raises(PolicyViolationError):
            audit_phase.run(ctx)

        assert any("--no-audit" in w for w in ctx.logger.warnings)
        assert any("--no-policy" in w for w in ctx.logger.warnings)

    def test_block_with_clean_scan_passes(self, monkeypatch, tmp_path):
        """Policy block + no findings -> no error (no false positives)."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch)

        ctx = _make_ctx(tmp_path, policy=_make_policy(on_install="block"))
        audit_phase.run(ctx)  # must not raise
        assert ctx.diagnostics.security_calls == []


class TestPolicyExternalScanner:
    """Policy ``external: [skillspector]`` requires a scanner to run."""

    def test_unavailable_scanner_fails_closed(self, monkeypatch, tmp_path):
        """Required external scanner that is not on PATH -> PolicyViolationError."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch)
        _patch_external_scanners(monkeypatch, error="skillspector not found on PATH")

        ctx = _make_ctx(
            tmp_path,
            policy=_make_policy(on_install="block", external=("skillspector",)),
        )
        with pytest.raises(PolicyViolationError, match="required external scanner"):
            audit_phase.run(ctx)

    def test_external_findings_merged_with_native(self, monkeypatch, tmp_path):
        """Both native and external findings merge; critical triggers block."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch, {"native.md": [_critical_finding(file="native.md")]})
        _patch_external_scanners(
            monkeypatch,
            findings={"ext.py": [_critical_finding(file="ext.py")]},
        )

        ctx = _make_ctx(
            tmp_path,
            policy=_make_policy(on_install="block", external=("skillspector",)),
        )
        with pytest.raises(PolicyViolationError, match="2 file") as exc_info:
            audit_phase.run(ctx)
        # Both files should appear in the summary.
        assert "2 critical" in str(exc_info.value)


class TestNoPolicyBypass:
    """``--no-policy`` bypasses the audit policy floor entirely."""

    def test_no_policy_skips_audit(self, monkeypatch, tmp_path):
        """With --no-policy, effective mode is 'off' and no scan runs."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)

        scan_called = {"native": False, "external": False}

        def _native_scan(*_a, **_kw):
            scan_called["native"] = True
            return {}, 0

        def _ext_scan(*_a, **_kw):
            scan_called["external"] = True
            return {}

        monkeypatch.setattr("apm_cli.security.file_scanner.scan_lockfile_packages", _native_scan)
        monkeypatch.setattr("apm_cli.security.external.runner.run_external_scanners", _ext_scan)

        ctx = _make_ctx(
            tmp_path,
            policy=_make_policy(on_install="block", external=("skillspector",)),
            no_policy=True,
        )
        audit_phase.run(ctx)  # must not raise
        assert not scan_called["native"], "native scan should not run under --no-policy"
        assert not scan_called["external"], "external scan should not run under --no-policy"


class TestForceDowngrade:
    """``--force`` downgrades a policy block to a warn."""

    def test_force_records_diagnostic_without_raising(self, monkeypatch, tmp_path):
        """Block + critical + --force -> diagnostic recorded, no exception."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch, {"skill.md": [_critical_finding()]})

        ctx = _make_ctx(tmp_path, policy=_make_policy(on_install="block"), force=True)
        audit_phase.run(ctx)  # must not raise

        assert len(ctx.diagnostics.security_calls) == 1
        diag = ctx.diagnostics.security_calls[0]
        assert diag["severity"] == "critical"
        assert "--force" in diag["detail"]

    def test_force_without_critical_findings_passes_cleanly(self, monkeypatch, tmp_path):
        """Block + no findings + --force -> clean pass, no diagnostics."""
        _enable_flag(monkeypatch)
        _config_off(monkeypatch)
        _patch_native_scan(monkeypatch)

        ctx = _make_ctx(tmp_path, policy=_make_policy(on_install="block"), force=True)
        audit_phase.run(ctx)
        assert ctx.diagnostics.security_calls == []
