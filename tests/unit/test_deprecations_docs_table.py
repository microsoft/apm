"""The CLI reference deprecations table is generated from the catalog (#4019)."""

from __future__ import annotations

import pytest

from scripts.render_deprecations_table import DOC_PAGE, render_tables, rendered_page

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics", "P-DOC-cli-surface"),
]


def test_cli_reference_deprecations_table_matches_the_catalog() -> None:
    page = DOC_PAGE.read_text(encoding="utf-8")

    assert page == rendered_page(page), (
        "Run 'uv run python scripts/render_deprecations_table.py --write'."
    )


def test_generated_table_shows_unscheduled_horizons() -> None:
    table = render_tables()

    assert "| not scheduled |" in table
    assert table.isascii()
