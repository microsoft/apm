"""The offline re-anchor reads apm.yml's replace block; a non-mapping one refuses (#5226)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.commands.uninstall.local_reanchor import _manifest_declares_replacements

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3", "P-INV-I1")]


@pytest.mark.parametrize(
    ("text", "declared"),
    [("- a\n- b\n", True), ("just a string\n", True), ("name: proj\n", False)],
    ids=["list", "scalar", "mapping"],
)
def test_a_non_mapping_manifest_counts_as_declaring_replacements(
    tmp_path: Path, text: str, declared: bool
) -> None:
    """An apm.yml that is not a mapping refuses the offline re-anchor like an
    unreadable one; a mapping without a replace block behaves as today (#5226)."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text(text, encoding="utf-8")

    assert _manifest_declares_replacements(manifest) is declared
    assert manifest.read_text(encoding="utf-8") == text
