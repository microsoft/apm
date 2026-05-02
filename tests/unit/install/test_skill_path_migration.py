"""Unit tests for skill_path_migration module.

Tests the three core functions:
- detect_legacy_skill_deployments: lockfile scanning
- check_collisions: collision detection
- execute_migration: deletion + lockfile update
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apm_cli.install.skill_path_migration import (
    _LEGACY_SKILL_PATTERN,
    MigrationPlan,
    MigrationResult,
    check_collisions,
    detect_legacy_skill_deployments,
    execute_migration,
)

# ---------------------------------------------------------------------------
# Lightweight stubs -- avoid importing the real lockfile module
# ---------------------------------------------------------------------------


@dataclass
class _StubDep:
    deployed_files: list[str] = field(default_factory=list)
    deployed_file_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class _StubLockFile:
    dependencies: dict[str, _StubDep] = field(default_factory=dict)
    local_deployed_files: list[str] = field(default_factory=list)
    local_deployed_file_hashes: dict[str, str] = field(default_factory=dict)

    def get_dependency(self, key: str) -> _StubDep | None:
        return self.dependencies.get(key)


# ===================================================================
# _LEGACY_SKILL_PATTERN regex tests
# ===================================================================


class TestLegacySkillPattern:
    """Verify the regex matches only the expected per-client prefixes."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/skills/my-skill/SKILL.md",
            ".cursor/skills/review/SKILL.md",
            ".opencode/skills/deep/nested/file.md",
            ".gemini/skills/lint/SKILL.md",
        ],
    )
    def test_matches_legacy_clients(self, path: str) -> None:
        assert _LEGACY_SKILL_PATTERN.match(path)

    @pytest.mark.parametrize(
        "path",
        [
            ".agents/skills/my-skill/SKILL.md",  # converged, not legacy
            ".claude/skills/my-skill/SKILL.md",  # Claude excluded
            ".codex/skills/my-skill/SKILL.md",  # Codex never legacy
            ".github/instructions/foo.md",  # not skills
            ".cursor/agents/something.md",  # not skills
            "skills/my-skill/SKILL.md",  # no dot-prefix
        ],
    )
    def test_rejects_non_legacy(self, path: str) -> None:
        assert not _LEGACY_SKILL_PATTERN.match(path)

    def test_captures_client_and_skill_name(self) -> None:
        m = _LEGACY_SKILL_PATTERN.match(".github/skills/my-skill/SKILL.md")
        assert m is not None
        assert m.group(1) == "github"
        assert m.group(2) == "my-skill"


# ===================================================================
# detect_legacy_skill_deployments
# ===================================================================


class TestDetectLegacySkillDeployments:
    """Tests for lockfile scanning."""

    def test_empty_lockfile(self, tmp_path: Path) -> None:
        lf = _StubLockFile()
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert plans == []

    def test_no_legacy_paths(self, tmp_path: Path) -> None:
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(deployed_files=[".agents/skills/foo/SKILL.md"]),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert plans == []

    def test_detects_github_legacy(self, tmp_path: Path) -> None:
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[
                        ".github/skills/my-skill/SKILL.md",
                        ".agents/skills/my-skill/SKILL.md",  # new path -- ignored
                    ]
                ),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert len(plans) == 1
        assert plans[0].src_path == ".github/skills/my-skill/SKILL.md"
        assert plans[0].dst_path == ".agents/skills/my-skill/SKILL.md"
        assert plans[0].dep_name == "pkg-a"

    def test_detects_multiple_clients(self, tmp_path: Path) -> None:
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[
                        ".github/skills/s1/SKILL.md",
                        ".cursor/skills/s1/SKILL.md",
                        ".opencode/skills/s1/SKILL.md",
                        ".gemini/skills/s1/SKILL.md",
                    ]
                ),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert len(plans) == 4
        for plan in plans:
            assert plan.dst_path == ".agents/skills/s1/SKILL.md"

    def test_ignores_claude_and_codex(self, tmp_path: Path) -> None:
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[
                        ".claude/skills/s1/SKILL.md",
                        ".codex/skills/s1/SKILL.md",
                    ]
                ),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert plans == []

    def test_multiple_deps(self, tmp_path: Path) -> None:
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(deployed_files=[".github/skills/s1/SKILL.md"]),
                "pkg-b": _StubDep(deployed_files=[".cursor/skills/s2/SKILL.md"]),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        assert len(plans) == 2
        dep_names = {p.dep_name for p in plans}
        assert dep_names == {"pkg-a", "pkg-b"}


# ===================================================================
# check_collisions
# ===================================================================


