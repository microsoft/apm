"""Opt-in, periodic process stacks for the Windows release unit run."""

from __future__ import annotations

import faulthandler
import math
import os
import sys
from threading import Event, Thread

import pytest

_WATCHDOG = pytest.StashKey[tuple[Event, Thread, int]]()


def _dump_stacks(stop: Event, interval: float, fd: int) -> None:
    """Inspect stacks with the GIL held, without inspecting frame locals."""
    while not stop.wait(interval):
        faulthandler.dump_traceback(file=fd, all_threads=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Allow short intervals in the subprocess contract, not test timeouts."""
    parser.addini("hang_dump_interval", "Seconds between process stack dumps", default="300")


def pytest_configure(config: pytest.Config) -> None:
    """Start one opt-in reporter in the controller and each xdist worker."""
    if config.pluginmanager.hasplugin("faulthandler"):
        raise pytest.UsageError("hang diagnostics requires -p no:faulthandler")
    interval = float(config.getini("hang_dump_interval"))
    if not math.isfinite(interval) or interval <= 0:
        raise pytest.UsageError("hang_dump_interval must be positive and finite")
    # A dedicated descriptor survives pytest's capture and test stderr patches.
    fd = os.dup(sys.__stderr__.fileno())
    stop = Event()
    thread = Thread(target=_dump_stacks, args=(stop, interval, fd), daemon=True)
    config.stash[_WATCHDOG] = (stop, thread, fd)
    faulthandler.enable(file=fd)
    # pytest's built-in per-test timer is canceled by a failed call, leaving
    # subsequent fixture teardown unobserved. This session reporter is independent.
    # A Python thread holds the GIL during stack inspection; native GIL-holding
    # deadlocks cannot be diagnosed here and rely on the workflow's timeout.
    thread.start()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stop the watchdog before closing its descriptor."""
    if _WATCHDOG in config.stash:
        stop, thread, fd = config.stash[_WATCHDOG]
        stop.set()
        thread.join(timeout=1)
        if thread.is_alive():
            # The daemon may still be writing: leave its fd owned by the process,
            # not available for reuse, and fail rather than strand pytest exit.
            raise RuntimeError("Hang diagnostics reporter did not stop within 1 second")
        faulthandler.disable()
        os.close(fd)
        del config.stash[_WATCHDOG]
