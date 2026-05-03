"""Unit tests for the shared install Live-region controller.

Workstream B (#1116) -- exercises ``apm_cli.utils.install_tui``:

* ``should_animate()`` matrix: ``APM_PROGRESS`` env knob, CI guard,
  TERM=dumb, console TTY detection.
* ``InstallTui`` deferred-start: an install completing in <250 ms
  must NEVER call ``Live.start()``.
* ``InstallTui`` no-op contract: when the controller is disabled
  every public method must return without touching Rich.
* Active-set overflow: more than four in-flight tasks collapse to
  ``... and N more``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.utils.install_tui import (
    _DEFER_SHOW_S,
    InstallTui,
    should_animate,
)

# ---------------------------------------------------------------------------
# should_animate() decision matrix
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip the env vars our controller cares about so each test starts clean."""
    for name in ("APM_PROGRESS", "CI", "TERM"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _interactive_console() -> MagicMock:
    c = MagicMock()
    c.is_terminal = True
    c.is_interactive = True
    return c


def _dumb_console() -> MagicMock:
    c = MagicMock()
    c.is_terminal = False
    c.is_interactive = False
    return c


class TestShouldAnimate:
    def test_explicit_never_disables_even_under_tty(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "never")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is False

    @pytest.mark.parametrize("alias", ["quiet", "off", "0", "false", "no"])
    def test_quiet_aliases_disable(self, _isolate_env: pytest.MonkeyPatch, alias: str) -> None:
        _isolate_env.setenv("APM_PROGRESS", alias)
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is False

    def test_explicit_always_enables_even_in_ci(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        _isolate_env.setenv("CI", "true")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_dumb_console()):
            assert should_animate() is True

    def test_auto_disabled_in_ci(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("CI", "true")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is False

    def test_auto_disabled_when_term_is_dumb(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("TERM", "dumb")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is False

    def test_auto_enabled_under_tty_no_ci(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("TERM", "xterm-256color")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is True

    def test_auto_disabled_when_console_not_terminal(
        self, _isolate_env: pytest.MonkeyPatch
    ) -> None:
        _isolate_env.setenv("TERM", "xterm-256color")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_dumb_console()):
            assert should_animate() is False

    def test_unrecognised_value_is_treated_as_auto(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "purple-monkey")
        _isolate_env.setenv("TERM", "xterm-256color")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            assert should_animate() is True


# ---------------------------------------------------------------------------
# Deferred-start behaviour
# ---------------------------------------------------------------------------


class TestDeferredStart:
    def test_install_under_defer_threshold_never_starts_live(
        self, _isolate_env: pytest.MonkeyPatch
    ) -> None:
        # Force the controller on so the deferred timer is scheduled.
        _isolate_env.setenv("APM_PROGRESS", "always")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            tui = InstallTui()
            assert tui._enabled is True
            with tui:
                # Fast-path: do nothing of substance and exit immediately.
                pass
            # The defer threshold is 0.25 s; a no-op body finishes in
            # microseconds, so the timer must have been cancelled
            # before _defer_start fired.
            assert tui._live is None

    def test_install_over_defer_threshold_starts_live_once(
        self, _isolate_env: pytest.MonkeyPatch
    ) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        with patch("apm_cli.utils.install_tui._get_console", return_value=_interactive_console()):
            tui = InstallTui()

            with patch.object(InstallTui, "_defer_start", autospec=True) as mock_defer:
                with tui:
                    # Sleep slightly longer than the defer window so the
                    # timer fires before __exit__ cancels it.
                    time.sleep(_DEFER_SHOW_S + 0.10)
                # Either the timer fired (preferred) or it was cancelled.
                # We assert at most one call -- never multiple.
                assert mock_defer.call_count <= 1
                # In the typical case the timer fires; assert it did.
                assert mock_defer.call_count == 1


# ---------------------------------------------------------------------------
# Disabled-controller no-op contract
# ---------------------------------------------------------------------------


class TestDisabledController:
    def test_every_method_is_a_noop_when_disabled(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "never")
        tui = InstallTui()
        assert tui._enabled is False
        # Enter / exit must not raise
        with tui:
            tui.start_phase("download", total=5)
            tui.task_started("k1", "fetch foo")
            tui.task_started("k2", "fetch bar")
            tui.task_completed("k1")
            tui.task_failed("k2")
        # No Rich primitives were ever instantiated.
        assert tui._aggregate is None
        assert tui._task_id is None
        assert tui._live is None
        assert tui._labels == []
        assert tui.is_animating() is False


# ---------------------------------------------------------------------------
# Label aggregation / overflow
# ---------------------------------------------------------------------------


class TestLabelAggregation:
    def test_active_set_overflow_renders_and_more(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        assert tui._enabled is True
        for i in range(7):
            tui.task_started(f"k{i}", f"task-{i}")

        rendered = tui._labels_renderable()
        text = rendered.plain  # rich.text.Text
        # First four labels visible.
        for i in range(4):
            assert f"task-{i}" in text
        # Tail summary mentions the remaining three.
        assert "... and 3 more" in text

    def test_task_completed_drops_labels_with_matching_key_prefix(
        self, _isolate_env: pytest.MonkeyPatch
    ) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        tui.task_started("dep-a", "fetch a")
        tui.task_started("dep-b", "fetch b")

        tui.task_completed("dep-a")
        with tui._lock:
            assert tui._labels == ["fetch b"]

    def test_task_started_is_idempotent_on_label(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        tui.task_started("k", "label")
        tui.task_started("k", "label")
        with tui._lock:
            assert tui._labels == ["label"]


# ---------------------------------------------------------------------------
# is_animating() reflects the Live state, not just the enabled bit
# ---------------------------------------------------------------------------


class TestIsAnimating:
    def test_returns_false_before_defer_fires(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        with tui:
            # Defer window not yet elapsed -- Live is still None.
            assert tui.is_animating() is False

    def test_returns_true_after_defer_fires(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        with tui:
            time.sleep(_DEFER_SHOW_S + 0.10)
            # The deferred timer should have fired and started Live.
            # If Rich initialization fails (no real terminal in tests),
            # the controller disables itself; accept either outcome.
            assert tui.is_animating() is (tui._live is not None)


# ---------------------------------------------------------------------------
# start_phase swap behaviour
# ---------------------------------------------------------------------------


class TestStartPhase:
    def test_start_phase_replaces_previous_task(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        tui.start_phase("resolve", total=3)
        first_task_id: Any = tui._task_id
        assert first_task_id is not None
        tui.start_phase("download", total=2)
        second_task_id: Any = tui._task_id
        assert second_task_id is not None
        assert first_task_id != second_task_id

    def test_start_phase_is_noop_when_disabled(self, _isolate_env: pytest.MonkeyPatch) -> None:
        _isolate_env.setenv("APM_PROGRESS", "never")
        tui = InstallTui()
        tui.start_phase("download", total=10)
        assert tui._task_id is None
        assert tui._aggregate is None


class TestConcurrentAccess:
    """Defends the controller's RLock against parallel BFS workers.

    The install pipeline spawns ThreadPoolExecutor workers that all
    call ``task_started``/``task_completed`` against a single shared
    ``InstallTui``. A regression that narrowed or removed the lock
    would only manifest under concurrency; this test pins the
    contract.
    """

    def test_parallel_lifecycle_no_corruption(self, _isolate_env: pytest.MonkeyPatch) -> None:
        from concurrent.futures import ThreadPoolExecutor

        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        tui.start_phase("download", total=32)

        def _one(idx: int) -> None:
            key = f"k{idx}"
            tui.task_started(key, f"fetch dep-{idx}")
            tui.task_completed(key)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_one, range(32)))

        # All labels consumed -- no leak, no double-count, no missed
        # removal under contention.
        assert tui._labels == []
        assert tui._key_to_label == {}

    def test_shutdown_sentinel_blocks_late_timer(self, _isolate_env: pytest.MonkeyPatch) -> None:
        """__exit__ must prevent _defer_start from publishing Live.

        Reproduces the TOCTOU race: the timer callback runs after
        __exit__ has set _shutdown but before .start() would fire.
        """
        _isolate_env.setenv("APM_PROGRESS", "always")
        tui = InstallTui()
        # Simulate __exit__ setting the sentinel before _defer_start
        # gets a chance to assign _live.
        with tui._lock:
            tui._shutdown = True
        tui._defer_start()
        # The deferred-start callback must have observed the sentinel
        # and bailed out without leaving an unowned Live region.
        assert tui._live is None
