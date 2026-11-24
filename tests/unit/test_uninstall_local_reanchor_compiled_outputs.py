"""The moving-copy check refuses a compiled-output walk it cannot complete (#5097)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.commands.uninstall.local_reanchor import _compiled_output_naming

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3", "P-INV-I1")]


def test_a_directory_the_walk_cannot_list_refuses_instead_of_being_skipped(
    tmp_path: Path,
) -> None:
    """os.walk would skip it silently; the check names it so the copy stays."""
    missing = tmp_path / "gone"

    assert _compiled_output_naming(missing, lambda _path: False) is not None


def test_a_walk_with_no_compiled_output_naming_the_copy_lets_it_move(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# not compiled\n", encoding="utf-8")

    assert _compiled_output_naming(tmp_path, lambda _path: True) is None
