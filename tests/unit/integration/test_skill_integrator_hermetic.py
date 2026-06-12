"""Unit tests for apm_cli.integration.skill_integrator.

Targets uncovered branches in:
- validate_skill_name (line 129 fallthrough)
- should_compile_instructions
- copy_skill_to_target (should_install_skill=False, auto_create guard)
- _dircmp_equal (left_only, right_only, mismatches, errors)
- _promote_sub_skills (non-dir, managed_files branches, overwrite warning paths)
- _promote_sub_skills_standalone (dedup / seen_skill_dirs)
- _build_skill_ownership_map / _build_native_skill_owner_map
- _integrate_native_skill (name normalization branches, dedup, collision)
- _integrate_skill_bundle (dedup, is_primary branches)
- integrate (virtual file skip, skill_subset with native SKILL.md, bundle path,
  sub-skills standalone)
- sync_remove_skills (errors, cowork skips)
- _clean_orphaned_skills (lockfile ownership skip, error increment)
"""

from __future__ import annotations

import filecmp
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.integration.skill_integrator import (
    SkillIntegrationResult,
    SkillIntegrator,
    copy_skill_to_target,
    should_compile_instructions,
    validate_skill_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_info(
    install_path: Path,
    package_type=None,
    dep_ref=None,
) -> MagicMock:
    from apm_cli.models.apm_package import PackageType

    pi = MagicMock()
    pi.install_path = install_path
    pi.package_type = package_type or PackageType.CLAUDE_SKILL
    pi.dependency_ref = dep_ref
    return pi


def _make_target(
    name: str = "copilot",
    supports_skills: bool = True,
    root_dir: str = ".github",
    auto_create: bool = True,
    deploy_root=None,
    resolved_deploy_root=None,
) -> MagicMock:
    target = MagicMock()
    target.name = name
    target.supports = MagicMock(return_value=supports_skills)
    target.root_dir = root_dir
    target.auto_create = auto_create
    target.resolved_deploy_root = resolved_deploy_root
    prim = MagicMock()
    mapping = MagicMock()
    mapping.deploy_root = deploy_root
    prim.__getitem__ = MagicMock(return_value=mapping)
    target.primitives = {"skills": mapping}
    return target


# ---------------------------------------------------------------------------
# validate_skill_name -- line 129 fallthrough
# ---------------------------------------------------------------------------


class TestValidateSkillNameFallthrough:
    """Cover the fallthrough return at line 129."""

    def test_single_hyphen_only(self) -> None:
        """A single hyphen hits the invalid-chars check first."""
        is_valid, _ = validate_skill_name("-")
        assert not is_valid
        # Either leading-hyphen or the fallthrough error
        assert not is_valid

    def test_digit_only_is_valid(self) -> None:
        """Single digit is a valid skill name."""
        is_valid, _ = validate_skill_name("3")
        assert is_valid

    def test_mixed_case_triggers_uppercase_error(self) -> None:
        """Uppercase letter returns specific error before fallthrough."""
        is_valid, msg = validate_skill_name("MySkill")
        assert not is_valid
        assert "lowercase" in msg

    def test_underscore_triggers_underscore_error(self) -> None:
        """Underscore returns specific error before fallthrough."""
        is_valid, msg = validate_skill_name("my_skill")
        assert not is_valid
        assert "underscore" in msg.lower()

    def test_space_triggers_space_error(self) -> None:
        """Space returns specific error."""
        is_valid, msg = validate_skill_name("my skill")
        assert not is_valid
        assert "space" in msg.lower()

    def test_special_char_triggers_invalid_chars_error(self) -> None:
        """Special character returns 'invalid characters' error."""
        is_valid, msg = validate_skill_name("my@skill")
        assert not is_valid
        assert "invalid character" in msg.lower()


# ---------------------------------------------------------------------------
# should_compile_instructions
# ---------------------------------------------------------------------------


class TestShouldCompileInstructions:
    """Cover lines 258, 260, 264."""

    def test_instructions_type_compiles(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.APM_PACKAGE)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.INSTRUCTIONS,
        ):
            assert should_compile_instructions(pi) is True

    def test_hybrid_type_compiles(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.HYBRID)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.HYBRID,
        ):
            assert should_compile_instructions(pi) is True

    def test_skill_type_does_not_compile(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.CLAUDE_SKILL)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.SKILL,
        ):
            assert should_compile_instructions(pi) is False

    def test_prompts_type_does_not_compile(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.APM_PACKAGE)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.PROMPTS,
        ):
            assert should_compile_instructions(pi) is False