class TestCheckCollisions:
    """Tests for collision detection."""

    def test_no_collision_when_dst_missing(self, tmp_path: Path) -> None:
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]
        # Source exists, destination does not.
        src = tmp_path / ".github/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("content")
        assert check_collisions(plans, tmp_path) == []

    def test_no_collision_when_identical_content(self, tmp_path: Path) -> None:
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]
        src = tmp_path / ".github/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("same content")
        dst = tmp_path / ".agents/skills/s1/SKILL.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("same content")
        assert check_collisions(plans, tmp_path) == []

    def test_collision_when_different_content(self, tmp_path: Path) -> None:
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]
        src = tmp_path / ".github/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("old content")
        dst = tmp_path / ".agents/skills/s1/SKILL.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("new content")
        collisions = check_collisions(plans, tmp_path)
        assert len(collisions) == 1
        assert "pkg-a" in collisions[0]

    def test_no_collision_when_src_missing(self, tmp_path: Path) -> None:
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]
        # Only destination exists.
        dst = tmp_path / ".agents/skills/s1/SKILL.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("content")
        assert check_collisions(plans, tmp_path) == []


# ===================================================================
# execute_migration
# ===================================================================


class TestExecuteMigration:
    """Tests for the migration executor."""

    def test_deletes_legacy_file(self, tmp_path: Path) -> None:
        """Deletes old file and updates lockfile deployed_files."""
        src = tmp_path / ".github/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("content")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[".github/skills/s1/SKILL.md"],
                    deployed_file_hashes={".github/skills/s1/SKILL.md": "sha256:abc"},
                ),
            }
        )
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]

        result = execute_migration(plans, lf, tmp_path)

        assert not src.exists()
        assert ".github/skills/s1/SKILL.md" not in lf.dependencies["pkg-a"].deployed_files
        assert ".agents/skills/s1/SKILL.md" in lf.dependencies["pkg-a"].deployed_files
        assert result.deleted == [".github/skills/s1/SKILL.md"]
        assert "pkg-a" in result.updated_deps

    def test_migrates_hash_entry(self, tmp_path: Path) -> None:
        """Content hash is migrated from old key to new key."""
        src = tmp_path / ".cursor/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("content")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[".cursor/skills/s1/SKILL.md"],
                    deployed_file_hashes={".cursor/skills/s1/SKILL.md": "sha256:def"},
                ),
            }
        )
        plans = [
            MigrationPlan(
                src_path=".cursor/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]

        execute_migration(plans, lf, tmp_path)

        hashes = lf.dependencies["pkg-a"].deployed_file_hashes
        assert ".cursor/skills/s1/SKILL.md" not in hashes
        assert hashes[".agents/skills/s1/SKILL.md"] == "sha256:def"

    def test_skips_missing_file(self, tmp_path: Path) -> None:
        """If legacy file is already gone, still update lockfile."""
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[".github/skills/s1/SKILL.md"],
                ),
            }
        )
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]

        result = execute_migration(plans, lf, tmp_path)

        assert result.skipped_no_file == [".github/skills/s1/SKILL.md"]
        assert result.deleted == []
        assert ".agents/skills/s1/SKILL.md" in lf.dependencies["pkg-a"].deployed_files

    def test_cleans_empty_parent_dirs(self, tmp_path: Path) -> None:
        """After deleting the file, empty parent dirs are removed."""
        src = tmp_path / ".opencode/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("content")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(deployed_files=[".opencode/skills/s1/SKILL.md"]),
            }
        )
        plans = [
            MigrationPlan(
                src_path=".opencode/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]

        execute_migration(plans, lf, tmp_path)

        assert not (tmp_path / ".opencode/skills/s1").exists()
        assert not (tmp_path / ".opencode/skills").exists()
        # .opencode/ dir itself may or may not exist depending on other content

    def test_idempotent_dst_already_in_deployed_files(self, tmp_path: Path) -> None:
        """If dst_path is already in deployed_files, don't duplicate it."""
        src = tmp_path / ".github/skills/s1/SKILL.md"
        src.parent.mkdir(parents=True)
        src.write_text("content")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[
                        ".github/skills/s1/SKILL.md",
                        ".agents/skills/s1/SKILL.md",  # Already present from integrate phase
                    ]
                ),
            }
        )
        plans = [
            MigrationPlan(
                src_path=".github/skills/s1/SKILL.md",
                dst_path=".agents/skills/s1/SKILL.md",
                dep_name="pkg-a",
            ),
        ]

        result = execute_migration(plans, lf, tmp_path)

        files = lf.dependencies["pkg-a"].deployed_files
        assert files.count(".agents/skills/s1/SKILL.md") == 1
        assert ".github/skills/s1/SKILL.md" not in files
        assert len(result.deleted) == 1

    def test_multiple_clients_same_dep(self, tmp_path: Path) -> None:
        """Multiple legacy paths for the same dep are all migrated."""
        for client in ("github", "cursor", "opencode", "gemini"):
            p = tmp_path / f".{client}/skills/s1/SKILL.md"
            p.parent.mkdir(parents=True)
            p.write_text("content")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[
                        ".github/skills/s1/SKILL.md",
                        ".cursor/skills/s1/SKILL.md",
                        ".opencode/skills/s1/SKILL.md",
                        ".gemini/skills/s1/SKILL.md",
                    ]
                ),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        result = execute_migration(plans, lf, tmp_path)

        assert len(result.deleted) == 4
        files = lf.dependencies["pkg-a"].deployed_files
        assert ".agents/skills/s1/SKILL.md" in files
        # All legacy paths removed:
        for client in ("github", "cursor", "opencode", "gemini"):
            assert f".{client}/skills/s1/SKILL.md" not in files

    def test_no_plans_noop(self, tmp_path: Path) -> None:
        """execute_migration with empty plans returns empty result."""
        lf = _StubLockFile()
        result = execute_migration([], lf, tmp_path)
        assert result == MigrationResult()


