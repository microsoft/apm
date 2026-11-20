"""Tests for policy.fetch_failure schema knob and project-side
policy.fetch_failure_default override (closes #829).

The org-side ``ApmPolicy.fetch_failure`` knob applies when a cached
policy is available (read off the ``ApmPolicy``); the project-side
``apm.yml`` ``policy.fetch_failure_default`` knob applies when no
policy is available at all (cache_miss_fetch_fail / garbage_response /
malformed). Both default to ``"warn"`` for backwards compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.phases.policy_gate import PolicyViolationError, run
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.parser import PolicyValidationError, load_policy
from apm_cli.policy.schema import ApmPolicy

pytestmark = pytest.mark.unit

_PATCH_DISCOVER = "apm_cli.install.phases.policy_gate._discover_with_chain"


# =====================================================================
# Parser: validates fetch_failure
# =====================================================================


class TestParserFetchFailure:
    def test_default_is_warn(self):
        policy, _ = load_policy("name: x\nversion: '1.0'")
        assert policy.fetch_failure == "warn"

    def test_explicit_warn_accepted(self):
        policy, _ = load_policy("name: x\nversion: '1.0'\nfetch_failure: warn")
        assert policy.fetch_failure == "warn"

    def test_explicit_block_accepted(self):
        policy, _ = load_policy("name: x\nversion: '1.0'\nfetch_failure: block")
        assert policy.fetch_failure == "block"

    def test_garbage_value_rejected(self):
        with pytest.raises(PolicyValidationError) as excinfo:
            load_policy("name: x\nversion: '1.0'\nfetch_failure: garbage")
        assert "fetch_failure" in str(excinfo.value)

    def test_off_rejected(self):
        # 'off' is valid for enforcement but NOT for fetch_failure.
        with pytest.raises(PolicyValidationError) as excinfo:
            load_policy("name: x\nversion: '1.0'\nfetch_failure: off")
        assert "fetch_failure" in str(excinfo.value)


# =====================================================================
# Project-side fetch_failure_default reader
# =====================================================================


# =====================================================================
# policy_gate: fail-closed behaviour
# =====================================================================


@dataclass
class _FakeCtx:
    project_root: Path = field(default_factory=lambda: Path("/tmp/fake"))
    apm_dir: Path = field(default_factory=lambda: Path("/tmp/fake/.apm"))
    verbose: bool = False
    logger: Any = None
    deps_to_install: list[Any] = field(default_factory=list)
    existing_lockfile: Any = None
    policy_fetch: Any = None
    policy_enforcement_active: bool = False
    no_policy: bool = False
    # Test-friendly override read by policy_gate._read_project_fetch_failure_default
    policy_fetch_failure_default: str = "warn"


def _fetch(outcome, *, policy=None, source="org:contoso/.github", fetch_error=None, error=None):
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=False,
        error=error,
        cache_age_seconds=None,
        cache_stale=outcome == "cached_stale",
        fetch_error=fetch_error,
        outcome=outcome,
    )


class TestPolicyGateFailClosed:
    """Install fails closed when project-side default is block."""

    @patch(_PATCH_DISCOVER)
    def test_cache_miss_fetch_fail_block_raises(self, mock_discover):
        mock_discover.return_value = _fetch(
            "cache_miss_fetch_fail", fetch_error="connection refused"
        )
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "cache_miss_fetch_fail" in str(excinfo.value)

    @patch(_PATCH_DISCOVER)
    def test_garbage_response_block_raises(self, mock_discover):
        mock_discover.return_value = _fetch("garbage_response", error="not yaml")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError):
            run(ctx)

    @patch(_PATCH_DISCOVER)
    def test_malformed_block_raises(self, mock_discover):
        mock_discover.return_value = _fetch("malformed", error="schema invalid")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError):
            run(ctx)

    @patch(_PATCH_DISCOVER)
    def test_cache_miss_fetch_fail_warn_does_not_raise(self, mock_discover):
        mock_discover.return_value = _fetch(
            "cache_miss_fetch_fail", fetch_error="connection refused"
        )
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="warn",
        )
        # Default warn behaviour: log + continue, no raise.
        run(ctx)
        assert ctx.policy_enforcement_active is False
        ctx.logger.policy_discovery_miss.assert_called_once()

    @patch(_PATCH_DISCOVER)
    def test_absent_block_raises(self, mock_discover):
        """#1159 install parity: absent/no_git_remote/empty now honour block."""
        mock_discover.return_value = _fetch("absent", source="org:foo/.github")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "no enforceable org policy" in str(excinfo.value)
        assert "outcome=absent" in str(excinfo.value)

    @patch(_PATCH_DISCOVER)
    def test_absent_block_error_carries_masked_404_hint(self, mock_discover):
        """#3754: block mode fails closed and names the masked-404 cause."""
        fetch = _fetch("absent", source="")
        fetch.discovery_hint = "foo/.github-private returned 404."
        mock_discover.return_value = fetch
        ctx = _FakeCtx(logger=MagicMock(), policy_fetch_failure_default="block")
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "foo/.github-private returned 404." in str(excinfo.value)
        # The block is the only report; no "proceeding without" miss (#4180).
        ctx.logger.policy_discovery_miss.assert_not_called()

    @patch(_PATCH_DISCOVER)
    def test_no_git_remote_block_raises(self, mock_discover):
        """#1159 install parity: no_git_remote now honours block."""
        mock_discover.return_value = _fetch("no_git_remote", source="")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "outcome=no_git_remote" in str(excinfo.value)

    @patch(_PATCH_DISCOVER)
    def test_empty_block_raises(self, mock_discover):
        """#1159 install parity: empty now honours block."""
        mock_discover.return_value = _fetch("empty", source="org:foo/.github")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "outcome=empty" in str(excinfo.value)

    @patch(_PATCH_DISCOVER)
    def test_absent_warn_does_not_raise(self, mock_discover):
        """Default warn keeps fail-open for no-policy outcomes."""
        mock_discover.return_value = _fetch("absent", source="org:foo/.github")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="warn",
        )
        run(ctx)
        assert ctx.policy_enforcement_active is False

    @patch("apm_cli.core.command_logger._rich_info")
    @patch("apm_cli.core.command_logger._rich_warning")
    @patch(_PATCH_DISCOVER)
    def test_absent_with_hint_is_silent_by_default(self, mock_discover, mock_warning, mock_info):
        """#3754: default install with no org policy prints nothing, hint included."""
        from apm_cli.core.command_logger import InstallLogger

        fetch = _fetch("absent", source="")
        fetch.discovery_hint = "foo/.github-private returned 404."
        mock_discover.return_value = fetch
        ctx = _FakeCtx(logger=InstallLogger(verbose=False), policy_fetch_failure_default="warn")
        run(ctx)
        assert ctx.policy_enforcement_active is False
        mock_warning.assert_not_called()
        mock_info.assert_not_called()