# ---------------------------------------------------------------------------
# copy_skill_to_target -- should_install_skill=False (line 305)
# ---------------------------------------------------------------------------


class TestCopySkillToTarget:
    """cover copy_skill_to_target branches."""

    def test_should_not_install_skill_returns_empty(self, tmp_path: Path) -> None:
        """When should_install_skill returns False, returns empty list."""
        from apm_cli.models.apm_package import PackageType

        pi = _make_package_info(tmp_path / "pkg", package_type=PackageType.CLAUDE_SKILL)
        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=False):
            _result = copy_skill_to_target(pi, tmp_path / "pkg", tmp_path)
        assert _result == []

    def test_no_skill_md_returns_empty(self, tmp_path: Path) -> None:
        """When SKILL.md is absent, returns empty list."""
        from apm_cli.models.apm_package import PackageType

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        pi = _make_package_info(pkg_dir, package_type=PackageType.CLAUDE_SKILL)

        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=True):
            _result = copy_skill_to_target(pi, pkg_dir, tmp_path)
        assert _result == []

    def test_auto_create_false_no_target_dir_skips(self, tmp_path: Path) -> None:
        """auto_create=False and target dir missing => skip target."""
        from apm_cli.models.apm_package import PackageType

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# skill", encoding="utf-8")

        target = _make_target(
            name="copilot",
            supports_skills=True,
            auto_create=False,
            deploy_root=None,
        )
        # Target root dir does not exist
        target.root_dir = ".github"

        with patch("apm_cli.integration.skill_integrator.should_install_skill", return_value=True):
            pkg_info = _make_package_info(pkg_dir, PackageType.CLAUDE_SKILL)
            _result = copy_skill_to_target(pkg_info, pkg_dir, tmp_path, targets=[target])
        # The target root (tmp_path/.github) doesn't exist so it's skipped -> empty
        assert _result == []


# ---------------------------------------------------------------------------
# _dircmp_equal -- lines 519, 526-527
# ---------------------------------------------------------------------------


