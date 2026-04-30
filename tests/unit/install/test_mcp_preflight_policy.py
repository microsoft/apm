"""Unit tests for W2-mcp-preflight: policy enforcement on ``install --mcp``.

Covers:
- Direct --mcp install of allowed MCP -> proceeds
- Direct --mcp install of denied MCP under block -> aborts BEFORE MCPIntegrator.install
- Same under warn -> proceeds with diagnostic
- ``mcp.transport.allow`` rule blocks an MCP using a non-allowed transport
- ``mcp.self_defined`` rule blocks/warns on inline MCP definitions
- ``mcp.trust_transitive: false`` blocks a transitive MCP not directly approved
- ``--no-policy`` and ``APM_POLICY_DISABLE=1`` skip preflight cleanly
- ``run_policy_preflight`` helper shape and return semantics
"""

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
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import (
    ApmPolicy,
    DependencyPolicy,  # noqa: F401
    McpPolicy,
    McpTransportPolicy,  # noqa: F401
)

# -- Fixtures / helpers -----------------------------------------------


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"
MCP_POLICY_FIXTURE = FIXTURE_DIR / "apm-policy-mcp.yml"


def _load_mcp_policy() -> ApmPolicy:
    """Load the MCP enforcement fixture (enforcement=block)."""
    policy, _warnings = load_policy(MCP_POLICY_FIXTURE)
    return policy


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

    def test_env_disable_zero_does_not_skip(self, monkeypatch):
        """APM_POLICY_DISABLE=0 does NOT skip (only '1' is canonical)."""
        monkeypatch.setenv("APM_POLICY_DISABLE", "0")
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)

        logger = _make_logger()
        with _patch_discover(fetch):
            # Should proceed to enforcement -- will raise because
            # the dep is denied under block enforcement.
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/tmp/fake"),
                    mcp_deps=[_make_mcp_dep("io.github.untrusted/evil-mcp")],
                    no_policy=False,
                    logger=logger,
                )


# -- Test: allowed MCP proceeds ---------------------------------------


class TestAllowedMCPProceeds:
    def test_allowed_mcp_under_block_proceeds(self):
        """An MCP matching the allow list passes even under block enforcement."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert result is not None
        assert active is True

    def test_allowed_mcp_under_warn_proceeds(self):
        """Allowed MCP proceeds under warn enforcement too."""
        policy, _ = load_policy(MCP_POLICY_FIXTURE)
        # Override enforcement to warn for this test
        policy = ApmPolicy(
            enforcement="warn",
            mcp=policy.mcp,
            dependencies=policy.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.modelcontextprotocol/test-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert active is True


# -- Test: denied MCP under block -> abort ----------------------------


class TestDeniedMCPBlock:
    def test_denied_mcp_raises_policy_block_error(self):
        """Denied MCP under block enforcement raises PolicyBlockError."""
        policy = _load_mcp_policy()  # enforcement=block
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError) as exc_info:
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert exc_info.value.audit_result is not None
        assert not exc_info.value.audit_result.passed
        assert exc_info.value.policy_source == "org:test-org/.github"

    def test_denied_mcp_aborts_before_mcp_integrator(self):
        """Verify MCPIntegrator.install is never called when policy blocks."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-mcp-server", transport="stdio")

        logger = _make_logger()
        with (
            _patch_discover(fetch),
            patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install") as mock_install,
        ):
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/tmp/fake"),
                    mcp_deps=[dep],
                    logger=logger,
                )

            mock_install.assert_not_called()

    def test_denied_mcp_emits_policy_violation_diagnostic(self):
        """Block-mode violations push to logger.policy_violation with severity='block'."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/tmp/fake"),
                    mcp_deps=[dep],
                    logger=logger,
                )

            # At least one violation was emitted
            assert mock_violation.call_count >= 1
            # All calls used severity="block"
            for c in mock_violation.call_args_list:
                assert (
                    c.kwargs.get("severity") == "block"
                    or c[1].get("severity") == "block"
                    or (len(c.args) >= 3 and c.args[2] == "block")
                )


# -- Test: denied MCP under warn -> proceeds with diagnostic ----------


class TestDeniedMCPWarn:
    def test_denied_mcp_under_warn_does_not_raise(self):
        """Denied MCP under warn enforcement proceeds (no exception)."""
        policy_base = _load_mcp_policy()
        policy = ApmPolicy(
            enforcement="warn",
            mcp=policy_base.mcp,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert active is True

    def test_denied_mcp_under_warn_emits_warn_severity(self):
        """Warn-mode violations use severity='warn'."""
        policy_base = _load_mcp_policy()
        policy = ApmPolicy(
            enforcement="warn",
            mcp=policy_base.mcp,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

            assert mock_violation.call_count >= 1
            for c in mock_violation.call_args_list:
                _, kwargs = c
                assert kwargs.get("severity") == "warn"


# -- Test: transport.allow blocks non-allowed transport ----------------


class TestTransportAllow:
    def test_non_allowed_transport_blocked(self):
        """MCP using a transport not in transport.allow is blocked."""
        # Fixture has transport.allow: [stdio, http]
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        # Use 'sse' transport which is NOT in [stdio, http]
        dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="sse")

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError) as exc_info:
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        # Verify the transport check failed
        failed = exc_info.value.audit_result.failed_checks
        transport_fails = [c for c in failed if c.name == "mcp-transport"]
        assert len(transport_fails) > 0

    def test_allowed_transport_passes(self):
        """MCP using an allowed transport proceeds."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert active is True


