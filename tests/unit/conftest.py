"""Unit-test conftest: hermetic HOME isolation.

The session-scoped autouse fixture below pins ``Path.home()`` to a tmp
directory for the entire unit-test session. Two reasons:

1. Hermeticity. Unit tests must not read or write the contributor's real
   ``~`` (config files, runtimes, caches). Tests that call
   ``RuntimeManager()`` or ``Path.home()`` directly previously inherited
   whatever HOME the runner had.
2. Windows runner robustness. On the ``windows-2025-vs2026`` GitHub
   Actions image the ``USERPROFILE`` / ``HOMEDRIVE`` + ``HOMEPATH``
   triplet is not set under the pytest worker subprocess, so
   ``Path.home()`` raises ``RuntimeError: Could not determine home
   directory.`` This fixture sets the platform-correct trio so
   ``Path.home()`` always resolves to ``<tmp>`` regardless of runner
   image.

Per-test fixtures that need a different HOME (e.g. tests/unit/integration
that exercise scope resolution) keep using ``monkeypatch.setenv`` and
override this baseline; the function-scoped monkeypatch wins over the
session-scoped baseline for the duration of the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _set_home_env(home: Path) -> None:
    """Set ``HOME`` and the Windows-equivalent vars to ``home``.

    ``Path.home()`` consults ``HOME`` on POSIX but ``USERPROFILE``
    (with ``HOMEDRIVE`` + ``HOMEPATH`` fallback) on Windows.
    """
    home_str = str(home)
    os.environ["HOME"] = home_str
    if os.name == "nt":
        os.environ["USERPROFILE"] = home_str
        drive, _, tail = home_str.partition(":")
        if tail:
            os.environ["HOMEDRIVE"] = f"{drive}:"
            os.environ["HOMEPATH"] = tail


@pytest.fixture(scope="session", autouse=True)
def _hermetic_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Pin ``Path.home()`` to a per-session tmp dir for all unit tests."""
    home = tmp_path_factory.mktemp("apm-unit-home")
    previous = {
        key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
    }
    _set_home_env(home)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
