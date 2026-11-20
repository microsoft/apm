"""--no-policy escape hatches and warnings."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A minimal apm.yml that has no deps -- used for bare-install and add-pkg tests.


# Import the real PolicyViolationError from the gate phase.
try:
    from apm_cli.install.phases.policy_gate import PolicyViolationError
except ImportError:  # pragma: no cover

    class PolicyViolationError(RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Help text validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm install --no-policy (bare install -- denied dep in block mode)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm install <pkg> --no-policy -> apm.yml retains the new dep
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm install --mcp <denied> --no-policy -> proceeds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm update --no-policy -> rejected (apm update is CLI self-update, not deps)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# APM_POLICY_DISABLE=1 env var (no --no-policy flag)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sanity: without --no-policy AND denied dep -> install fails
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Loud warnings visible even without --verbose
# ---------------------------------------------------------------------------


class TestLoudWarningsWithoutVerbose:
    """policy_disabled warning is always visible (not gated by --verbose)."""

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_policy_disabled_warning_non_verbose_flag(self, mock_warning):
        """--no-policy reason produces warning even when verbose=False."""
        from apm_cli.core.command_logger import InstallLogger

        logger = InstallLogger(verbose=False)
        logger.policy_disabled("--no-policy")

        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "--no-policy" in msg
        assert "for this invocation" in msg
        assert "does NOT bypass apm audit --ci" in msg
        assert "CI will still fail the PR" in msg

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_policy_disabled_warning_non_verbose_env(self, mock_warning):
        """APM_POLICY_DISABLE=1 reason produces warning even when verbose=False."""
        from apm_cli.core.command_logger import InstallLogger

        logger = InstallLogger(verbose=False)
        logger.policy_disabled("APM_POLICY_DISABLE=1")

        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "APM_POLICY_DISABLE=1" in msg
        assert "for this invocation" in msg
        assert "does NOT bypass apm audit --ci" in msg
        assert "CI will still fail the PR" in msg

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_warning_text_is_ascii(self, mock_warning):
        """Loud warning text must be plain ASCII per cli.instructions.md."""
        from apm_cli.core.command_logger import InstallLogger

        logger = InstallLogger(verbose=False)
        logger.policy_disabled("--no-policy")

        msg = mock_warning.call_args[0][0]
        assert msg.isascii(), f"Non-ASCII in policy_disabled output: {msg!r}"


# ---------------------------------------------------------------------------
# Policy gate unit test: ctx.no_policy + env var skip enforcement
# ---------------------------------------------------------------------------


class TestPolicyGateEscapeHatch:
    """The policy_gate phase respects no_policy flag and APM_POLICY_DISABLE env."""

    def test_ctx_no_policy_skips_gate(self):
        """When ctx.no_policy=True, policy_gate.run returns immediately."""
        from apm_cli.install.phases.policy_gate import run as run_gate

        ctx = MagicMock()
        ctx.no_policy = True
        ctx.logger = MagicMock()

        run_gate(ctx)

        ctx.logger.policy_disabled.assert_called_once_with("--no-policy")

    def test_env_var_skips_gate(self):
        """When APM_POLICY_DISABLE=1, policy_gate.run returns immediately."""
        from apm_cli.install.phases.policy_gate import run as run_gate

        ctx = MagicMock()
        ctx.no_policy = False
        ctx.logger = MagicMock()

        env_patch = patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"})
        with env_patch:
            run_gate(ctx)

        ctx.logger.policy_disabled.assert_called_once_with("APM_POLICY_DISABLE=1")

    def test_env_var_zero_does_not_skip(self):
        """APM_POLICY_DISABLE=0 does not trigger the escape hatch."""
        from apm_cli.install.phases.policy_gate import _is_policy_disabled

        ctx = MagicMock()
        ctx.no_policy = False
        ctx.logger = MagicMock()

        env_patch = patch.dict(os.environ, {"APM_POLICY_DISABLE": "0"}, clear=False)
        with env_patch:
            result = _is_policy_disabled(ctx)

        assert result is False
        ctx.logger.policy_disabled.assert_not_called()

    def test_env_var_unset_does_not_skip(self):
        """When APM_POLICY_DISABLE is not set, escape hatch does not trigger."""
        from apm_cli.install.phases.policy_gate import _is_policy_disabled

        ctx = MagicMock()
        ctx.no_policy = False
        ctx.logger = MagicMock()

        env_patch = patch.dict(os.environ, {}, clear=True)
        with env_patch:
            # Ensure APM_POLICY_DISABLE is definitely not set
            os.environ.pop("APM_POLICY_DISABLE", None)
            result = _is_policy_disabled(ctx)

        assert result is False
        ctx.logger.policy_disabled.assert_not_called()


# ---------------------------------------------------------------------------
# Install preflight (--mcp) escape hatch
# ---------------------------------------------------------------------------


class TestPreflightEscapeHatch:
    """run_policy_preflight respects no_policy and APM_POLICY_DISABLE."""

    def test_no_policy_true_skips_preflight(self):
        """no_policy=True -> preflight returns (None, False) immediately."""
        from apm_cli.policy.install_preflight import run_policy_preflight

        logger = MagicMock()
        fetch, active = run_policy_preflight(
            project_root=Path("/tmp/fake"),
            mcp_deps=[],
            no_policy=True,
            logger=logger,
        )

        assert fetch is None
        assert active is False
        logger.policy_disabled.assert_called_once_with("--no-policy")

    def test_env_var_skips_preflight(self):
        """APM_POLICY_DISABLE=1 -> preflight returns (None, False) immediately."""
        from apm_cli.policy.install_preflight import run_policy_preflight

        logger = MagicMock()
        env_patch = patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"})
        with env_patch:
            fetch, active = run_policy_preflight(
                project_root=Path("/tmp/fake"),
                mcp_deps=[],
                no_policy=False,
                logger=logger,
            )

        assert fetch is None
        assert active is False
        logger.policy_disabled.assert_called_once_with("APM_POLICY_DISABLE=1")


# ---------------------------------------------------------------------------
# InstallRequest and pipeline wiring
# ---------------------------------------------------------------------------


class TestInstallRequestNoPolicy:
    """InstallRequest carries no_policy through to the pipeline."""

    def test_install_request_has_no_policy_field(self):
        """InstallRequest dataclass exposes no_policy with default False."""
        from apm_cli.install.request import InstallRequest

        # Default
        req = InstallRequest(apm_package=MagicMock())
        assert req.no_policy is False

        # Explicit True
        req = InstallRequest(apm_package=MagicMock(), no_policy=True)
        assert req.no_policy is True

    def test_install_context_has_no_policy_field(self):
        """InstallContext dataclass exposes no_policy with default False."""
        from apm_cli.install.context import InstallContext

        ctx = InstallContext(
            project_root=Path("/tmp/fake"),
            apm_dir=Path("/tmp/fake/.apm"),
        )
        assert ctx.no_policy is False

        ctx = InstallContext(
            project_root=Path("/tmp/fake"),
            apm_dir=Path("/tmp/fake/.apm"),
            no_policy=True,
        )
        assert ctx.no_policy is True