class TestPolicyGateCachedStale:
    """cached_stale reads fetch_failure off the cached ApmPolicy."""

    @patch("apm_cli.policy.policy_checks.run_dependency_policy_checks")
    @patch(_PATCH_DISCOVER)
    def test_cached_stale_block_raises_from_cached_policy(self, mock_discover, mock_checks):
        cached = ApmPolicy(enforcement="warn", fetch_failure="block")
        mock_discover.return_value = _fetch("cached_stale", policy=cached, fetch_error="timeout")
        ctx = _FakeCtx(
            logger=MagicMock(),
            # Project-side warn must NOT prevent block from cached policy.
            policy_fetch_failure_default="warn",
        )
        with pytest.raises(PolicyViolationError) as excinfo:
            run(ctx)
        assert "cached" in str(excinfo.value).lower()

    @patch("apm_cli.policy.policy_checks.run_dependency_policy_checks")
    @patch(_PATCH_DISCOVER)
    def test_cached_stale_warn_proceeds(self, mock_discover, mock_checks):
        cached = ApmPolicy(enforcement="warn", fetch_failure="warn")
        from apm_cli.policy.models import CheckResult, CIAuditResult

        mock_checks.return_value = CIAuditResult(
            checks=[CheckResult(name="x", passed=True, message="OK")]
        )
        mock_discover.return_value = _fetch("cached_stale", policy=cached, fetch_error="timeout")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="warn",
        )
        run(ctx)  # Must not raise
        assert ctx.policy_enforcement_active is True


# =====================================================================
# install_preflight parallel call site
# =====================================================================
