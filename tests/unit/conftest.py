"""Unit-test conftest: hermetic HOME isolation.

This module sets ``HOME`` (POSIX) / ``USERPROFILE`` + ``HOMEDRIVE`` +
``HOMEPATH`` (Windows) to a process-wide tmp directory **at import
time**, before any fixture or test resolution. Two reasons:

1. Hermeticity. Unit tests must not read or write the contributor's real
   ``~`` (config files, runtimes, caches). Tests that call
   ``RuntimeManager()`` or ``Path.home()`` directly previously inherited
   whatever HOME the runner had.
2. Windows runner robustness. On the ``windows-2025-vs2026`` GitHub
   Actions image the ``USERPROFILE`` / ``HOMEDRIVE`` + ``HOMEPATH``
   triplet is not set under the pytest-xdist worker subprocess, so
   ``Path.home()`` raises ``RuntimeError: Could not determine home
   directory.``

Why import-time and not a session-scoped autouse fixture: a previous
attempt used ``@pytest.fixture(scope="session", autouse=True)`` and
still failed on a single xdist worker (``gw2`` on Windows). Whatever
the cause (worker scheduling order, a downstream fixture resolving
``Path.home()`` before the autouse fixture's setup completed), running
the env mutation at conftest import time guarantees the env vars are
in place before pytest collects, schedules, or runs anything in this
worker process. Pytest imports each test directory's conftest.py once
per worker, before any fixtures run.

Per-test fixtures that need a different HOME (e.g. tests/unit/integration
that exercise scope resolution) keep using ``monkeypatch.setenv`` and
override this baseline; monkeypatch's snapshot/restore cycle preserves
this baseline across tests.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _set_home_env(home: Path) -> None:
    """Set ``HOME`` and the Windows-equivalent vars to ``home``.

    ``Path.home()`` consults ``HOME`` on POSIX but ``USERPROFILE``
    (with ``HOMEDRIVE`` + ``HOMEPATH`` fallback) on Windows. We
    overwrite unconditionally because the Windows runner sometimes
    leaves these keys present but empty, which still trips
    ``Path.home()``.
    """
    home_str = str(home)
    os.environ["HOME"] = home_str
    if os.name == "nt":
        os.environ["USERPROFILE"] = home_str
        drive, _, tail = home_str.partition(":")
        if tail:
            os.environ["HOMEDRIVE"] = f"{drive}:"
            os.environ["HOMEPATH"] = tail


_TMP_HOME = Path(tempfile.mkdtemp(prefix="apm-unit-home-"))
_set_home_env(_TMP_HOME)
