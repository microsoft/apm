"""Contracts keeping the public CLI registry aligned with rendered docs (pure logic cases)."""

import pytest

from scripts.check_cli_docs import DEFAULT_DIST, recovery_guidance

pytestmark = pytest.mark.unit


def test_default_dist_recovery_guidance_uses_root_safe_build_command() -> None:
    """The repository output keeps an executable build-and-rerun sequence."""
    assert recovery_guidance(DEFAULT_DIST, mismatch=True) == (
        "[i] Add or remove the matching CLI reference page, "
        "rebuild with 'npm --prefix docs run build', then rerun "
        f"'uv run --frozen python scripts/check_cli_docs.py {DEFAULT_DIST}'."
    )
