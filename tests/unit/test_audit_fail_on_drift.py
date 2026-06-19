"""Seam 3 (part B): ``security.audit.fail_on_drift`` exit-code escalation.

Bare ``apm audit`` treats workspace drift as advisory: it renders drift but
exits 0. When ``security.audit.fail_on_drift`` is on, a drifted workspace must
exit non-zero. Default-off must preserve today's advisory behavior exactly.

The drift detection itself is unchanged -- this key only changes the exit code,
it does not add a second drift pass. Tests mock ``_check_drift`` (the same seam
the existing suite uses) and the policy resolver helper.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_cfg(tmp_path):
    from apm_cli.commands.audit import _AuditConfig
    from apm_cli.core.command_logger import CommandLogger

    return _AuditConfig(
        project_root=tmp_path,
        logger=CommandLogger("audit", verbose=False),
        verbose=False,
        output_format="text",
        output_path=None,
    )


def _drift_return():
    drift_check = MagicMock()
    drift_check.passed = False
    drift_check.message = "drift detected"
    return drift_check, [MagicMock()]


def _drift_could_not_run():
    """A drift check that FAILED to run: passed=False with zero findings."""
    drift_check = MagicMock()
    drift_check.passed = False
    drift_check.message = "drift replay unsupported: scheme not yet handled"
    return drift_check, []


def _run(tmp_path, fail_on_drift, drift_ret=None):
    (tmp_path / "apm.yml").write_text("name: demo\n", encoding="utf-8")
    (tmp_path / "apm.lock.yaml").write_text("{}", encoding="utf-8")
    cfg = _make_cfg(tmp_path)

    from apm_cli.commands import audit as audit_mod

    with (
        patch(
            "apm_cli.policy.ci_checks._check_drift",
            return_value=drift_ret if drift_ret is not None else _drift_return(),
        ),
        patch.object(audit_mod.LockFile, "read", return_value=MagicMock()),
        patch.object(audit_mod, "scan_lockfile_packages", return_value=({}, 1)),
        patch.object(audit_mod, "_resolve_fail_on_drift", return_value=fail_on_drift),
        patch("apm_cli.install.drift.render_drift_text", return_value=""),
        pytest.raises(SystemExit) as exc,
    ):
        audit_mod._audit_content_scan(cfg, package=None, file_path=None, strip=False, dry_run=False)
    return exc.value.code


def test_drift_with_fail_on_drift_exits_nonzero(tmp_path):
    assert _run(tmp_path, fail_on_drift=True) != 0


def test_drift_default_off_exits_zero(tmp_path):
    assert _run(tmp_path, fail_on_drift=False) == 0


def test_drift_check_could_not_run_with_fail_on_drift_exits_nonzero(tmp_path):
    # A drift check that fails to RUN (passed=False, no findings) must still
    # gate when fail_on_drift is on -- matching `apm audit --ci`, which fails
    # on the same drift_check.passed signal. Otherwise the gate silently
    # fails open on a broken drift replay.
    assert _run(tmp_path, fail_on_drift=True, drift_ret=_drift_could_not_run()) != 0


def test_drift_check_could_not_run_default_off_exits_zero(tmp_path):
    assert _run(tmp_path, fail_on_drift=False, drift_ret=_drift_could_not_run()) == 0
