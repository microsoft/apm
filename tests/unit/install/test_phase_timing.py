"""Unit tests for ``_run_phase`` verbose timing (F6, microsoft/apm#1116).

The pipeline wraps every ``phase.run(ctx)`` call so verbose mode emits
``[i] Phase: <name> -> 1.234s`` for each phase. Non-verbose mode must
stay byte-identical to the legacy direct-call path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from apm_cli.install.pipeline import _run_phase


def _make_phase(return_value=None, raise_exc=None):
    phase = MagicMock()
    if raise_exc is not None:
        phase.run.side_effect = raise_exc
    else:
        phase.run.return_value = return_value
    return phase


def test_run_phase_no_verbose_does_not_call_logger():
    logger = MagicMock()
    ctx = SimpleNamespace(logger=logger, verbose=False)
    phase = _make_phase(return_value="done")
    result = _run_phase("resolve", phase, ctx)
    assert result == "done"
    phase.run.assert_called_once_with(ctx)
    logger.verbose_detail.assert_not_called()


def test_run_phase_verbose_emits_timing_line():
    logger = MagicMock()
    ctx = SimpleNamespace(logger=logger, verbose=True)
    phase = _make_phase(return_value=None)
    _run_phase("download", phase, ctx)
    assert logger.verbose_detail.call_count == 1
    msg = logger.verbose_detail.call_args.args[0]
    assert msg.startswith("Phase: download -> ")
    assert msg.endswith("s")


def test_run_phase_returns_phase_return_value():
    logger = MagicMock()
    ctx = SimpleNamespace(logger=logger, verbose=True)
    phase = _make_phase(return_value={"installed": 5})
    assert _run_phase("finalize", phase, ctx) == {"installed": 5}


def test_run_phase_emits_timing_even_on_exception():
    logger = MagicMock()
    ctx = SimpleNamespace(logger=logger, verbose=True)
    phase = _make_phase(raise_exc=RuntimeError("boom"))
    try:
        _run_phase("integrate", phase, ctx)
    except RuntimeError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("RuntimeError should have propagated")
    logger.verbose_detail.assert_called_once()
    assert logger.verbose_detail.call_args.args[0].startswith("Phase: integrate -> ")


def test_run_phase_logger_failure_does_not_mask_phase_exception():
    logger = MagicMock()
    logger.verbose_detail.side_effect = RuntimeError("logger down")
    ctx = SimpleNamespace(logger=logger, verbose=True)
    phase = _make_phase(raise_exc=ValueError("phase boom"))
    try:
        _run_phase("cleanup", phase, ctx)
    except ValueError as e:
        assert str(e) == "phase boom"
    else:
        raise AssertionError("phase ValueError should propagate, not the logger RuntimeError")


def test_run_phase_no_logger_skips_timing():
    """Some phases run with ``ctx.logger=None``; must not crash."""
    ctx = SimpleNamespace(logger=None, verbose=True)
    phase = _make_phase(return_value="ok")
    assert _run_phase("targets", phase, ctx) == "ok"
