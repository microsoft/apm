"""``apm audit`` when org policy discovery cannot resolve a policy.

A project that sets ``policy.fetch_failure_default: block`` requires an org
policy. When discovery raises or resolves nothing, the policy-outcome owner
routes that as a fetch failure: a required policy fails the audit with a
named ``[x]`` cause, an optional one stays advisory. Only discovery (the
network boundary) and the drift replay are replaced; the audit command and
the routing owner run for real.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, AuditPolicy, SecurityPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-outcome-routing")]

_DISCOVER = "apm_cli.policy.discovery.discover_policy_with_chain"
_BLOCK = "policy:\n  fetch_failure_default: block\n"
_CAUSE = "Policy fetch failed: policy discovery failed (RuntimeError)"
_UNRESOLVED = "Org policy could not be resolved; fail_on_drift could not be applied: "
_RAISES = RuntimeError("transport detail")


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    clear_apm_yml_cache()
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00Z'\ndependencies: []\n",
        encoding="utf-8",
    )
    prompts = tmp_path / ".github" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "hello.prompt.md").write_text("Say hello.\n", encoding="utf-8")
    yield
    clear_apm_yml_cache()


def _manifest(tmp_path, extra: str = "") -> None:
    (tmp_path / "apm.yml").write_text("name: demo\nversion: '1.0.0'\n" + extra, encoding="utf-8")


def _bare_audit(discover: dict):
    """Run bare ``apm audit`` with a drift check that failed to run."""
    drift = MagicMock(passed=False, message="drift replay unsupported")
    with (
        patch("apm_cli.policy.ci_checks._check_drift", return_value=(drift, [])),
        patch(_DISCOVER, **discover) as mock_discover,
    ):
        result = CliRunner().invoke(audit, [])
    return result, mock_discover


def _found(*, fail_on_drift: bool) -> PolicyFetchResult:
    policy = ApmPolicy(security=SecurityPolicy(audit=AuditPolicy(fail_on_drift=fail_on_drift)))
    return PolicyFetchResult(policy=policy, source="org:o/.github", outcome="found")


class TestBareAudit:
    def test_required_policy_discovery_error_fails_closed(self, tmp_path):
        _manifest(tmp_path, _BLOCK)

        result, _ = _bare_audit({"side_effect": _RAISES})

        assert result.exit_code == 1, result.output
        assert f"[x] {_UNRESOLVED}{_CAUSE} (policy.fetch_failure_default=block)\n" in (
            result.stderr
        )
        assert "transport detail" not in result.output

    def test_required_policy_resolving_nothing_fails_closed(self, tmp_path):
        _manifest(tmp_path, _BLOCK)
        absent = PolicyFetchResult(policy=None, source="org:o/.github", outcome="absent")

        result, _ = _bare_audit({"return_value": absent})

        assert result.exit_code == 1, result.output
        assert (
            f"[x] {_UNRESOLVED}No org policy found at org:o/.github "
            "(policy.fetch_failure_default=block)\n" in result.stderr
        )

    def test_optional_policy_discovery_error_stays_advisory(self, tmp_path):
        _manifest(tmp_path)

        result, _ = _bare_audit({"side_effect": _RAISES})

        assert result.exit_code == 0, result.output
        assert f"[!] {_UNRESOLVED}{_CAUSE}\n" in result.stderr
        assert "[x]" not in result.output

    def test_resolved_policy_without_fail_on_drift_is_unchanged(self, tmp_path):
        _manifest(tmp_path, _BLOCK)

        result, mock_discover = _bare_audit({"return_value": _found(fail_on_drift=False)})

        mock_discover.assert_called_once()
        assert result.exit_code == 0, result.output
        assert _UNRESOLVED not in result.output

    def test_policy_disable_env_skips_discovery(self, tmp_path, monkeypatch):
        _manifest(tmp_path, _BLOCK)
        monkeypatch.setenv("APM_POLICY_DISABLE", "1")

        result, mock_discover = _bare_audit({"side_effect": _RAISES})

        mock_discover.assert_not_called()
        assert result.exit_code == 0, result.output
        assert _UNRESOLVED not in result.output


class TestCiAudit:
    def test_required_policy_discovery_error_exits_one_with_named_cause(self, tmp_path):
        _manifest(tmp_path, _BLOCK)

        with patch(_DISCOVER, side_effect=_RAISES):
            result = CliRunner().invoke(audit, ["--ci", "--no-drift"])

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert f"[x] {_CAUSE} (policy.fetch_failure_default=block)\n" in result.stderr
        assert "transport detail" not in result.output

    def test_optional_policy_discovery_error_warns_and_proceeds(self, tmp_path):
        _manifest(tmp_path)

        with patch(_DISCOVER, side_effect=_RAISES):
            result = CliRunner().invoke(audit, ["--ci", "--no-drift"])

        assert result.exit_code == 0, result.output
        assert f"[!] {_CAUSE}; enforcement skipped" in result.stderr
        assert "[x]" not in result.output