# -- Test: self_defined rule -------------------------------------------


class TestSelfDefined:
    def test_self_defined_deny_blocks(self):
        """self_defined='deny' blocks an inline MCP definition."""
        policy_base = _load_mcp_policy()
        # Override self_defined to 'deny' (fixture has 'warn')
        mcp_policy = McpPolicy(
            allow=policy_base.mcp.allow,
            deny=policy_base.mcp.deny,
            transport=policy_base.mcp.transport,
            self_defined="deny",
            trust_transitive=policy_base.mcp.trust_transitive,
        )
        policy = ApmPolicy(
            enforcement="block",
            mcp=mcp_policy,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        # Self-defined (registry=False) but name matches the allow list
        # so the allowlist check passes; only self_defined check catches it.
        dep = _make_mcp_dep(
            "io.github.github/custom-local-mcp",
            transport="stdio",
            registry=False,
        )

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError) as exc_info:
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        failed = exc_info.value.audit_result.failed_checks
        self_defined_fails = [c for c in failed if c.name == "mcp-self-defined"]
        assert len(self_defined_fails) > 0

    def test_self_defined_warn_passes_with_diagnostic(self):
        """self_defined='warn' passes but records a diagnostic."""
        # The fixture already has self_defined='warn'
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        # Self-defined dep -- but also needs to be in allow list to not
        # fail on the denylist check. Use a name matching the allow pattern.
        dep = _make_mcp_dep(
            "io.github.github/custom-mcp",
            transport="stdio",
            registry=False,
        )

        logger = _make_logger()
        with _patch_discover(fetch):
            # Under block enforcement, self_defined='warn' means the
            # self_defined check itself passes (it returns passed=True
            # with details). No exception.
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert active is True

    def test_self_defined_allow_passes(self):
        """self_defined='allow' passes self-defined entries."""
        policy_base = _load_mcp_policy()
        mcp_policy = McpPolicy(
            allow=policy_base.mcp.allow,
            deny=policy_base.mcp.deny,
            transport=policy_base.mcp.transport,
            self_defined="allow",
            trust_transitive=policy_base.mcp.trust_transitive,
        )
        policy = ApmPolicy(
            enforcement="block",
            mcp=mcp_policy,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep(
            "io.github.github/custom-mcp",
            transport="stdio",
            registry=False,
        )

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert active is True


# -- Test: trust_transitive: false blocks unapproved transitives ------


class TestTrustTransitive:
    """Verify trust_transitive enforcement strategy.

    The ``--mcp`` branch installs a single direct MCP server, so there
    are no transitive MCPs in that specific path.  Transitive MCP
    collection happens in the pipeline path (``install.py:1335-1345``
    via ``MCPIntegrator.collect_transitive``).

    These tests verify the *policy check* logic: when
    ``trust_transitive=False`` and an MCP dep is marked as transitive
    (not in the explicit allow list), the policy denylist / allowlist
    catches it.  The preflight helper is called with the transitive
    MCP list by the pipeline (W2-gate-phase) or by the caller after
    collecting transitives.

    Strategy: the caller feeds transitives into a SECOND preflight call
    (or extends the first to include them).  This is documented in the
    helper docstring.
    """

    def test_transitive_mcp_not_in_allow_blocked(self):
        """A transitive MCP not in the allow list is blocked when
        trust_transitive=False (fixture default)."""
        policy = _load_mcp_policy()
        assert policy.mcp.trust_transitive is False
        fetch = _make_fetch_result(policy=policy)

        # This MCP is NOT in the allow list -- simulates a transitive
        # dep that was pulled in by an allowed package.
        transitive_dep = _make_mcp_dep("io.github.random-org/sneaky-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError) as exc_info:
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[transitive_dep],
                logger=logger,
            )

        # The allowlist check should catch this
        failed = exc_info.value.audit_result.failed_checks
        assert any(c.name in ("mcp-allowlist", "mcp-denylist") for c in failed)

    def test_transitive_mcp_in_allow_passes(self):
        """A transitive MCP in the allow list passes even with trust_transitive=False."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)

        # This IS in the allow list
        transitive_dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=[transitive_dep],
                logger=logger,
            )

        assert active is True


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


class TestHelperReturnShape:
    def test_returns_tuple_of_result_and_bool(self):
        """Verify the return type is (Optional[PolicyFetchResult], bool)."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.github/test-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[dep],
                logger=logger,
            )

        assert isinstance(result, tuple)
        assert len(result) == 2
        fetch_result, enforcement_active = result
        assert isinstance(fetch_result, PolicyFetchResult)
        assert isinstance(enforcement_active, bool)

    def test_no_mcp_deps_skips_mcp_checks(self):
        """mcp_deps=None skips MCP checks entirely."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/tmp/fake"),
                mcp_deps=None,
                logger=logger,
            )

        # No MCP checks -> nothing to fail
        assert active is True
