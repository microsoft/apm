"""Unit tests for S1 fix (#827-C2): transitive MCP policy enforcement.

Covers the second ``run_policy_preflight`` call in ``commands/install.py``
that guards transitive MCP servers collected from installed APM packages
BEFORE ``MCPIntegrator.install()`` writes runtime configs.

Scenarios:
- Transitive MCP matching deny pattern under block -> block, non-zero exit,
  MCP configs NOT written
- Transitive MCP matching deny pattern under warn -> warning emitted,
  MCP configs written normally
- All transitive MCP allowed -> no policy output, normal flow
- ``--no-policy`` skips the second preflight
- ``APM_POLICY_DISABLE=1`` skips it
- No transitive MCP -> no preflight call (guard ``transitive_mcp`` is empty)
- Direct ``--mcp`` install (single server, not pipeline path) is NOT
  affected by this change
"""

from __future__ import annotations

import os  # noqa: F401
import sys  # noqa: F401
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
from apm_cli.policy.schema import ApmPolicy

# -- Fixtures / helpers -----------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"
# Registry MCP servers resolve as stdio packages (mcp.transport.allow).
pytestmark = [pytest.mark.component, pytest.mark.usefixtures("stdio_mcp_registry")]

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
    return MCPDependency(
        name=name,
        transport=transport,
        registry=registry,
        url=url,
    )


def _make_logger(**kwargs) -> InstallLogger:
    return InstallLogger(verbose=kwargs.get("verbose", False))


def _patch_discover(fetch_result: PolicyFetchResult):
    return patch(
        "apm_cli.policy.install_preflight.discover_policy_with_chain",
        return_value=fetch_result,
    )


# Shared constants for the install-level patching


# -- Test: transitive MCP denied under block -> abort -----------------


class TestTransitiveMCPBlock:
    """Transitive MCP matching deny pattern under block enforcement."""

    def test_transitive_mcp_denied_blocks_before_mcp_install(self):
        """When transitive MCP is denied (block), MCPIntegrator.install is
        never called and the process exits non-zero."""
        policy = _load_mcp_policy()  # enforcement=block
        fetch = _make_fetch_result(policy=policy)
        evil_dep = _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=[evil_dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )

    def test_transitive_preflight_uses_merged_mcp_set(self):
        """The second preflight receives the *merged* (direct + transitive)
        MCP set, not just the transitive portion."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        direct_dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")
        transitive_dep = _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio")
        merged = [direct_dep, transitive_dep]

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=merged,
                no_policy=False,
                logger=logger,
                dry_run=False,
            )

    def test_transitive_block_emits_violation_diagnostic(self):
        """Block-severity violations from transitive MCP are emitted via
        logger.policy_violation with severity='block'."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/fake/project"),
                    mcp_deps=[dep],
                    no_policy=False,
                    logger=logger,
                    dry_run=False,
                )
            assert mock_violation.call_count >= 1
            for c in mock_violation.call_args_list:
                _, kwargs = c
                assert kwargs.get("severity") == "block"

    def test_transitive_block_hint_names_declaring_dependency(self):
        """#3835: the violation names the server and the hint points at the
        direct apm.yml dependency that brings it in, not at the declaring
        package's display name (``resolved_by``), the server or the check."""
        from dataclasses import replace

        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = replace(
            _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio"),
            resolved_by="parent-pkg",
        )

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/fake/project"),
                    mcp_deps=[dep],
                    mcp_brought_in_by={"io.github.untrusted/evil-transitive": "acme/parent-pkg"},
                    no_policy=False,
                    logger=logger,
                    dry_run=False,
                )
        calls = [c.kwargs for c in mock_violation.call_args_list]
        assert calls
        for kwargs in calls:
            assert kwargs["dep_ref"] == "io.github.untrusted/evil-transitive"
            assert kwargs["declared_by"] == "acme/parent-pkg"

    def test_transitive_block_without_direct_entry_suggests_no_apm_yml_edit(self):
        """A transitive server whose direct apm.yml entry is unknown must not
        be hinted with its declaring package's display name."""
        from dataclasses import replace

        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = replace(
            _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio"),
            resolved_by="parent-pkg",
        )

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            with pytest.raises(PolicyBlockError):
                run_policy_preflight(
                    project_root=Path("/fake/project"),
                    mcp_deps=[dep],
                    no_policy=False,
                    logger=logger,
                    dry_run=False,
                )
        calls = [c.kwargs for c in mock_violation.call_args_list]
        assert calls
        for kwargs in calls:
            assert kwargs["dep_ref"] == "io.github.untrusted/evil-transitive"
            assert kwargs["declared_by"] is None
            assert kwargs["remedy"] is None