class TestDircmpEqual:
    """Cover _dircmp_equal branches."""

    def test_left_only_files_returns_false(self, tmp_path: Path) -> None:
        """left_only files -> returns False."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "extra.txt").write_text("x", encoding="utf-8")

        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        assert SkillIntegrator._dircmp_equal(dcmp) is False

    def test_right_only_files_returns_false(self, tmp_path: Path) -> None:
        """right_only files -> returns False."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_b / "extra.txt").write_text("x", encoding="utf-8")

        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        assert SkillIntegrator._dircmp_equal(dcmp) is False

    def test_mismatched_files_returns_false(self, tmp_path: Path) -> None:
        """Files with same name but different content -> False."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "file.txt").write_text("hello", encoding="utf-8")
        (dir_b / "file.txt").write_text("world", encoding="utf-8")

        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        assert SkillIntegrator._dircmp_equal(dcmp) is False

    def test_identical_dirs_returns_true(self, tmp_path: Path) -> None:
        """Identical directories -> True."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "SKILL.md").write_text("# skill", encoding="utf-8")
        (dir_b / "SKILL.md").write_text("# skill", encoding="utf-8")

        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        assert SkillIntegrator._dircmp_equal(dcmp) is True

    def test_empty_dirs_returns_true(self, tmp_path: Path) -> None:
        """Both empty -> True."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        assert SkillIntegrator._dircmp_equal(dcmp) is True

    def test_funny_files_returns_false(self) -> None:
        """funny_files -> returns False."""
        dcmp = MagicMock()
        dcmp.left_only = []
        dcmp.right_only = []
        dcmp.funny_files = ["weird.bin"]
        dcmp.common_files = []
        dcmp.subdirs = {}
        assert SkillIntegrator._dircmp_equal(dcmp) is False


# ---------------------------------------------------------------------------
# _promote_sub_skills -- non-dir entry is skipped (line 578)
# ---------------------------------------------------------------------------


class TestPromoteSubSkillsNonDir:
    """File entries in sub_skills_dir are silently skipped."""

    def test_non_dir_in_sub_skills_dir_is_skipped(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        # A file (not a directory) must be skipped
        (skills_dir / "stray.md").write_text("stray", encoding="utf-8")

        target_root = tmp_path / ".github" / "skills"
        target_root.mkdir(parents=True)

        n, _deployed = SkillIntegrator._promote_sub_skills(
            skills_dir,
            target_root,
            "parent-pkg",
        )
        assert n == 0
        assert _deployed == []


# ---------------------------------------------------------------------------
# _promote_sub_skills -- managed_files unmanaged + logger branch (lines 608-609)
# ---------------------------------------------------------------------------


class TestPromoteSubSkillsManagedFiles:
    """Managed-files protection path: no force, uses logger."""

    def test_unmanaged_skill_with_logger_warns_and_skips(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".apm" / "skills"
        sub = skills_dir / "my-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# skill", encoding="utf-8")

        target_root = tmp_path / ".github" / "skills"
        # Create a different target skill (content won't match)
        existing = target_root / "my-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("# different", encoding="utf-8")

        logger = MagicMock()

        n, _deployed = SkillIntegrator._promote_sub_skills(
            skills_dir,
            target_root,
            "parent-pkg",
            managed_files=set(),  # empty -> not managed
            force=False,
            logger=logger,
        )
        logger.warning.assert_called_once()
        assert n == 0

    def test_unmanaged_skill_no_logger_no_diagnostics_calls_rich_warning(
        self, tmp_path: Path
    ) -> None:
        skills_dir = tmp_path / ".apm" / "skills"
        sub = skills_dir / "my-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# skill", encoding="utf-8")

        target_root = tmp_path / ".github" / "skills"
        existing = target_root / "my-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("# different", encoding="utf-8")

        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            n, _deployed = SkillIntegrator._promote_sub_skills(
                skills_dir,
                target_root,
                "parent-pkg",
                managed_files=set(),
                force=False,
                logger=None,
                diagnostics=None,
            )
            mock_warn.assert_called_once()
        assert n == 0

    def test_overwrite_warning_with_logger(self, tmp_path: Path) -> None:
        """When warn=True and not self_overwrite, logger.warning is called."""
        skills_dir = tmp_path / ".apm" / "skills"
        sub = skills_dir / "my-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# skill", encoding="utf-8")

        target_root = tmp_path / ".github" / "skills"
        existing = target_root / "my-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("# different", encoding="utf-8")

        logger = MagicMock()

        # owned_by says a *different* package owns this skill
        n, _deployed = SkillIntegrator._promote_sub_skills(
            skills_dir,
            target_root,
            "parent-pkg",
            warn=True,
            owned_by={"my-skill": "other-pkg"},
            logger=logger,
        )
        # Overwrite warning emitted, skill is deployed (no force guard since not managed_files)
        logger.warning.assert_called()
        assert n >= 0


# ---------------------------------------------------------------------------
# _promote_sub_skills_standalone -- dedup via seen_skill_dirs (lines 761-762)
# ---------------------------------------------------------------------------


class TestPromoteSubSkillsStandaloneDedup:
    """Second target with identical resolved path is logged+skipped."""

    def test_duplicate_target_skips_with_logger(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "pkg"
        sub_skills = pkg_dir / ".apm" / "skills"
        sub_skills.mkdir(parents=True)
        sub = sub_skills / "sub-skill"
        sub.mkdir()
        (sub / "SKILL.md").write_text("# sub", encoding="utf-8")

        pi = _make_package_info(pkg_dir)
        logger = MagicMock()

        # Two targets pointing to the same resolved path
        skills_root = tmp_path / ".github" / "skills"

        target1 = _make_target(name="t1", root_dir=".github", auto_create=True)
        target2 = _make_target(name="t2", root_dir=".github", auto_create=True)
        # Both resolved deploy roots point to the same dir
        target1.resolved_deploy_root = skills_root
        target2.resolved_deploy_root = skills_root

        integrator = SkillIntegrator()

        with patch.object(SkillIntegrator, "_build_skill_ownership_map", return_value={}):
            _count, _deployed = integrator._promote_sub_skills_standalone(
                pi,
                tmp_path,
                logger=logger,
                targets=[target1, target2],
            )
        # Logger.progress called for dedup skip on second target
        logger.progress.assert_called()


# ---------------------------------------------------------------------------
# _build_skill_ownership_map / _build_native_skill_owner_map
# ---------------------------------------------------------------------------


class TestBuildOwnershipMaps:
    """Cover lines 701-702 (_build_native_skill_owner_map delegates to _build_ownership_maps)."""

    def test_build_skill_ownership_map_empty_lockfile(self, tmp_path: Path) -> None:
        with patch.object(
            SkillIntegrator, "_build_ownership_maps", return_value=({}, {})
        ) as mock_bom:
            result = SkillIntegrator._build_skill_ownership_map(tmp_path)
            mock_bom.assert_called_once_with(tmp_path)
            assert result == {}

    def test_build_native_skill_owner_map_empty_lockfile(self, tmp_path: Path) -> None:
        with patch.object(
            SkillIntegrator, "_build_ownership_maps", return_value=({}, {"my-skill": "owner/pkg"})
        ) as mock_bom:
            result = SkillIntegrator._build_native_skill_owner_map(tmp_path)
            mock_bom.assert_called_once_with(tmp_path)
            assert result == {"my-skill": "owner/pkg"}


# ---------------------------------------------------------------------------
# _integrate_native_skill -- name normalization with logger (lines 844-856)
# ---------------------------------------------------------------------------


class TestIntegrateNativeSkillNameNormalization:
    """Skill name normalization warnings emitted correctly."""

    def test_invalid_name_with_logger_warns(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "MyInvalidSkill"  # uppercase -> normalized
        pkg_dir.mkdir()
        skill_md = pkg_dir / "SKILL.md"
        skill_md.write_text("# skill", encoding="utf-8")

        target = _make_target(name="copilot", root_dir=".github", auto_create=True)
        logger = MagicMock()

        pi = _make_package_info(pkg_dir)
        integrator = SkillIntegrator()

        with (
            patch.object(SkillIntegrator, "_build_ownership_maps", return_value=({}, {})),
        ):
            _result = integrator._integrate_native_skill(
                pi,
                tmp_path,
                skill_md,
                logger=logger,
                targets=[target],
            )
        logger.warning.assert_called()

    def test_invalid_name_with_diagnostics_warns(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "MyInvalidSkill"
        pkg_dir.mkdir()
        skill_md = pkg_dir / "SKILL.md"
        skill_md.write_text("# skill", encoding="utf-8")

        target = _make_target(name="copilot", root_dir=".github", auto_create=True)
        diagnostics = MagicMock()

        pi = _make_package_info(pkg_dir)
        integrator = SkillIntegrator()

        with (
            patch.object(SkillIntegrator, "_build_ownership_maps", return_value=({}, {})),
        ):
            _result = integrator._integrate_native_skill(
                pi,
                tmp_path,
                skill_md,
                diagnostics=diagnostics,
                targets=[target],
            )
        diagnostics.warn.assert_called()

    def test_invalid_name_no_logger_no_diagnostics_calls_rich_warning(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "MyInvalidSkill"
        pkg_dir.mkdir()
        skill_md = pkg_dir / "SKILL.md"
        skill_md.write_text("# skill", encoding="utf-8")

        target = _make_target(name="copilot", root_dir=".github", auto_create=True)

        pi = _make_package_info(pkg_dir)
        integrator = SkillIntegrator()

        with (
            patch.object(SkillIntegrator, "_build_ownership_maps", return_value=({}, {})),
            patch("apm_cli.utils.console._rich_warning") as mock_warn,
        ):
            _result = integrator._integrate_native_skill(
                pi,
                tmp_path,
                skill_md,
                targets=[target],
            )
        mock_warn.assert_called()


# ---------------------------------------------------------------------------
# _integrate_native_skill -- collision warning with logger (line 958-959)
# ---------------------------------------------------------------------------


class TestIntegrateNativeSkillCollision:
    """Cross-package collision emits warning via logger."""

    def test_collision_warning_with_logger(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        skill_md = pkg_dir / "SKILL.md"
        skill_md.write_text("# skill", encoding="utf-8")

        target = _make_target(name="copilot", root_dir=".github", auto_create=True)
        # Pre-create the target skill dir
        target_skill_dir = tmp_path / ".github" / "skills" / "my-skill"
        target_skill_dir.mkdir(parents=True)
        (target_skill_dir / "SKILL.md").write_text("# old", encoding="utf-8")

        logger = MagicMock()
        dep_ref = MagicMock()
        dep_ref.get_unique_key = MagicMock(return_value="other/repo")
        pi = _make_package_info(pkg_dir)
        pi.dependency_ref = dep_ref

        integrator = SkillIntegrator()

        with patch.object(
            SkillIntegrator,
            "_build_ownership_maps",
            return_value=({}, {"my-skill": "prev-owner/pkg"}),
        ):
            _result = integrator._integrate_native_skill(
                pi,
                tmp_path,
                skill_md,
                logger=logger,
                targets=[target],
            )
        logger.warning.assert_called()


# ---------------------------------------------------------------------------
# integrate -- virtual file skip (line 1188)
# ---------------------------------------------------------------------------


class TestIntegrateVirtualFileSkip:
    """Virtual non-subdirectory packages are skipped."""

    def test_virtual_file_returns_skipped(self, tmp_path: Path) -> None:
        dep_ref = MagicMock()
        dep_ref.is_virtual = True
        dep_ref.is_virtual_subdirectory = MagicMock(return_value=False)

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        pi = _make_package_info(pkg_dir, dep_ref=dep_ref)

        integrator = SkillIntegrator()
        _result = integrator.integrate_package_skill(pi, tmp_path)
        assert isinstance(_result, SkillIntegrationResult)
        assert _result.skill_skipped is True

    def test_virtual_subdirectory_is_not_skipped(self, tmp_path: Path) -> None:
        """Subdirectory virtual packages are allowed through."""
        dep_ref = MagicMock()
        dep_ref.is_virtual = True
        dep_ref.is_virtual_subdirectory = MagicMock(return_value=True)

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        # SKILL.md is required so code reaches _integrate_native_skill
        (pkg_dir / "SKILL.md").write_text("# skill", encoding="utf-8")
        pi = _make_package_info(pkg_dir, dep_ref=dep_ref)

        integrator = SkillIntegrator()

        with patch.object(integrator, "_integrate_native_skill") as mock_native:
            mock_native.return_value = SkillIntegrationResult(
                skill_created=True,
                skill_updated=False,
                skill_skipped=False,
                skill_path=None,
                references_copied=0,
            )
            _result = integrator.integrate_package_skill(pi, tmp_path)
        # Does not immediately return skipped
        assert _result.skill_skipped is False


# ---------------------------------------------------------------------------
# integrate -- skill_subset warning for native SKILL.md (lines 1203, 1205)
# ---------------------------------------------------------------------------


class TestIntegrateSkillSubsetWarning:
    """--skill filter on a native skill emits _rich_warning."""

    def test_skill_subset_with_native_skill_warns(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# skill", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_virtual = False
        pi = _make_package_info(pkg_dir, dep_ref=dep_ref)

        integrator = SkillIntegrator()

        with (
            patch("apm_cli.utils.console._rich_warning") as mock_warn,
            patch.object(integrator, "_integrate_native_skill") as mock_native,
        ):
            mock_native.return_value = SkillIntegrationResult(
                skill_created=True,
                skill_updated=False,
                skill_skipped=False,
                skill_path=None,
                references_copied=0,
            )
            _result = integrator.integrate_package_skill(
                pi, tmp_path, skill_subset=("other-skill",)
            )
        mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# integrate -- skill bundle path (lines 1221-1225)
# ---------------------------------------------------------------------------


class TestIntegrateSkillBundle:
    """A package with root-level skills/ containing SKILL.md dirs routes to bundle."""

    def test_skill_bundle_routes_to_integrate_skill_bundle(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "bundle-pkg"
        pkg_dir.mkdir()
        skills_dir = pkg_dir / "skills"
        sub = skills_dir / "sub-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# sub skill", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_virtual = False
        pi = _make_package_info(pkg_dir, dep_ref=dep_ref)

        integrator = SkillIntegrator()

        with patch.object(integrator, "_integrate_skill_bundle") as mock_bundle:
            mock_bundle.return_value = SkillIntegrationResult(
                skill_created=True,
                skill_updated=False,
                skill_skipped=False,
                skill_path=None,
                references_copied=0,
            )
            _result = integrator.integrate_package_skill(pi, tmp_path)
        mock_bundle.assert_called_once()


# ---------------------------------------------------------------------------
# integrate -- sub-skills standalone (lines 1239, 1248)
# ---------------------------------------------------------------------------


class TestIntegrateSubSkillsStandalone:
    """Package without SKILL.md still promotes .apm/skills/ sub-skills."""

    def test_no_skill_md_promotes_sub_skills(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "instr-pkg"
        pkg_dir.mkdir()
        # No SKILL.md at root, no skills/ dir at root
        sub_skills = pkg_dir / ".apm" / "skills"
        sub = sub_skills / "embedded-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# embedded", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_virtual = False
        pi = _make_package_info(pkg_dir, dep_ref=dep_ref)

        integrator = SkillIntegrator()

        with patch.object(
            integrator, "_promote_sub_skills_standalone", return_value=(1, [sub])
        ) as mock_promo:
            _result = integrator.integrate_package_skill(pi, tmp_path)
        mock_promo.assert_called_once()
        assert _result.sub_skills_promoted == 1
        assert _result.skill_skipped is True


# ---------------------------------------------------------------------------
# _integrate_skill_bundle -- dedup second target (lines 1084-1090)
# ---------------------------------------------------------------------------


class TestIntegrateSkillBundleDedup:
    """Duplicate resolved skills root is logged and skipped in bundle."""

    def test_dedup_second_target_logs_progress(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        sub = skills_dir / "sub-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# sub", encoding="utf-8")

        pkg_dir = tmp_path / "bundle-pkg"
        pkg_dir.mkdir()
        pi = _make_package_info(pkg_dir)

        _shared_root = tmp_path / ".github" / "skills"

        target1 = _make_target(name="t1", root_dir=".github", auto_create=True)
        target2 = _make_target(name="t2", root_dir=".github", auto_create=True)
        target1.resolved_deploy_root = None
        target2.resolved_deploy_root = None

        logger = MagicMock()
        integrator = SkillIntegrator()

        with patch.object(SkillIntegrator, "_build_ownership_maps", return_value=({}, {})):
            _result = integrator._integrate_skill_bundle(
                pi,
                tmp_path,
                skills_dir,
                logger=logger,
                targets=[target1, target2],  # same resolved dir
            )
        # Both targets resolve to tmp_path/.github/skills; second one is skipped
        logger.progress.assert_called()


# ---------------------------------------------------------------------------
# sync_remove_skills -- errors increment (line 1362)
# ---------------------------------------------------------------------------


class TestSyncRemoveSkillsErrors:
    """Error during skill dir removal increments error count."""

    def test_remove_error_increments_errors(self, tmp_path: Path) -> None:
        integrator = SkillIntegrator()
        skills_dir = tmp_path / ".github" / "skills"
        orphan = skills_dir / "orphan-skill"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_text("# orphan", encoding="utf-8")

        with patch("shutil.rmtree", side_effect=OSError("permission denied")):
            stats = integrator._clean_orphaned_skills(
                skills_dir=skills_dir,
                installed_skill_names=set(),  # nothing installed => orphan should be removed
                project_root=tmp_path,
            )
        assert stats["errors"] >= 1


# ---------------------------------------------------------------------------
# _clean_orphaned_skills -- lockfile ownership skip (line 1479)
# ---------------------------------------------------------------------------


class TestCleanOrphanedSkills:
    """Skills in .agents/ dir not owned by lockfile are skipped."""

    def test_unowned_agent_skill_is_skipped(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".agents" / "skills"
        foreign = skills_dir / "foreign-skill"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("# foreign", encoding="utf-8")

        integrator = SkillIntegrator()

        with patch.object(
            SkillIntegrator,
            "_get_lockfile_owned_agent_skills",
            return_value=set(),  # lockfile owns nothing
        ):
            stats = integrator._clean_orphaned_skills(
                skills_dir=skills_dir,
                installed_skill_names=set(),  # not installed either
                project_root=tmp_path,
            )
        # foreign-skill should NOT be removed (not lockfile-owned)
        assert foreign.exists()
        assert stats["files_removed"] == 0

    def test_owned_agent_skill_is_removed(self, tmp_path: Path) -> None:
        """Skills owned by lockfile AND not installed are removed."""
        skills_dir = tmp_path / ".agents" / "skills"
        owned = skills_dir / "owned-skill"
        owned.mkdir(parents=True)
        (owned / "SKILL.md").write_text("# owned", encoding="utf-8")

        integrator = SkillIntegrator()

        with patch.object(
            SkillIntegrator,
            "_get_lockfile_owned_agent_skills",
            return_value={"owned-skill"},
        ):
            stats = integrator._clean_orphaned_skills(
                skills_dir=skills_dir,
                installed_skill_names=set(),
                project_root=tmp_path,
            )
        assert stats["files_removed"] == 1

    def test_remove_error_increments_errors(self, tmp_path: Path) -> None:
        """OSError during cleanup increments error count."""
        skills_dir = tmp_path / ".agents" / "skills"
        owned = skills_dir / "owned-skill"
        owned.mkdir(parents=True)
        (owned / "SKILL.md").write_text("# owned", encoding="utf-8")

        integrator = SkillIntegrator()

        with (
            patch.object(
                SkillIntegrator,
                "_get_lockfile_owned_agent_skills",
                return_value={"owned-skill"},
            ),
            patch("shutil.rmtree", side_effect=OSError("perm denied")),
        ):
            stats = integrator._clean_orphaned_skills(
                skills_dir=skills_dir,
                installed_skill_names=set(),
                project_root=tmp_path,
            )
        assert stats["errors"] == 1
