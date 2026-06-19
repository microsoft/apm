"""Integration tests for skill_integrator.

Targets uncovered lines / branches in:
  src/apm_cli/integration/skill_integrator.py

Strategy: hermetic -- no network, no subprocess.  Only real filesystem I/O
(tmp_path) plus unittest.mock where needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.skill_integrator import (
    SkillIntegrator,
    copy_skill_to_target,
    validate_skill_name,
)
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_info(
    install_path: Path,
    name: str = "test-skill",
    pkg_type: PackageType | None = None,
) -> PackageInfo:
    pkg = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=pkg, install_path=install_path, package_type=pkg_type)


def _make_skill_source(tmp_path: Path, name: str = "my-skill") -> Path:
    """Create a minimal SKILL.md package directory."""
    src = tmp_path / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# validate_skill_name -- line 129 (fallback invalid name)
# ---------------------------------------------------------------------------


class TestValidateSkillNameFallback:
    def test_invalid_mixed_characters_returns_false(self):
        ok, msg = validate_skill_name("abc!def")
        assert not ok
        assert "invalid characters" in msg.lower() or msg

    def test_invalid_single_char_alphanumeric_is_valid(self):
        ok, _ = validate_skill_name("a")
        assert ok

    def test_consecutive_hyphens_rejected(self):
        ok, msg = validate_skill_name("abc--def")
        assert not ok
        assert "consecutive" in msg

    def test_leading_hyphen_rejected(self):
        ok, msg = validate_skill_name("-abc")
        assert not ok
        assert "start" in msg.lower()

    def test_trailing_hyphen_rejected(self):
        ok, msg = validate_skill_name("abc-")
        assert not ok
        assert "end" in msg.lower()

    def test_uppercase_rejected(self):
        ok, msg = validate_skill_name("MySkill")
        assert not ok
        assert "lowercase" in msg.lower()

    def test_underscore_rejected(self):
        ok, msg = validate_skill_name("my_skill")
        assert not ok
        assert "underscore" in msg.lower()

    def test_spaces_rejected(self):
        ok, msg = validate_skill_name("my skill")
        assert not ok
        assert "space" in msg.lower()

    def test_too_long_rejected(self):
        ok, msg = validate_skill_name("a" * 65)
        assert not ok
        assert "64" in msg

    def test_empty_rejected(self):
        ok, msg = validate_skill_name("")
        assert not ok
        assert "empty" in msg.lower()

    def test_valid_name_accepted(self):
        ok, msg = validate_skill_name("my-skill-123")
        assert ok
        assert msg == ""


# ---------------------------------------------------------------------------
# copy_skill_to_target -- symlink rejection (line 360), path outside project (line 370),
# dedup already deployed (lines 379-384)
# ---------------------------------------------------------------------------


class TestCopySkillToTarget:
    def test_symlink_skill_dir_raises(self, tmp_path):
        """Line 360: symlink at the destination is rejected."""
        src = _make_skill_source(tmp_path, "my-skill")
        pkg_info = _make_package_info(src, "my-skill", PackageType.CLAUDE_SKILL)
        target_base = tmp_path / "project"
        target_base.mkdir()

        # Create a fake target profile that supports skills
        skills_root = target_base / ".github" / "skills"
        skills_root.mkdir(parents=True)
        # Make the destination a symlink
        dest = skills_root / "my-skill"
        link_target = tmp_path / "somewhere_else"
        link_target.mkdir()
        dest.symlink_to(link_target)

        target = MagicMock()
        target.supports.return_value = True
        target.primitives = {"skills": MagicMock(deploy_root=None)}
        target.root_dir = Path(".github")
        target.auto_create = True

        with patch(
            "apm_cli.integration.skill_integrator.should_install_skill",
            return_value=True,
        ):
            from apm_cli.utils.path_security import PathTraversalError

            with pytest.raises(PathTraversalError, match=r"symlink"):
                copy_skill_to_target(
                    package_info=pkg_info,
                    source_path=src,
                    target_base=target_base,
                    targets=[target],
                )

    def test_target_not_support_skills_skipped(self, tmp_path):
        """Target that doesn't support skills is skipped -- empty result."""
        src = _make_skill_source(tmp_path, "my-skill")
        pkg_info = _make_package_info(src, "my-skill", PackageType.CLAUDE_SKILL)
        target_base = tmp_path / "project"
        target_base.mkdir()

        target = MagicMock()
        target.supports.return_value = False

        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=True):
            result = copy_skill_to_target(
                package_info=pkg_info,
                source_path=src,
                target_base=target_base,
                targets=[target],
            )
        assert result == []

    def test_auto_create_false_missing_dir_skipped(self, tmp_path):
        """auto_create=False and root dir missing -- target skipped."""
        src = _make_skill_source(tmp_path, "my-skill")
        pkg_info = _make_package_info(src, "my-skill", PackageType.CLAUDE_SKILL)
        target_base = tmp_path / "project"
        target_base.mkdir()

        target = MagicMock()
        target.supports.return_value = True
        target.primitives = {"skills": MagicMock(deploy_root=None)}
        target.root_dir = Path(".missing-dir")
        target.auto_create = False

        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=True):
            result = copy_skill_to_target(
                package_info=pkg_info,
                source_path=src,
                target_base=target_base,
                targets=[target],
            )
        assert result == []

    def test_dedup_same_resolved_path_only_deploys_once(self, tmp_path):
        """Lines 379-384: duplicate resolved paths are skipped after first deploy."""
        src = _make_skill_source(tmp_path, "my-skill")
        pkg_info = _make_package_info(src, "my-skill", PackageType.CLAUDE_SKILL)
        target_base = tmp_path / "project"
        target_base.mkdir()

        # Two targets that resolve to the same directory
        skills_root = target_base / ".github" / "skills"

        def _make_target():
            t = MagicMock()
            t.supports.return_value = True
            t.primitives = {"skills": MagicMock(deploy_root=None)}
            t.root_dir = Path(".github")
            t.auto_create = True
            return t

        t1 = _make_target()
        t2 = _make_target()

        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=True):
            deployed = copy_skill_to_target(
                package_info=pkg_info,
                source_path=src,
                target_base=target_base,
                targets=[t1, t2],
            )
        # Should be deployed exactly once despite two identical targets
        assert len(deployed) == 1
        assert deployed[0] == skills_root / "my-skill"


