"""Component coverage for the shared compile inventory."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.compilation.inventory import CompileInventory

pytestmark = pytest.mark.component


def _touch(base: Path, relative_path: str) -> None:
    """Create one fixture file."""
    path = base / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")


@pytest.mark.windows_compat
def test_inventory_is_sorted_exclusion_aware_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    """The snapshot keeps portable relative paths without following directory links."""
    _touch(tmp_path, "src/z.py")
    _touch(tmp_path, "src/a.py")
    _touch(tmp_path, "vendor/ignored.py")
    _touch(tmp_path, ".git/config")
    _touch(tmp_path, "node_modules/package/index.js")
    _touch(tmp_path, "__pycache__/inventory.pyc")
    _touch(tmp_path, ".pytest_cache/metadata")
    (tmp_path / "linked").symlink_to(tmp_path / "src", target_is_directory=True)

    inventory = CompileInventory.collect(tmp_path, exclude_patterns=["vendor"])

    assert [entry.relative_path.as_posix() for entry in inventory.directories] == [".", "src"]
    assert inventory.directories[1].file_names == ("a.py", "z.py")
    assert inventory.files_under(frozenset({"src"})) == (
        tmp_path / "src/a.py",
        tmp_path / "src/z.py",
    )
    assert inventory.files_within(tmp_path / "src") == (
        tmp_path / "src/a.py",
        tmp_path / "src/z.py",
    )
    assert not inventory.contains_directory(tmp_path / "vendor")
    assert not inventory.contains_directory(tmp_path / ".git")
    assert not inventory.contains_directory(tmp_path / "node_modules")
    assert not inventory.contains_directory(tmp_path / "__pycache__")
    assert not inventory.contains_directory(tmp_path / ".pytest_cache")
    assert not inventory.contains_directory(tmp_path / "linked")
