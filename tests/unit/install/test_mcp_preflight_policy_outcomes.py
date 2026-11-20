"""MCP preflight policy outcomes that need no policy files."""

from __future__ import annotations

import os  # noqa: F401
from pathlib import Path
from typing import Optional  # noqa: F401
from unittest.mock import MagicMock, call, patch  # noqa: F401

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.install_preflight import (
    PolicyBlockError,
    run_policy_preflight,
)
from apm_cli.policy.models import CheckResult, CIAuditResult  # noqa: F401
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,  # noqa: F401
    McpTransportPolicy,  # noqa: F401
)

# -- Fixtures / helpers -----------------------------------------------


# Registry MCP servers resolve as stdio packages (mcp.transport.allow).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stdio_mcp_registry")]


def _make_fetch_result(
    policy: ApmPolicy | None = None,
    outcome: str = "found",
    source: str = "org:test-org/.github",
) -> PolicyFetchResult:
    """Build a PolicyFetchResult for testing."""
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=False,
        outcome=outcome,
    )


def _make_mcp_dep(
    name: str,
    transport: str | None = None,
    registry=None,
    url: str | None = None,
) -> MCPDependency:
    """Build a minimal MCPDependency for policy checks."""
    return MCPDependency(
        name=name,
        transport=transport,
        registry=registry,
        url=url,
    )


def _make_logger(**kwargs) -> InstallLogger:
    """Create a real InstallLogger (not a mock) so policy methods work."""
    return InstallLogger(verbose=kwargs.get("verbose", False))


def _patch_discover(fetch_result: PolicyFetchResult):
    """Return a patch context manager for discover_policy."""
    return patch(
        "apm_cli.policy.install_preflight.discover_policy_with_chain",
        return_value=fetch_result,
    )


# -- Test: escape hatches (--no-policy, APM_POLICY_DISABLE) -----------


class TestEscapeHatches:
    def test_no_policy_flag_skips_preflight(self):
        """--no-policy skips discovery entirely and logs loud warning."""
        logger = _make_logger()
        with patch.object(logger, "policy_disabled") as mock_disabled:
            result, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("io.github.untrusted/evil")],
                no_policy=True,
                logger=logger,
            )

        assert result is None
        assert active is False
        mock_disabled.assert_called_once_with("--no-policy")

    def test_env_disable_skips_preflight(self, monkeypatch):
        """APM_POLICY_DISABLE=1 skips discovery entirely."""
        monkeypatch.setenv("APM_POLICY_DISABLE", "1")
        logger = _make_logger()
        with patch.object(logger, "policy_disabled") as mock_disabled:
            result, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("io.github.untrusted/evil")],
                no_policy=False,
                logger=logger,
            )

        assert result is None
        assert active is False
        mock_disabled.assert_called_once_with("APM_POLICY_DISABLE=1")


# -- Test: allowed MCP proceeds ---------------------------------------


class TestAllowedMCPProceeds:
    def test_direct_mcp_plural_targets_are_policy_checked(self):
        """Direct --mcp applies compilation policy to the whole target set."""
        policy = ApmPolicy(
            enforcement="block",
            compilation=CompilationPolicy(
                target=CompilationTargetPolicy(allow=("copilot",)),
            ),
        )
        fetch = _make_fetch_result(policy=policy)

        with _patch_discover(fetch), pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("test-server", transport="http")],
                effective_target=("copilot", "claude"),
                logger=_make_logger(),
            )


# -- Test: denied MCP under block -> abort ----------------------------


# -- Test: denied MCP under warn -> proceeds with diagnostic ----------


# -- Test: transport.allow blocks non-allowed transport ----------------


# -- Test: self_defined rule -------------------------------------------


# -- Test: trust_transitive: false blocks unapproved transitives ------


# -- Test: discovery outcomes (no policy, malformed, etc.) -----------


class TestDiscoveryOutcomes:
    def test_no_policy_found_proceeds_silently(self):
        """Absent policy -> enforcement_active=False, no exception."""
        fetch = PolicyFetchResult(
            policy=None,
            source="org:test-org/.github",
            outcome="absent",
        )

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("anything/server")],
                logger=logger,
            )

        assert result is not None
        assert active is False

    def test_enforcement_off_does_not_check(self):
        """enforcement=off -> no checks run, enforcement_active=False."""
        policy = ApmPolicy(enforcement="off")
        fetch = _make_fetch_result(policy=policy)

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("io.github.untrusted/evil")],
                logger=logger,
            )

        assert active is False

    def test_no_git_remote_outcome(self):
        """no_git_remote outcome -> enforcement_active=False, silent in non-verbose.

        UX F2 + #832: this is a normal state for fresh `git init`,
        unpacked bundles, or temp dirs.  Verbose-gated so the majority
        of users without an org policy don't see a line on every
        install (fresh checkouts, CI, unpacked tarballs).
        """
        fetch = PolicyFetchResult(
            policy=None,
            source="",
            outcome="no_git_remote",
        )

        # Non-verbose: no info / warning emitted at all.
        logger = _make_logger(verbose=False)
        with (
            _patch_discover(fetch),
            patch("apm_cli.core.command_logger._rich_info") as mock_info,
            patch("apm_cli.core.command_logger._rich_warning") as mock_warning,
        ):
            result, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("anything/server")],
                logger=logger,
            )

        assert active is False
        assert mock_info.call_count == 0
        assert mock_warning.call_count == 0

        # Verbose: the info line surfaces with the explanatory text.
        logger = _make_logger(verbose=True)
        with _patch_discover(fetch), patch("apm_cli.core.command_logger._rich_info") as mock_info:
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[_make_mcp_dep("anything/server")],
                logger=logger,
            )

        assert active is False
        assert mock_info.call_count >= 1
        info_messages = [str(c) for c in mock_info.call_args_list]
        assert any("git remote" in msg for msg in info_messages)


# -- Test: helper return shape ----------------------------------------
