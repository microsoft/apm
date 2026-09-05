"""Tests for the canonical .apmignore owner."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.apmignore import ApmIgnoreError, ApmIgnoreSpec, clear_apmignore_cache

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def _clear_ignore_cache() -> None:
    clear_apmignore_cache()
    yield
    clear_apmignore_cache()


def _write(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _spec(root: Path, body: str, *, nested: dict[str, str] | None = None) -> ApmIgnoreSpec:
    _write(root, ".apmignore", body)
    for rel, nested_body in (nested or {}).items():
        _write(root, rel, nested_body)
    return ApmIgnoreSpec.load(root)


def test_no_ignore_file_keeps_everything(tmp_path: Path) -> None:
    skill = _write(tmp_path, "SKILL.md", "# skill")
    evals = _write(tmp_path, "evals/foo.md")
    spec = ApmIgnoreSpec.load(tmp_path)
    assert spec.is_ignored(skill) is False
    assert spec.is_ignored(evals) is False


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    kept = _write(tmp_path, "keep.md")
    spec = _spec(tmp_path, "# just a comment\n\n")
    assert spec.is_ignored(kept) is False


def test_star_and_double_star(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    log = _write(tmp_path, "notes.log")
    nested_log = _write(tmp_path, "refs/debug.log")
    md = _write(tmp_path, "refs/guide.md")
    spec = _spec(tmp_path, "*.log\n")
    assert spec.is_ignored(log) is True
    assert spec.is_ignored(nested_log) is True
    assert spec.is_ignored(md) is False


def test_directory_only_slash(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    evals_dir = tmp_path / "evals"
    evals_dir.mkdir()
    evals_file = _write(tmp_path, "evals/case.md")
    evals_name = _write(tmp_path, "evals.txt")
    spec = _spec(tmp_path, "evals/\n")
    assert spec.is_ignored(evals_dir, is_dir=True) is True
    assert spec.is_ignored(evals_file) is True
    assert spec.is_ignored(evals_name) is False


def test_last_match_wins_negation(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    dropped = _write(tmp_path, "foo.log")
    kept = _write(tmp_path, "keep.log")
    spec = _spec(tmp_path, "*.log\n!keep.log\n")
    assert spec.is_ignored(dropped) is True
    assert spec.is_ignored(kept) is False


def test_parent_dir_blocks_child_negation(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    readme = _write(tmp_path, "evals/README.md")
    spec = _spec(tmp_path, "evals/\n!evals/README.md\n")
    assert spec.is_ignored(readme) is True


def test_unignore_file_when_directory_itself_is_not_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    readme = _write(tmp_path, "evals/README.md")
    other = _write(tmp_path, "evals/secret.md")
    spec = _spec(tmp_path, "evals/*\n!evals/README.md\n")
    assert spec.is_ignored(readme) is False
    assert spec.is_ignored(other) is True


def test_nested_apmignore_last_match_wins(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    local = _write(tmp_path, "skills/foo/tmp/note.md")
    spec = _spec(
        tmp_path,
        "tmp/\n",
        nested={"skills/foo/.apmignore": "!tmp/\n!tmp/note.md\n"},
    )
    assert spec.is_ignored(local) is False


def test_unreadable_apmignore_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    ignore_path = _write(tmp_path, ".apmignore", "evals/\n")
    original = Path.read_text

    def _blocked(self, *args, **kwargs):
        if self.name == ".apmignore":
            raise OSError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _blocked)
    with pytest.raises(ApmIgnoreError, match="Cannot read"):
        ApmIgnoreSpec.load(tmp_path)
    assert ignore_path.exists()


def test_cannot_ignore_skill_md(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    _write(tmp_path, ".apmignore", "SKILL.md\n")
    with pytest.raises(ApmIgnoreError, match=r"SKILL\.md"):
        ApmIgnoreSpec.load(tmp_path)


def test_cannot_ignore_apm_yml(tmp_path: Path) -> None:
    _write(tmp_path, "apm.yml", "name: demo\nversion: 0.0.1\n")
    _write(tmp_path, ".apmignore", "apm.yml\n")
    with pytest.raises(ApmIgnoreError, match=r"apm\.yml"):
        ApmIgnoreSpec.load(tmp_path)


def test_path_outside_package_is_not_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "SKILL.md", "# skill")
    spec = _spec(tmp_path, "secret.md\n")
    outside = tmp_path.parent / "other.txt"
    outside.write_text("x", encoding="utf-8")
    assert spec.is_ignored(outside) is False


def test_copytree_drops_evals_and_keeps_skill(tmp_path: Path) -> None:
    src = tmp_path / "pkg"
    src.mkdir()
    _write(src, "SKILL.md", "# skill")
    _write(src, "references/guide.md")
    _write(src, "evals/case.md")
    spec = _spec(src, "evals/\n")
    dest = tmp_path / "dest"
    shutil.copytree(src, dest, ignore=spec.copytree_ignore())
    copied = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert copied == [".apmignore", "SKILL.md", "references/guide.md"]


def test_discovery_skips_ignored_instruction(tmp_path: Path) -> None:
    from apm_cli.primitives.discovery import find_primitive_files

    _write(tmp_path, "SKILL.md", "# skill")
    kept = _write(
        tmp_path,
        ".apm/instructions/keep.instructions.md",
        "---\napplyTo: '**'\n---\nkeep\n",
    )
    _write(
        tmp_path,
        "evals/hidden.instructions.md",
        "---\napplyTo: '**'\n---\nhidden\n",
    )
    _spec(tmp_path, "evals/\n")
    found = find_primitive_files(str(tmp_path), ["**/*.instructions.md"])
    assert kept.resolve() in {path.resolve() for path in found}
    assert all(path.name != "hidden.instructions.md" for path in found)


def test_pack_recursive_collect_skips_evals(tmp_path: Path) -> None:
    from apm_cli.bundle.plugin_exporter import _collect_recursive

    _write(tmp_path, "SKILL.md", "# skill")
    _write(tmp_path, ".apm/skills/demo/SKILL.md", "---\nname: demo\n---\n")
    _write(tmp_path, ".apm/skills/demo/evals/case.md")
    spec = _spec(tmp_path, "evals/\n")
    out: list[tuple[Path, str]] = []
    _collect_recursive(tmp_path / ".apm" / "skills", "skills", out, ignore=spec)
    rels = sorted(rel for _src, rel in out)
    assert rels == ["skills/demo/SKILL.md"]
