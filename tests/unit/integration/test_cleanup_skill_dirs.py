"""Unit tests for skill directory cleanup in ``remove_stale_deployed_files``.

Tests the deferred skill directory removal feature (issue #1483): when a
package is removed and its deployed files include a skill directory entry,
APM should safely remove that directory after deleting individual files
instead of emitting a "Refused to remove directory entry" warning.
"""

from pathlib import Path

import pytest

from apm_cli.integration.cleanup import (
    _is_skill_directory_entry,
    remove_stale_deployed_files,
)
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


@pytest.fixture
def diagnostics():
    return DiagnosticCollector(verbose=False)


def _make_file(root: Path, rel: str, content: str = "hello\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ------------------------------------------------------------------
# _is_skill_directory_entry
# ------------------------------------------------------------------


class TestIsSkillDirectoryEntry:
    def test_standard_agents_skill(self):
        assert _is_skill_directory_entry(".agents/skills/my-skill")

    def test_github_skills(self):
        assert _is_skill_directory_entry(".github/skills/coding-lint")

    def test_claude_skills(self):
        assert _is_skill_directory_entry(".claude/skills/my-tool")

    def test_cursor_skills(self):
        assert _is_skill_directory_entry(".cursor/skills/helper")

    def test_too_short_rejected(self):
        assert not _is_skill_directory_entry("skills/name")

    def test_skills_root_rejected(self):
        assert not _is_skill_directory_entry(".agents/skills")

    def test_subdir_within_skill_rejected(self):
        assert not _is_skill_directory_entry(".agents/skills/my-skill/scripts")

    def test_non_skill_directory(self):
        assert not _is_skill_directory_entry(".github/instructions")

    def test_prompts_dir_rejected(self):
        assert not _is_skill_directory_entry(".github/prompts")

    def test_no_skills_component(self):
        assert not _is_skill_directory_entry(".github/agents/my-agent")


# ------------------------------------------------------------------
# Skill directory removal in remove_stale_deployed_files
# ------------------------------------------------------------------


class TestSkillDirectoryCleanup:
    def test_empty_skill_dir_removed_after_files_deleted(self, project_root, diagnostics):
        """Skill dir is empty after individual files are deleted -> rmdir."""
        skill_md = _make_file(project_root, ".agents/skills/my-skill/SKILL.md", "# My Skill\n")
        # Skill dir entry + file entry, both stale
        stale = [
            ".agents/skills/my-skill",
            ".agents/skills/my-skill/SKILL.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        assert ".agents/skills/my-skill/SKILL.md" in result.deleted
        assert ".agents/skills/my-skill" in result.deleted
        assert not skill_md.exists()
        assert not (project_root / ".agents/skills/my-skill").exists()
        # No "Refused" warnings
        msgs = [d.message for d in diagnostics._diagnostics]
        assert not any("Refused to remove directory entry" in m for m in msgs)

    def test_skill_dir_with_assets_removed_when_hashes_match(self, project_root, diagnostics):
        """Skill dir with remaining APM-tracked files is rmtree'd.

        The asset file is NOT in the stale list, so it remains on disk
        after the first pass. The second pass checks its hash and removes
        the whole directory tree via rmtree.
        """
        skill_md = _make_file(project_root, ".agents/skills/my-skill/SKILL.md", "# Skill\n")
        asset = _make_file(project_root, ".agents/skills/my-skill/assets/data.json", '{"a":1}\n')
        # Record hashes for both files
        recorded_hashes = {
            ".agents/skills/my-skill/SKILL.md": compute_file_hash(skill_md),
            ".agents/skills/my-skill/assets/data.json": compute_file_hash(asset),
        }
        # Only the dir entry and SKILL.md are stale -- asset is NOT in the
        # stale list, so it remains after the first pass and the second pass
        # must exercise the rmtree + hash verification path.
        stale = [
            ".agents/skills/my-skill",
            ".agents/skills/my-skill/SKILL.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            recorded_hashes=recorded_hashes,
        )
        assert ".agents/skills/my-skill" in result.deleted
        assert not (project_root / ".agents/skills/my-skill").exists()

    def test_skill_dir_not_removed_when_user_file_present(self, project_root, diagnostics):
        """Skill dir is NOT removed when it contains user-created files."""
        _make_file(project_root, ".agents/skills/my-skill/SKILL.md", "# Skill\n")
        # User adds their own file in the skill directory
        _make_file(project_root, ".agents/skills/my-skill/my-notes.txt", "user notes\n")
        stale = [
            ".agents/skills/my-skill",
            ".agents/skills/my-skill/SKILL.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        # SKILL.md is deleted (it is a file entry)
        assert ".agents/skills/my-skill/SKILL.md" in result.deleted
        # But directory is skipped because of user file
        assert ".agents/skills/my-skill" in result.skipped_unmanaged
        # User file intact
        assert (project_root / ".agents/skills/my-skill/my-notes.txt").exists()
        msgs = [d.message for d in diagnostics._diagnostics]
        assert any("not owned by APM" in m for m in msgs)
        # Diagnostic should list the blocking file path
        assert any("my-notes.txt" in m for m in msgs)

    def test_skill_dir_not_removed_when_hash_mismatch(self, project_root, diagnostics):
        """Skill dir is NOT removed when a tracked file was user-edited."""
        _make_file(project_root, ".agents/skills/my-skill/SKILL.md", "# Skill\n")
        asset = _make_file(
            project_root,
            ".agents/skills/my-skill/references/guide.md",
            "edited content\n",
        )
        # Recorded hash does NOT match current content
        recorded_hashes = {
            ".agents/skills/my-skill/references/guide.md": "sha256:" + "0" * 64,
        }
        stale = [
            ".agents/skills/my-skill",
            ".agents/skills/my-skill/SKILL.md",
            ".agents/skills/my-skill/references/guide.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            recorded_hashes=recorded_hashes,
        )
        # SKILL.md deleted (no hash recorded -> falls through)
        assert ".agents/skills/my-skill/SKILL.md" in result.deleted
        # guide.md skipped (hash mismatch -> user edit)
        assert ".agents/skills/my-skill/references/guide.md" in result.skipped_user_edit
        # Directory skipped because guide.md is still there with mismatch
        assert ".agents/skills/my-skill" in result.skipped_unmanaged
        assert asset.exists()

    def test_non_skill_dir_still_rejected(self, project_root, diagnostics):
        """Non-skill directory entries still get the old rejection."""
        (project_root / ".github" / "prompts").mkdir(parents=True)
        _make_file(project_root, ".github/prompts/user.prompt.md", "user content\n")
        result = remove_stale_deployed_files(
            [".github/prompts"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        assert result.deleted == []
        assert ".github/prompts" in result.skipped_unmanaged
        msgs = [d.message for d in diagnostics._diagnostics]
        assert any("Refused to remove directory entry" in m for m in msgs)

    def test_skill_dir_already_gone(self, project_root, diagnostics):
        """Skill dir entry for a non-existent directory is a no-op."""
        result = remove_stale_deployed_files(
            [".agents/skills/gone-skill"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        # Not an error, not a warning -- just silently clean
        assert result.deleted == []
        assert result.failed == []
        assert result.skipped_unmanaged == []

    def test_backward_compat_no_hashes_empty_dir_removed(self, project_root, diagnostics):
        """Legacy lockfile without hashes: skill dir removed when empty."""
        _make_file(project_root, ".agents/skills/old-skill/SKILL.md", "# Old\n")
        stale = [
            ".agents/skills/old-skill",
            ".agents/skills/old-skill/SKILL.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            recorded_hashes=None,
        )
        assert ".agents/skills/old-skill/SKILL.md" in result.deleted
        assert ".agents/skills/old-skill" in result.deleted
        assert not (project_root / ".agents/skills/old-skill").exists()

    def test_skill_dir_with_subdirs_removed_when_all_tracked(self, project_root, diagnostics):
        """Skill bundle with scripts/ and assets/ subdirs, all tracked."""
        skill_md = _make_file(project_root, ".agents/skills/full-skill/SKILL.md", "# Full\n")
        script = _make_file(project_root, ".agents/skills/full-skill/scripts/run.sh", "#!/bin/sh\n")
        asset = _make_file(project_root, ".agents/skills/full-skill/assets/img.png", "PNG\n")
        recorded_hashes = {
            ".agents/skills/full-skill/SKILL.md": compute_file_hash(skill_md),
            ".agents/skills/full-skill/scripts/run.sh": compute_file_hash(script),
            ".agents/skills/full-skill/assets/img.png": compute_file_hash(asset),
        }
        stale = [
            ".agents/skills/full-skill",
            ".agents/skills/full-skill/SKILL.md",
            ".agents/skills/full-skill/scripts/run.sh",
            ".agents/skills/full-skill/assets/img.png",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            recorded_hashes=recorded_hashes,
        )
        assert ".agents/skills/full-skill" in result.deleted
        assert not (project_root / ".agents/skills/full-skill").exists()

    def test_path_traversal_in_skill_dir_still_rejected(self, project_root, diagnostics):
        """Path traversal in a skill directory name is caught by Gate 1."""
        result = remove_stale_deployed_files(
            [".agents/skills/../../../etc"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        assert result.deleted == []
        assert result.skipped_unmanaged == [".agents/skills/../../../etc"]

    def test_symlink_inside_skill_dir_blocks_removal(self, project_root, diagnostics):
        """Symlinks inside a skill directory are treated as user content."""
        _make_file(project_root, ".agents/skills/my-skill/SKILL.md", "# Skill\n")
        # Create a symlink inside the skill directory
        link = project_root / ".agents/skills/my-skill/link.txt"
        target = project_root / "some-other-file.txt"
        target.write_text("target content\n", encoding="utf-8")
        link.symlink_to(target)
        stale = [
            ".agents/skills/my-skill",
            ".agents/skills/my-skill/SKILL.md",
        ]
        result = remove_stale_deployed_files(
            stale,
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )
        assert ".agents/skills/my-skill/SKILL.md" in result.deleted
        assert ".agents/skills/my-skill" in result.skipped_unmanaged
        # Symlink and its target remain
        assert link.is_symlink()
        assert target.exists()