# ---------------------------------------------------------------------------
# SkillIntegrator._dircmp_equal -- subdirectory recursion (lines 526-527)
# ---------------------------------------------------------------------------


class TestDircmpEqual:
    def test_identical_dirs_returns_true(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "file.txt").write_text("hello")
        (b / "file.txt").write_text("hello")

        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is True

    def test_different_content_returns_false(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "file.txt").write_text("hello")
        (b / "file.txt").write_text("world")

        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is False

    def test_extra_file_returns_false(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "file.txt").write_text("hello")
        (b / "file.txt").write_text("hello")
        (b / "extra.txt").write_text("extra")

        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is False

    def test_nested_identical_dirs_returns_true(self, tmp_path):
        """Lines 525-527: recursive subdirectory comparison."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        (a / "sub").mkdir(parents=True)
        (b / "sub").mkdir(parents=True)
        (a / "sub" / "nested.md").write_text("same content")
        (b / "sub" / "nested.md").write_text("same content")

        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is True

    def test_nested_different_dirs_returns_false(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        (a / "sub").mkdir(parents=True)
        (b / "sub").mkdir(parents=True)
        (a / "sub" / "nested.md").write_text("version A")
        (b / "sub" / "nested.md").write_text("version B")

        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is False


# ---------------------------------------------------------------------------
# SkillIntegrator._promote_sub_skills -- various branches
# ---------------------------------------------------------------------------


class TestPromoteSubSkills:
    """Cover lines 569, 572, 578, 606-644 (user-authored skill handling)."""

    def _make_sub_skills_dir(self, tmp_path: Path, skill_names: list[str]) -> Path:
        sub = tmp_path / ".apm" / "skills"
        for name in skill_names:
            skill_dir = sub / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\n")
        return sub

    def test_non_directory_in_sub_skills_skipped(self, tmp_path):
        """Line 578: non-directory entries in sub_skills_dir are skipped."""
        sub = tmp_path / ".apm" / "skills"
        sub.mkdir(parents=True)
        (sub / "not-a-dir.txt").write_text("file")
        (sub / "real-skill").mkdir()
        (sub / "real-skill" / "SKILL.md").write_text("# Real\n")

        target_root = tmp_path / "target"
        target_root.mkdir()

        count, deployed = SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
        )
        assert count == 1
        assert len(deployed) == 1

    def test_entry_without_skill_md_skipped(self, tmp_path):
        """Directories without SKILL.md are skipped."""
        sub = tmp_path / ".apm" / "skills"
        sub.mkdir(parents=True)
        (sub / "no-skill-md").mkdir()
        (sub / "good-skill").mkdir()
        (sub / "good-skill" / "SKILL.md").write_text("# Good\n")

        target_root = tmp_path / "target"
        target_root.mkdir()

        count, _deployed = SkillIntegrator._promote_sub_skills(sub, target_root, "parent-pkg")
        assert count == 1

    def test_name_filter_excludes_non_matching(self, tmp_path):
        """name_filter set: only matching skills are promoted."""
        sub = self._make_sub_skills_dir(tmp_path, ["skill-a", "skill-b", "skill-c"])
        target_root = tmp_path / "target"
        target_root.mkdir()

        count, _deployed = SkillIntegrator._promote_sub_skills(
            sub, target_root, "parent-pkg", name_filter={"skill-a"}
        )
        assert count == 1
        assert (target_root / "skill-a").exists()
        assert not (target_root / "skill-b").exists()

    def test_content_identical_existing_skill_skipped_no_copy(self, tmp_path):
        """Lines 591-594: content-identical existing skill => increments count, no rmtree."""
        sub = self._make_sub_skills_dir(tmp_path, ["skill-a"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        # Pre-populate target with identical content
        target_skill = target_root / "skill-a"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# skill-a\n")
        mtime_before = target_skill.stat().st_mtime

        count, _deployed = SkillIntegrator._promote_sub_skills(sub, target_root, "parent-pkg")
        assert count == 1
        # Directory was NOT replaced (mtime unchanged)
        assert target_skill.stat().st_mtime == mtime_before

    def test_user_authored_skill_skipped_when_not_managed(self, tmp_path):
        """Lines 603-623: managed_files provided and skill NOT managed => skip (no force)."""
        sub = self._make_sub_skills_dir(tmp_path, ["user-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        # Pre-populate target with DIFFERENT content
        target_skill = target_root / "user-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# User authored\n")

        # managed_files set does NOT contain this skill path
        managed = set()

        count, _deployed = SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            managed_files=managed,
            force=False,
        )
        # Skill was skipped -- count stays 0
        assert count == 0

    def test_user_authored_skill_overwritten_with_force(self, tmp_path):
        """force=True overrides the user-authored guard."""
        sub = self._make_sub_skills_dir(tmp_path, ["user-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "user-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# User authored\n")

        managed: set[str] = set()

        count, _deployed = SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            managed_files=managed,
            force=True,
        )
        assert count == 1
        # Content should be replaced by the package version
        assert (target_root / "user-skill" / "SKILL.md").read_text() == "# user-skill\n"

    def test_diagnostics_skip_called_for_unmanaged_skill(self, tmp_path):
        """Line 607: diagnostics.skip() is called when skill is not managed."""
        sub = self._make_sub_skills_dir(tmp_path, ["blocked-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "blocked-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# different\n")

        diag = MagicMock()
        managed: set[str] = set()

        SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            diagnostics=diag,
            managed_files=managed,
            force=False,
        )
        diag.skip.assert_called_once()

    def test_logger_warning_for_unmanaged_skill(self, tmp_path):
        """Line 608-612: logger.warning() when no diagnostics and skill is not managed."""
        sub = self._make_sub_skills_dir(tmp_path, ["blocked-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "blocked-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# different\n")

        logger = MagicMock()
        managed: set[str] = set()

        SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            logger=logger,
            managed_files=managed,
            force=False,
        )
        logger.warning.assert_called()

    def test_warn_overwrite_calls_diagnostics(self, tmp_path):
        """Line 627: diagnostics.overwrite() for cross-package collision."""
        sub = self._make_sub_skills_dir(tmp_path, ["shared-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "shared-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# from other package\n")

        diag = MagicMock()
        # owned_by = None so managed_files check is skipped; warn=True
        SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            warn=True,
            diagnostics=diag,
        )
        diag.overwrite.assert_called()

    def test_warn_overwrite_calls_logger_warning(self, tmp_path):
        """Line 633: logger.warning() for cross-package collision when no diagnostics."""
        sub = self._make_sub_skills_dir(tmp_path, ["shared-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "shared-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# from other package\n")

        logger = MagicMock()
        SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            warn=True,
            logger=logger,
        )
        logger.warning.assert_called()

    def test_warn_overwrite_importerror_path(self, tmp_path):
        """Lines 643-644: ImportError path when no diagnostics and no logger."""
        sub = self._make_sub_skills_dir(tmp_path, ["shared-skill"])
        target_root = tmp_path / "target"
        target_root.mkdir()
        target_skill = target_root / "shared-skill"
        target_skill.mkdir()
        (target_skill / "SKILL.md").write_text("# from other package\n")

        with patch(
            "apm_cli.utils.console._rich_warning",
            side_effect=ImportError,
        ):
            # Should not raise -- ImportError is caught internally
            count, _ = SkillIntegrator._promote_sub_skills(
                sub,
                target_root,
                "parent-pkg",
                warn=True,
            )
        assert count == 1

    def test_project_root_provided_computes_rel_prefix(self, tmp_path):
        """Lines 567-572: project_root provided -- uses relative path prefix."""
        sub = self._make_sub_skills_dir(tmp_path, ["rel-skill"])
        target_root = tmp_path / ".github" / "skills"
        target_root.mkdir(parents=True)
        project_root = tmp_path

        count, _ = SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            project_root=project_root,
        )
        assert count == 1

    def test_project_root_outside_skills_root_uses_name_fallback(self, tmp_path):
        """Line 572: ValueError from relative_to => fallback to name."""
        sub = self._make_sub_skills_dir(tmp_path, ["fallback-skill"])
        # target_root lives OUTSIDE project_root so relative_to raises
        target_root = tmp_path / "outside" / "skills"
        target_root.mkdir(parents=True)
        project_root = tmp_path / "project"
        project_root.mkdir()

        count, _ = SkillIntegrator._promote_sub_skills(
            sub,
            target_root,
            "parent-pkg",
            project_root=project_root,
        )
        assert count == 1


# ---------------------------------------------------------------------------
# SkillIntegrator.integrate_skill -- name normalization warning paths
# ---------------------------------------------------------------------------


class TestIntegrateSkillNameNormalization:
    def _build_skill_pkg(self, tmp_path: Path, name: str) -> tuple[Path, PackageInfo]:
        """Build a minimal skill package with given directory name."""
        pkg_dir = tmp_path / "apm_modules" / "owner" / name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n# skill\n")
        info = _make_package_info(pkg_dir, name=name, pkg_type=PackageType.CLAUDE_SKILL)
        return pkg_dir, info

    def test_invalid_name_normalized_with_diagnostics(self, tmp_path):
        """Lines 839-843: invalid name triggers diagnostics.warn with normalized name."""
        _, info = self._build_skill_pkg(tmp_path, "MySkill_Package")
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        diag = MagicMock()

        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")
        fake_target.resolved_deploy_root = None
        fake_target.auto_create = True

        with (
            patch(
                "apm_cli.integration.skill_integrator.SkillIntegrator._build_ownership_maps",
                return_value=({}, {}),
            ),
            patch(
                "apm_cli.security.gate.ignore_non_content",
                return_value=[],
            ),
        ):
            result = integrator._integrate_native_skill(
                package_info=info,
                project_root=project_root,
                source_skill_md=info.install_path / "SKILL.md",
                diagnostics=diag,
                targets=[fake_target],
            )

        # Diagnostics.warn should have been called about the normalization
        diag.warn.assert_called()
        assert result.skill_created is True or result.skill_updated is True

    def test_invalid_name_normalized_with_logger(self, tmp_path):
        """Lines 844-847: invalid name triggers logger.warning."""
        _, info = self._build_skill_pkg(tmp_path, "BadName__Here")
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        logger = MagicMock()

        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")
        fake_target.resolved_deploy_root = None
        fake_target.auto_create = True

        with (
            patch(
                "apm_cli.integration.skill_integrator.SkillIntegrator._build_ownership_maps",
                return_value=({}, {}),
            ),
            patch(
                "apm_cli.security.gate.ignore_non_content",
                return_value=[],
            ),
        ):
            integrator._integrate_native_skill(
                package_info=info,
                project_root=project_root,
                source_skill_md=info.install_path / "SKILL.md",
                logger=logger,
                targets=[fake_target],
            )

        logger.warning.assert_called()


# ---------------------------------------------------------------------------
# SkillIntegrator.sync_integration -- managed_files code paths
# ---------------------------------------------------------------------------


class TestSyncIntegration:
    def _make_apm_package(self, name: str = "test-pkg") -> APMPackage:
        return APMPackage(name=name, version="1.0.0")

    def test_managed_files_removes_tracked_skill_dir(self, tmp_path):
        """Lines 1310-1379: manifest-based removal removes tracked skill dir."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        skills_dir = project_root / ".github" / "skills"
        skills_dir.mkdir(parents=True)
        orphan = skills_dir / "orphan-skill"
        orphan.mkdir()

        integrator = SkillIntegrator()
        apm_pkg = self._make_apm_package()
        managed = {".github/skills/orphan-skill"}

        # We need a target that maps to .github/skills prefix
        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.user_root_resolver = None
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")

        stats = integrator.sync_integration(
            apm_pkg,
            project_root,
            managed_files=managed,
            targets=[fake_target],
        )
        assert stats["files_removed"] >= 1
        assert not orphan.exists()

    def test_managed_files_dotdot_path_skipped(self, tmp_path):
        """Line 1323: paths with '..' are silently skipped."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        apm_pkg = self._make_apm_package()
        managed = {".github/skills/../../../etc/passwd"}

        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.user_root_resolver = None
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")

        stats = integrator.sync_integration(
            apm_pkg,
            project_root,
            managed_files=managed,
            targets=[fake_target],
        )
        # Nothing removed -- the traversal path was skipped
        assert stats["files_removed"] == 0

    def test_managed_files_outside_project_root_skipped(self, tmp_path):
        """Path that resolves outside project root is silently skipped."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        apm_pkg = self._make_apm_package()
        # This path is valid-looking but resolves outside
        managed = {".github/skills/../../../outside"}

        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.user_root_resolver = None
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")

        stats = integrator.sync_integration(
            apm_pkg,
            project_root,
            managed_files=managed,
            targets=[fake_target],
        )
        assert stats["files_removed"] == 0

    def test_legacy_orphan_detection_removes_unknown_skill(self, tmp_path):
        """Lines 1383-1438: npm-style fallback removes skills not in installed set."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        skills_dir = project_root / ".github" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "orphaned").mkdir()

        apm_pkg = self._make_apm_package()
        # No apm deps -- installed set is empty
        apm_pkg.dependencies = {}

        integrator = SkillIntegrator()

        fake_target = MagicMock()
        fake_target.supports.return_value = True
        fake_target.user_root_resolver = None
        fake_target.primitives = {"skills": MagicMock(deploy_root=None)}
        fake_target.root_dir = Path(".github")

        stats = integrator.sync_integration(
            apm_pkg,
            project_root,
            managed_files=None,
            targets=[fake_target],
        )
        # Orphaned skill should be removed
        assert stats["files_removed"] >= 1

    def test_legacy_target_without_skills_support_skipped(self, tmp_path):
        """Target without skills support is skipped in legacy path."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        apm_pkg = self._make_apm_package()
        apm_pkg.dependencies = {}

        integrator = SkillIntegrator()

        fake_target = MagicMock()
        fake_target.supports.return_value = False

        stats = integrator.sync_integration(
            apm_pkg,
            project_root,
            managed_files=None,
            targets=[fake_target],
        )
        assert stats == {"files_removed": 0, "errors": 0}


