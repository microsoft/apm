"""Unit tests for ignore_symlinks_and_pin copytree callback.

Verifies that skill deployment does not copy the .apm-pin cache marker
from apm_modules/ into deploy targets (e.g., .agents/skills/).

Regression test for https://github.com/microsoft/apm/issues/1150.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.security.gate import ignore_symlinks_and_pin


class TestIgnoreSymlinksAndPin:
    def test_filters_apm_pin_file(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill")
        (src / ".apm-pin").write_text('{"schema_version": 1, "resolved_commit": "abc123"}')

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_symlinks_and_pin)

        assert (dest / "SKILL.md").exists()
        assert not (dest / ".apm-pin").exists()

    def test_filters_symlinks(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill")
        target = tmp_path / "secret.txt"
        target.write_text("secret")
        (src / "evil-link").symlink_to(target)

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_symlinks_and_pin)

        assert (dest / "SKILL.md").exists()
        assert not (dest / "evil-link").exists()

    def test_preserves_normal_files_and_dirs(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill")
        sub = src / "examples"
        sub.mkdir()
        (sub / "example.md").write_text("example")

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_symlinks_and_pin)

        assert (dest / "SKILL.md").exists()
        assert (dest / "examples" / "example.md").exists()

    def test_filters_pin_in_subdirectory(self, tmp_path: Path):
        src = tmp_path / "src"
        sub = src / "nested"
        sub.mkdir(parents=True)
        (src / "SKILL.md").write_text("# My Skill")
        (sub / "data.md").write_text("data")
        (sub / ".apm-pin").write_text('{"schema_version": 1, "resolved_commit": "def456"}')

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_symlinks_and_pin)

        assert (dest / "SKILL.md").exists()
        assert (dest / "nested" / "data.md").exists()
        assert not (dest / "nested" / ".apm-pin").exists()
