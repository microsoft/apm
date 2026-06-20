"""Local conftest for install.sh safety tests.

The root tests/conftest.py autouses fixtures that import apm_cli
(primitive-coverage check, discovery cache reset). Those are valid for
the Python CLI test suite but irrelevant to a shell-script regression
test -- and forcing apm_cli on PATH here would couple the safety tests
to a Python dependency chain they don't exercise.

We override the two autouse fixtures with no-ops so this test file can
run in any environment that has bash + pytest, no Python build needed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _validate_primitive_coverage():
    """No-op: shell-script safety tests don't exercise the dispatch table."""


@pytest.fixture(autouse=True)
def _isolate_discovery_state():
    """No-op: shell-script safety tests don't touch discovery state."""
    yield