# ---------------------------------------------------------------------------
# SkillIntegrator._clean_orphaned_skills
# ---------------------------------------------------------------------------


class TestCleanOrphanedSkills:
    def test_removes_skill_not_in_installed_set(self, tmp_path):
        """Orphaned skill directory is removed."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        orphan = skills_dir / "old-skill"
        orphan.mkdir()

        integrator = SkillIntegrator()
        result = integrator._clean_orphaned_skills(skills_dir, {"new-skill"})
        assert result["files_removed"] == 1
        assert not orphan.exists()

    def test_keeps_skill_in_installed_set(self, tmp_path):
        """Installed skill directory is preserved."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        keeper = skills_dir / "installed-skill"
        keeper.mkdir()

        integrator = SkillIntegrator()
        result = integrator._clean_orphaned_skills(skills_dir, {"installed-skill"})
        assert result["files_removed"] == 0
        assert keeper.exists()

    def test_agents_dir_only_removes_lockfile_owned_skills(self, tmp_path):
        """Lines 1464-1484: .agents/skills only removes APM-owned skills."""
        agents_skills_dir = tmp_path / ".agents" / "skills"
        agents_skills_dir.mkdir(parents=True)
        foreign = agents_skills_dir / "foreign-tool-skill"
        foreign.mkdir()
        apm_owned = agents_skills_dir / "apm-skill"
        apm_owned.mkdir()

        integrator = SkillIntegrator()

        # Patch lockfile ownership: only apm-skill is owned
        with patch.object(
            SkillIntegrator,
            "_get_lockfile_owned_agent_skills",
            return_value={"apm-skill"},
        ):
            result = integrator._clean_orphaned_skills(
                agents_skills_dir,
                installed_skill_names=set(),
                project_root=tmp_path,
            )

        # Only apm-skill is removed; foreign-tool-skill is kept
        assert result["files_removed"] == 1
        assert foreign.exists()
        assert not apm_owned.exists()

    def test_error_during_rmtree_counted(self, tmp_path):
        """Exception during rmtree is counted as error."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        orphan = skills_dir / "bad-skill"
        orphan.mkdir()

        integrator = SkillIntegrator()
        with patch("shutil.rmtree", side_effect=PermissionError("denied")):
            result = integrator._clean_orphaned_skills(skills_dir, set())

        assert result["errors"] == 1


# ---------------------------------------------------------------------------
# SkillIntegrator._get_lockfile_owned_agent_skills
# ---------------------------------------------------------------------------


class TestGetLockfileOwnedAgentSkills:
    def test_returns_skill_names_from_agents_paths(self, tmp_path):
        """Lines 1495-1506: parses .agents/skills/ paths from lockfile."""
        from unittest.mock import MagicMock

        mock_dep = MagicMock()
        mock_dep.deployed_files = [
            ".agents/skills/skill-one/SKILL.md",
            ".agents/skills/skill-two/",
            ".github/skills/other",
        ]
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"pkg": mock_dep}

        with (
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=mock_lockfile,
            ),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
        ):
            result = SkillIntegrator._get_lockfile_owned_agent_skills(tmp_path)

        assert "skill-one" in result
        assert "skill-two" in result
        assert "other" not in result

    def test_returns_empty_on_missing_lockfile(self, tmp_path):
        """FileNotFoundError yields empty set."""
        with (
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                side_effect=FileNotFoundError,
            ),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
        ):
            result = SkillIntegrator._get_lockfile_owned_agent_skills(tmp_path)

        assert result == set()

    def test_returns_empty_when_lockfile_is_none(self, tmp_path):
        """LockFile.read returning None yields empty set."""
        with (
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
        ):
            result = SkillIntegrator._get_lockfile_owned_agent_skills(tmp_path)

        assert result == set()