# ===================================================================
# Path-traversal security tests (H9)
# ===================================================================


class TestPathTraversalRejection:
    """Ensure path-traversal attempts are rejected at plan creation and execution."""

    def test_detect_rejects_path_traversal_in_lockfile_entry(self, tmp_path: Path) -> None:
        """Lockfile entry with .. segments must NOT appear in the plan."""
        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(
                    deployed_files=[".cursor/skills/x/../../../etc/passwd"],
                ),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        # The traversal entry should have been rejected:
        assert len(plans) == 0

    def test_execute_rejects_path_traversal_at_unlink(self, tmp_path: Path) -> None:
        """A MigrationPlan whose src_path resolves outside project_root must not delete."""

        # Create a file outside the project root to serve as the "escape" target.
        outer = tmp_path / "outer" / "victim.txt"
        outer.parent.mkdir(parents=True)
        outer.write_text("should survive")

        project = tmp_path / "project"
        project.mkdir()
        # Construct a plan that would resolve outside project_root.
        evil_plan = MigrationPlan(
            src_path="../outer/victim.txt",
            dst_path=".agents/skills/evil/SKILL.md",
            dep_name="pkg-evil",
        )
        lf = _StubLockFile(
            dependencies={
                "pkg-evil": _StubDep(deployed_files=["../outer/victim.txt"]),
            }
        )

        # ensure_path_within should prevent deletion; the file is outside project.
        result = execute_migration([evil_plan], lf, project)
        assert outer.exists(), "File outside project_root must NOT be deleted"
        assert result.deleted == []
        assert result.failed == ["../outer/victim.txt"]

    @pytest.mark.parametrize(
        "path",
        [
            ".github/skills.bak/foo/SKILL.md",
            "./.github/skills/foo/SKILL.md",
            ".github/skills/foo/",
        ],
    )
    def test_regex_near_miss_rejects(self, path: str) -> None:
        """Paths that look similar but shouldn't match the legacy pattern."""
        assert not _LEGACY_SKILL_PATTERN.match(path)


# ===================================================================
# Mixed-content parent dir preservation (H10)
# ===================================================================


class TestMixedContentParentDir:
    """Ensure non-empty parent dirs are preserved after migration."""

    def test_preserves_nonempty_parent_dir(self, tmp_path: Path) -> None:
        """Delete .cursor/skills/s1/ but keep .cursor/rules/foo.md and .cursor/ itself."""
        # Legacy skill:
        skill = tmp_path / ".cursor/skills/s1/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("content")

        # Foreign file under .cursor/:
        foreign = tmp_path / ".cursor/rules/foo.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("keep me")

        lf = _StubLockFile(
            dependencies={
                "pkg-a": _StubDep(deployed_files=[".cursor/skills/s1/SKILL.md"]),
            }
        )
        plans = detect_legacy_skill_deployments(lf, tmp_path)
        result = execute_migration(plans, lf, tmp_path)

        assert len(result.deleted) == 1
        # Skill dir should be cleaned up:
        assert not (tmp_path / ".cursor/skills/s1").exists()
        assert not (tmp_path / ".cursor/skills").exists()
        # Foreign file and .cursor/ must survive:
        assert foreign.exists()
        assert (tmp_path / ".cursor").exists()