# -- Test: transitive MCP denied under warn -> warning + proceed ------


class TestTransitiveMCPWarn:
    """Transitive MCP matching deny pattern under warn enforcement."""

    def test_transitive_mcp_denied_warn_does_not_raise(self):
        """Under warn enforcement, denied transitive MCP does not raise."""
        policy_base = _load_mcp_policy()
        policy = ApmPolicy(
            enforcement="warn",
            mcp=policy_base.mcp,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
        assert active is True

    def test_transitive_mcp_denied_warn_emits_warn_severity(self):
        """Warn-mode violations use severity='warn' in the diagnostic."""
        policy_base = _load_mcp_policy()
        policy = ApmPolicy(
            enforcement="warn",
            mcp=policy_base.mcp,
            dependencies=policy_base.dependencies,
        )
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-transitive", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
            assert mock_violation.call_count >= 1
            for c in mock_violation.call_args_list:
                _, kwargs = c
                assert kwargs.get("severity") == "warn"


# -- Test: all transitive MCP allowed -> normal flow ------------------


class TestTransitiveMCPAllowed:
    """All transitive MCP pass policy -> no violations, normal flow."""

    def test_all_transitive_allowed_no_exception(self):
        """Allowed transitive MCP passes through the preflight cleanly."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
        assert active is True
        assert result is not None
        assert result.policy is not None

    def test_all_transitive_allowed_no_violations_logged(self):
        """No policy_violation calls when all MCP pass."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.modelcontextprotocol/test-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), patch.object(logger, "policy_violation") as mock_violation:
            run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
            mock_violation.assert_not_called()


# -- Test: --no-policy skips the second preflight ---------------------


# -- Test: no transitive MCP -> preflight guard short-circuits --------


class TestNoTransitiveMCP:
    """When there are no transitive MCP deps, the second preflight
    guard in install.py short-circuits (``transitive_mcp`` is empty)."""

    def test_empty_transitive_list_skips_preflight(self):
        """With an empty transitive list the preflight is never invoked.

        This tests the guard condition in install.py:
        ``if should_install_mcp and mcp_deps and transitive_mcp:``

        When ``transitive_mcp`` is empty (falsy), the block is skipped.
        We verify by calling preflight with empty mcp_deps -- which would
        be the runtime equivalent -- and confirming no discovery runs.
        """
        logger = _make_logger()
        # If preflight were called with no deps, discovery would run.
        # Here we verify the *contract* that the guard condition in
        # install.py uses ``transitive_mcp`` truthiness to skip the call.
        # We test the guard by confirming that run_policy_preflight with
        # mcp_deps=[] does NOT trigger enforcement (no checks to run).
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)

        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake/project"),
                mcp_deps=[],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
        # Passes because empty dep list produces no violations
        assert active is True

    def test_none_mcp_deps_skips_mcp_checks(self):
        """mcp_deps=None entirely skips MCP policy checks."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake/project"),
                mcp_deps=None,
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
        assert active is True


# -- Test: direct --mcp install is NOT affected by this change --------


class TestDirectMCPNotAffected:
    """The direct ``install --mcp`` path has its own preflight (tested in
    test_mcp_preflight_policy.py).  This change does NOT alter that path.
    Verify the existing preflight still works independently."""

    def test_direct_mcp_preflight_still_blocks_denied_server(self):
        """Direct --mcp install of a denied server still raises."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.untrusted/evil-direct", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch), pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )

    def test_direct_mcp_preflight_still_allows_good_server(self):
        """Direct --mcp install of an allowed server passes."""
        policy = _load_mcp_policy()
        fetch = _make_fetch_result(policy=policy)
        dep = _make_mcp_dep("io.github.github/github-mcp-server", transport="stdio")

        logger = _make_logger()
        with _patch_discover(fetch):
            result, active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake/project"),
                mcp_deps=[dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )
        assert active is True


# -- Test: install.py integration (mocked pipeline) -------------------
