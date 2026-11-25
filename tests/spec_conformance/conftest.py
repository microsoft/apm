"""pytest configuration for the spec-conformance suite.

Requirement markers (``@pytest.mark.req``) are validated and mapped suite-wide
by ``tests/spec_conformance/coverage_map.py``, wired from ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._manifest import REPO_ROOT


@pytest.fixture(scope="session")
def fixture_dir():
    from tests.spec_conformance._manifest import FIXTURE_ROOT

    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


@pytest.fixture(autouse=True)
def _isolate_console_stream():
    """An in-process ``--format json`` CLI call routes the console to stderr.

    That routing is process-global, so reset it after every test; otherwise a
    later test reading ``capsys.readouterr().out`` depends on shard order.
    """
    yield
    from apm_cli.utils.console import _reset_console

    _reset_console()
