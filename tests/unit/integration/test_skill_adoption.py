"""Unit contracts for receipt-less skill tree adoption (#3391)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.integration.skill_adoption import adoptable_skill_members, adoption_baselines
from apm_cli.utils.content_hash import compute_file_hash


def _walk(source: Path):
    yield from os.walk(source, followlinks=False)


def _no_ignore(_directory: str, _contents: list[str]) -> list[str]:
    return []


def _no_links(content: str, *_args, **_kwargs) -> tuple[str, int]:
    return content, 0


def _rewrite_links(content: str, *_args, **_kwargs) -> tuple[str, int]:
    rewritten = content.replace("](sibling.md)", "](rendered/sibling.md)")
    return rewritten, int(rewritten != content)


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: s\n---\nSee [x](sibling.md).\n", encoding="ascii")
    (source / "references/guide.md").write_text("Guide.\n", encoding="ascii")
    destination = tmp_path / "deployed/s"
    shutil.copytree(source, destination)
    return source, destination


def _members(source: Path, destination: Path, **overrides) -> frozenset[Path]:
    options = {"walk_source": _walk, "ignore": _no_ignore, "resolve_links": _no_links}
    options.update(overrides)
    return adoptable_skill_members(source, destination, **options)


def test_identical_tree_is_adoptable(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    assert _members(source, destination) == {
        destination / "SKILL.md",
        destination / "references/guide.md",
    }


@pytest.mark.parametrize("change", ["extra", "missing", "edited"])
def test_any_tree_difference_blocks_adoption(tmp_path: Path, change: str) -> None:
    source, destination = _tree(tmp_path)
    if change == "extra":
        (destination / "retired.md").write_text("Old.\n", encoding="ascii")
    elif change == "missing":
        (destination / "references/guide.md").unlink()
    else:
        (destination / "references/guide.md").write_text("Edited.\n", encoding="ascii")
    assert _members(source, destination) == frozenset()


def test_ignored_source_members_are_not_expected(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    (source / "apm.yml").write_text("name: s\n", encoding="ascii")

    def ignore(_directory: str, contents: list[str]) -> list[str]:
        return [name for name in contents if name == "apm.yml"]

    assert destination / "SKILL.md" in _members(source, destination, ignore=ignore)


@pytest.mark.windows_compat
def test_comparison_uses_rendered_markdown(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    assert _members(source, destination, resolve_links=_rewrite_links) == frozenset()
    # Rendered members are deployed LF-only (write_text_lf); write_text would emit CRLF on Windows.
    (destination / "SKILL.md").write_bytes(b"---\nname: s\n---\nSee [x](rendered/sibling.md).\n")
    assert destination / "SKILL.md" in _members(source, destination, resolve_links=_rewrite_links)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_destination_symlink_blocks_adoption(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    (destination / "references/guide.md").unlink()
    (destination / "references/guide.md").symlink_to(source / "references/guide.md")
    assert _members(source, destination) == frozenset()


def test_missing_destination_is_not_adoptable(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    shutil.rmtree(destination)
    assert _members(source, destination) == frozenset()


def test_baselines_require_every_member_to_be_claimed(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    options = {"walk_source": _walk, "ignore": _no_ignore, "resolve_links": _no_links}
    anchor = destination / "SKILL.md"
    assert (
        adoption_baselines(source, destination, owned=lambda path: path == anchor, **options) == {}
    )
    baselines = adoption_baselines(source, destination, owned=lambda _path: True, **options)
    assert baselines == {path: compute_file_hash(path) for path in _members(source, destination)}
