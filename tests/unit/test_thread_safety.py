"""Thread-safety tests for console singleton and marketplace registry lock."""

import threading
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.utils.console import _get_console, _reset_console

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Console singleton tests
# ---------------------------------------------------------------------------


class TestConsoleSingleton:
    """Verify _get_console() returns a thread-safe singleton."""

    def setup_method(self):
        _reset_console()

    def teardown_method(self):
        _reset_console()

    def test_console_singleton_returns_same_instance(self):
        """Two sequential calls return the exact same object."""
        first = _get_console()
        second = _get_console()
        assert first is not None
        assert first is second

    def test_console_singleton_thread_safe(self):
        """10 threads all receive the same Console instance."""
        results: list = [None] * 10
        barrier = threading.Barrier(10)

        def _worker(idx: int) -> None:
            barrier.wait()
            results[idx] = _get_console()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results must be the same object
        assert all(r is not None for r in results)
        assert all(r is results[0] for r in results)

    def test_console_reset_clears_singleton(self):
        """_reset_console() forces a fresh instance on next call."""
        first = _get_console()
        assert first is not None

        _reset_console()

        second = _get_console()
        assert second is not None
        assert second is not first


# ---------------------------------------------------------------------------
# Marketplace registry lock tests
# ---------------------------------------------------------------------------
