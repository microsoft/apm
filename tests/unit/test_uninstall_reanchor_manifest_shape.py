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


class _FakeSourceFacts:
    """Injected source facts: the volume folds NFC and NFD to one directory (#5222)."""

    def __init__(self, inodes: dict[str, int], linked: frozenset[str] = frozenset()) -> None:
        self.inodes, self.linked = inodes, linked

    def identity(self, path: Path) -> tuple[int, int] | None:
        inode = self.inodes.get(path.name)
        return None if inode is None else (1, inode)

    def contained(self, root: Path, path: Path) -> bool:
        return path.name not in self.linked and path.is_relative_to(root)


_NFC, _NFD = "caf\u00e9", "cafe\u0301"


@pytest.mark.parametrize(
    ("inodes", "linked", "outside", "same"),
    [
        ({_NFC: 7, _NFD: 7}, frozenset(), False, True),
        ({_NFC: 7, _NFD: 8}, frozenset(), False, False),
        ({_NFC: 7, _NFD: 7}, frozenset({_NFD}), False, False),
        ({_NFC: 7, _NFD: 7}, frozenset(), True, False),
        ({_NFC: 7}, frozenset(), False, False),
    ],
    ids=["one-source-directory", "two-directories", "linked", "outside", "missing"],
)
def test_a_normalization_respelling_is_one_source_directory_only_when_proven(
    tmp_path: Path, inodes, linked, outside: bool, same: bool
) -> None:
    """NFC and NFD spellings name one package only when both SOURCE directories are in
    the project, reached without a link, and one inode (#5222); install paths never decide."""
    from types import SimpleNamespace

    from apm_cli.commands.uninstall.local_reanchor import _same_install_dir

    root = tmp_path / "project"
    parent = (tmp_path / "elsewhere") if outside else (root / "vendor")
    row = SimpleNamespace(repo_url=f"_local/{_NFC}")
    source = (parent / _NFC, parent / _NFD, root)

    verdict = _same_install_dir(
        row,
        f"_local/{_NFD}",
        root / "apm_modules",
        source=source,
        facts=_FakeSourceFacts(inodes, linked),
    )

    assert verdict is same
