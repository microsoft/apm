# Root conftest.py -- shared pytest configuration
#
# Test directory structure:
#   tests/unit/          -- Fast isolated unit tests (default CI scope)
#   tests/integration/   -- E2E tests requiring network / external services
#   tests/acceptance/    -- Acceptance criteria tests
#   tests/benchmarks/    -- Performance benchmarks (excluded by default)
#   tests/test_*.py      -- Root-level tests (mixed unit/integration)
#
# Quick reference:
#   uv run pytest tests/unit tests/test_console.py -x   # CI-equivalent fast run
#   uv run pytest                                         # Full suite
#   uv run pytest -m benchmark                            # Benchmarks only
#
# Path.home() override (top of file, runs at conftest import time on every
# pytest-xdist worker): the windows-2025-vs2026 GitHub-hosted runner does not
# seed USERPROFILE / HOMEDRIVE / HOMEPATH for pytest-xdist worker subprocesses,
# so Path.home() raises RuntimeError. Earlier attempts patched only the env
# vars in tests/unit/conftest.py, but at least one xdist worker (gw2) still
# evaluated Path.home() before that conftest's import-time mutation took
# effect. Override Path.home() directly here -- the root conftest is loaded
# by every worker before any test in any test directory runs, so this is
# the earliest hook we have without writing a pytest plugin.

import os
import tempfile
from pathlib import Path

import pytest

_TMP_HOME = Path(tempfile.mkdtemp(prefix="apm-test-home-"))


def _ensure_home_env(home: Path) -> None:
    home_str = str(home)
    os.environ["HOME"] = home_str
    if os.name == "nt":
        os.environ["USERPROFILE"] = home_str
        drive, _, tail = home_str.partition(":")
        if tail:
            os.environ["HOMEDRIVE"] = f"{drive}:"
            os.environ["HOMEPATH"] = tail


_ensure_home_env(_TMP_HOME)


def _hermetic_home(_cls=Path) -> Path:
    """Resolve a home dir without ever raising.

    Honors HOME / USERPROFILE / HOMEDRIVE+HOMEPATH so per-test
    `monkeypatch.setenv("HOME", ...)` (or its Windows-trio equivalent)
    keeps working. Falls back to a hermetic tmp dir only when the env
    is empty -- which is the windows-2025-vs2026 xdist worker case.
    """
    home = os.environ.get("HOME")
    if not home and os.name == "nt":
        home = os.environ.get("USERPROFILE")
        if not home:
            drive = os.environ.get("HOMEDRIVE", "")
            tail = os.environ.get("HOMEPATH", "")
            if tail:
                home = drive + tail
    return Path(home) if home else _TMP_HOME


# Override Path.home() so any code path -- production or test -- that calls
# it during the test run gets the hermetic tmp dir, regardless of whether
# the runner subprocess inherited a usable HOME / USERPROFILE.
Path.home = classmethod(_hermetic_home)  # type: ignore[method-assign]


@pytest.fixture(autouse=True, scope="session")
def _validate_primitive_coverage():
    """Fail fast if KNOWN_TARGETS has primitives without dispatch handlers."""
    from apm_cli.integration.coverage import check_primitive_coverage
    from apm_cli.integration.dispatch import get_dispatch_table

    dispatch = get_dispatch_table()
    check_primitive_coverage(dispatch)
