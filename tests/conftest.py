# Root conftest.py — shared pytest configuration
#
# Test directory structure:
#   tests/unit/          — Fast isolated unit tests (default CI scope)
#   tests/integration/   — E2E tests requiring network / external services
#   tests/acceptance/    — Acceptance criteria tests
#   tests/benchmarks/    — Performance benchmarks (excluded by default)
#   tests/test_*.py      — Root-level tests (mixed unit/integration)
#
# Quick reference:
#   uv run pytest tests/unit tests/test_console.py -x   # CI-equivalent fast run
#   uv run pytest                                         # Full suite
#   uv run pytest -m benchmark                            # Benchmarks only

import pytest


@pytest.fixture(autouse=True, scope="session")
def _validate_primitive_coverage():
    """Fail fast if KNOWN_TARGETS has primitives without dispatch handlers."""
    from apm_cli.integration.coverage import check_primitive_coverage
    from apm_cli.integration.dispatch import get_dispatch_table

    dispatch = get_dispatch_table()
    check_primitive_coverage(dispatch)
