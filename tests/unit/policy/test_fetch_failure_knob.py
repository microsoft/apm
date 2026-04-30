"""Tests for policy.fetch_failure schema knob and project-side
policy.fetch_failure_default override (closes #829).

The org-side ``ApmPolicy.fetch_failure`` knob applies when a cached
policy is available (read off the ``ApmPolicy``); the project-side
``apm.yml`` ``policy.fetch_failure_default`` knob applies when no
policy is available at all (cache_miss_fetch_fail / garbage_response /
malformed). Both default to ``"warn"`` for backwards compatibility.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.phases.policy_gate import PolicyViolationError, run
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.parser import PolicyValidationError, load_policy
from apm_cli.policy.project_config import read_project_fetch_failure_default
from apm_cli.policy.schema import ApmPolicy

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


class TestProjectFetchFailureDefault:
    def test_missing_apm_yml_returns_warn(self, tmp_path: Path):
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_apm_yml_without_policy_block_returns_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("name: p\nversion: '1.0'\n", encoding="utf-8")
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_explicit_block(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: block
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "block"

    def test_explicit_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: warn
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_garbage_value_falls_back_to_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: bogus
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_malformed_yaml_returns_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(":::not yaml:::\n", encoding="utf-8")
        assert read_project_fetch_failure_default(tmp_path) == "warn"


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
    def test_absent_block_does_not_raise(self, mock_discover):
        """absent / no_git_remote / empty are NOT fetch failures."""
        mock_discover.return_value = _fetch("absent", source="org:foo/.github")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        run(ctx)  # Must not raise
        assert ctx.policy_enforcement_active is False

    @patch(_PATCH_DISCOVER)
    def test_no_git_remote_block_does_not_raise(self, mock_discover):
        mock_discover.return_value = _fetch("no_git_remote", source="")
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        run(ctx)
        assert ctx.policy_enforcement_active is False


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


class TestPreflightFailClosed:
    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_block_raises_PolicyBlockError(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import (
            PolicyBlockError,
            run_policy_preflight,
        )

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: '1.0'\npolicy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )
        with pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=tmp_path,
                apm_deps=[],
                no_policy=False,
                logger=MagicMock(),
            )

    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_warn_does_not_raise(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import run_policy_preflight

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        # No apm.yml -> default warn.
        result, active = run_policy_preflight(  # noqa: RUF059
            project_root=tmp_path,
            apm_deps=[],
            no_policy=False,
            logger=MagicMock(),
        )
        assert active is False

    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_dry_run_block_does_not_raise(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import run_policy_preflight

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: '1.0'\npolicy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )
        # dry_run never raises.
        result, active = run_policy_preflight(  # noqa: RUF059
            project_root=tmp_path,
            apm_deps=[],
            no_policy=False,
            logger=MagicMock(),
            dry_run=True,
        )
        assert active is False
