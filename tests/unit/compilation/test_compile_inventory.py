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


def test_inventory_prunes_nested_git_repositories_but_exempts_its_root(tmp_path: Path) -> None:
    """Nested Git roots stay visible while their foreign contents are excluded."""
    _touch(tmp_path, "owned.py")
    _touch(tmp_path, "nested-gitdir/.git/config")
    _touch(tmp_path, "nested-gitdir/.apm/instructions/foreign.instructions.md")
    _touch(tmp_path, "nested-gitfile/.apm/instructions/foreign.instructions.md")
    (tmp_path / "nested-gitfile" / ".git").write_text(
        "gitdir: ../.git/worktrees/nested-gitfile\n",
        encoding="utf-8",
    )

    inventory = CompileInventory.collect(tmp_path)

    assert inventory.nested_repository_roots == frozenset(
        {tmp_path / "nested-gitdir", tmp_path / "nested-gitfile"}
    )
    assert inventory.files_within(tmp_path) == (tmp_path / "owned.py",)
    assert inventory.nested_repository_root_for(
        tmp_path / "nested-gitdir/.apm/instructions/foreign.instructions.md"
    ) == (tmp_path / "nested-gitdir")
    assert inventory.nested_repository_root_for(tmp_path / "owned.py") is None

    nested_inventory = CompileInventory.collect(tmp_path / "nested-gitdir")
    assert nested_inventory.nested_repository_roots == frozenset()
    assert nested_inventory.files_within(tmp_path / "nested-gitdir") == (
        tmp_path / "nested-gitdir/.apm/instructions/foreign.instructions.md",
    )
