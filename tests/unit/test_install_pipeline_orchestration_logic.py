"""Unit tests for apm_cli.install.pipeline (pure logic cases)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _run_phase
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_non_verbose_calls_phase(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = False
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "result"
        result = _run_phase("myphase", phase, ctx)
        phase.run.assert_called_once_with(ctx)
        assert result == "result"

    def test_verbose_calls_phase_and_logs(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "done"
        result = _run_phase("myphase", phase, ctx)
        assert result == "done"
        ctx.logger.verbose_detail.assert_called_once()

    def test_verbose_phase_exception_propagates(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.side_effect = RuntimeError("phase failed")
        with pytest.raises(RuntimeError, match="phase failed"):
            _run_phase("myphase", phase, ctx)

    def test_no_logger_falls_through(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = None
        phase = MagicMock()
        phase.run.return_value = 42
        result = _run_phase("myphase", phase, ctx)
        assert result == 42


# ---------------------------------------------------------------------------
# run_install_pipeline -- empty deps short-circuit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- plan_callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- exception wrapping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check
# ---------------------------------------------------------------------------
