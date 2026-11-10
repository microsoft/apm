"""#3441: only a truly empty skill destination tree is treated as absent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.integration.skill_support import skill_dir_has_content


def test_missing_path_has_no_content(tmp_path: Path) -> None:
    assert skill_dir_has_content(tmp_path / "absent") is False


def test_empty_and_nested_empty_trees_have_no_content(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    nested = tmp_path / "nested"
    (nested / "a" / "b").mkdir(parents=True)
    (nested / "c").mkdir()
    assert skill_dir_has_content(empty) is False
    assert skill_dir_has_content(nested) is False


def test_any_file_anywhere_is_content(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / ".keep").write_bytes(b"")
    assert skill_dir_has_content(root) is True


def test_regular_file_destination_is_content(tmp_path: Path) -> None:
    target = tmp_path / "skill"
    target.write_text("not a directory", encoding="utf-8")
    assert skill_dir_has_content(target) is True


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinks_count_as_content(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dir_link = tmp_path / "dir-link"
    (dir_link / "sub").mkdir(parents=True)
    (dir_link / "sub" / "link").symlink_to(elsewhere, target_is_directory=True)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "dangling").symlink_to(tmp_path / "missing")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(elsewhere, target_is_directory=True)
    assert skill_dir_has_content(dir_link) is True
    assert skill_dir_has_content(broken) is True
    assert skill_dir_has_content(root_link) is True


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission semantics required",
)
def test_unreadable_subtree_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    locked = root / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert skill_dir_has_content(root) is True
    finally:
        locked.chmod(0o700)
