"""Unit tests for ignore_non_content copytree callback.

Verifies that skill deployment does not copy the .apm-pin cache marker
from apm_modules/ into deploy targets (e.g., .agents/skills/).

Regression test for https://github.com/microsoft/apm/issues/1150.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.security.gate import ignore_non_content


class TestIgnoreNonContent:
    def test_filters_apm_pin_at_root(self, tmp_path: Path):
        """The .apm-pin marker at the skill root is excluded from copy."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        (src / ".apm-pin").write_text(
            '{"schema_version": 1, "resolved_commit": "abc123"}',
            encoding="utf-8",
        )

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_non_content)

        copied_names = sorted(p.name for p in dest.iterdir())
        assert copied_names == ["SKILL.md"]

    def test_filters_symlinks(self, tmp_path: Path):
        """Symlinks are still excluded (preserves original security behavior)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        target = tmp_path / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        try:
            (src / "evil-link").symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_non_content)

        copied_names = sorted(p.name for p in dest.iterdir())
        assert copied_names == ["SKILL.md"]

    def test_preserves_normal_files_and_subdirectories(self, tmp_path: Path):
        """Regular files and directories pass through unfiltered."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        sub = src / "examples"
        sub.mkdir()
        (sub / "example.md").write_text("example", encoding="utf-8")

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_non_content)

        assert (dest / "SKILL.md").exists()
        assert (dest / "examples" / "example.md").exists()
        root_names = sorted(p.name for p in dest.iterdir())
        assert root_names == ["SKILL.md", "examples"]

    def test_filters_pin_in_nested_directory(self, tmp_path: Path):
        """The filter applies at every directory depth, not just root."""
        src = tmp_path / "src"
        sub = src / "nested"
        sub.mkdir(parents=True)
        (src / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        (sub / "data.md").write_text("data", encoding="utf-8")
        (sub / ".apm-pin").write_text(
            '{"schema_version": 1, "resolved_commit": "def456"}',
            encoding="utf-8",
        )

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_non_content)

        assert (dest / "SKILL.md").exists()
        assert (dest / "nested" / "data.md").exists()
        nested_names = sorted(p.name for p in (dest / "nested").iterdir())
        assert nested_names == ["data.md"]

    def test_simulates_skill_deploy_from_apm_modules(self, tmp_path: Path):
        """End-to-end: cache dir with .apm-pin copies cleanly to deploy target.

        Simulates the exact scenario from issue #1150: apm_modules
        contains a skill directory with SKILL.md + .apm-pin, and the
        skill integrator copies it into .agents/skills/<name>/.
        """
        apm_modules = tmp_path / "apm_modules" / "owner" / "repo" / ".apm" / "skills" / "my-skill"
        apm_modules.mkdir(parents=True)
        (apm_modules / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n# My Skill", encoding="utf-8"
        )
        (apm_modules / ".apm-pin").write_text(
            '{"schema_version": 1, "resolved_commit": "3745092c"}',
            encoding="utf-8",
        )

        deploy_target = tmp_path / ".agents" / "skills" / "my-skill"
        shutil.copytree(apm_modules, deploy_target, ignore=ignore_non_content)

        deployed_names = sorted(p.name for p in deploy_target.iterdir())
        assert deployed_names == ["SKILL.md"]

    def test_does_not_filter_files_with_similar_names(self, tmp_path: Path):
        """Only the exact name .apm-pin is filtered, not similar names."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        (src / ".apm-pin").write_text("marker", encoding="utf-8")
        (src / ".apm-pin.bak").write_text("backup", encoding="utf-8")
        (src / "apm-pin").write_text("no dot prefix", encoding="utf-8")
        (src / ".apm-pins").write_text("plural", encoding="utf-8")

        dest = tmp_path / "dest"
        shutil.copytree(src, dest, ignore=ignore_non_content)

        copied_names = sorted(p.name for p in dest.iterdir())
        assert copied_names == [".apm-pin.bak", ".apm-pins", "SKILL.md", "apm-pin"]
