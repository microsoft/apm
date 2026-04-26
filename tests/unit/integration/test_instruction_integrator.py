"""Tests for instruction integration functionality."""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock
from datetime import datetime

from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.models.apm_package import PackageInfo, APMPackage, ResolvedReference, GitReferenceType


def _make_package_info(package_dir, name="test-pkg"):
    """Helper shared across test classes."""
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


class TestInstructionIntegrator:
    """Test instruction integration logic."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_package_info(self, package_dir, name="test-pkg"):
        package = APMPackage(
            name=name,
            version="1.0.0",
            package_path=package_dir,
            source=f"github.com/test/{name}",
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

    # ===== Discovery =====

    def test_should_integrate_always_returns_true(self):
        assert self.integrator.should_integrate(self.project_root) is True

    def test_find_instruction_files_in_apm_instructions(self):
        """Finds *.instructions.md files under .apm/instructions/."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("---\napplyTo: '**/*.py'\n---\n# Python rules")
        (inst_dir / "readme.md").write_text("# Not an instruction")

        files = self.integrator.find_instruction_files(pkg)
        assert len(files) == 1
        assert files[0].name == "python.instructions.md"

    def test_find_instruction_files_returns_empty_when_no_dir(self):
        pkg = self.project_root / "package"
        pkg.mkdir()
        assert self.integrator.find_instruction_files(pkg) == []

    def test_find_multiple_instruction_files(self):
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")
        (inst_dir / "testing.instructions.md").write_text("# Testing")
        (inst_dir / "security.instructions.md").write_text("# Security")

        files = self.integrator.find_instruction_files(pkg)
        assert len(files) == 3

    # ===== Copy =====

    def test_copy_instruction_verbatim(self):
        """Copies content without modification when no link resolver."""
        source = self.project_root / "source.instructions.md"
        target = self.project_root / "target.instructions.md"
        content = "---\napplyTo: '**/*.py'\n---\n# Python coding standards\n\nUse type hints."
        source.write_text(content)

        self.integrator.copy_instruction(source, target)
        assert target.read_text() == content

    def test_copy_instruction_preserves_frontmatter(self):
        """Frontmatter with applyTo is preserved exactly."""
        source = self.project_root / "source.instructions.md"
        target = self.project_root / "target.instructions.md"
        content = "---\napplyTo: 'src/**/*.ts'\ndescription: TypeScript guidelines\n---\n\n# TS Rules"
        source.write_text(content)

        self.integrator.copy_instruction(source, target)
        assert target.read_text() == content

    # ===== Integration =====

    def test_integrate_creates_target_directory(self):
        """Creates .github/instructions/ if it doesn't exist."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        (self.project_root / ".github").mkdir()
        pkg_info = self._make_package_info(pkg)

        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert result.files_integrated == 1
        assert (self.project_root / ".github" / "instructions").exists()

    def test_integrate_returns_integration_result(self):
        """Returns IntegrationResult (shared base type, not custom dataclass)."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert isinstance(result, IntegrationResult)

    def test_integrate_keeps_original_filename(self):
        """Deploys with original filename — no suffix, no renaming."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python rules")

        pkg_info = self._make_package_info(pkg)
        self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        target = self.project_root / ".github" / "instructions" / "python.instructions.md"
        assert target.exists()
        assert target.read_text() == "# Python rules"

    def test_integrate_overwrites_when_no_manifest(self):
        """Without managed_files (no manifest), overwrites existing files."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# New version")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Old version")

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert result.files_integrated == 1
        assert (target_dir / "python.instructions.md").read_text() == "# New version"

    def test_integrate_skips_user_file_collision(self):
        """Skips user-authored file when managed_files says it's not APM-owned."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM version")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# User version")

        pkg_info = self._make_package_info(pkg)
        # managed_files is empty set — python.instructions.md not in it → user-authored
        result = self.integrator.integrate_package_instructions(
            pkg_info, self.project_root, managed_files=set()
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert (target_dir / "python.instructions.md").read_text() == "# User version"

    def test_integrate_overwrites_managed_file(self):
        """Overwrites file when managed_files includes it (APM-owned)."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Updated APM version")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Old APM version")

        pkg_info = self._make_package_info(pkg)
        managed = {".github/instructions/python.instructions.md"}
        result = self.integrator.integrate_package_instructions(
            pkg_info, self.project_root, managed_files=managed
        )

        assert result.files_integrated == 1
        assert (target_dir / "python.instructions.md").read_text() == "# Updated APM version"

    def test_integrate_force_overwrites_user_file(self):
        """Force flag overrides collision detection."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM version")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# User version")

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(
            pkg_info, self.project_root, force=True, managed_files=set()
        )

        assert result.files_integrated == 1
        assert (target_dir / "python.instructions.md").read_text() == "# APM version"

    def test_integrate_multiple_files_from_one_package(self):
        """Integrates all instruction files from a single package."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")
        (inst_dir / "testing.instructions.md").write_text("# Testing")

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert result.files_integrated == 2
        target_dir = self.project_root / ".github" / "instructions"
        assert (target_dir / "python.instructions.md").exists()
        assert (target_dir / "testing.instructions.md").exists()

    def test_integrate_returns_empty_when_no_instructions(self):
        """Returns zero-result when package has no instruction files."""
        pkg = self.project_root / "package"
        pkg.mkdir()

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert result.files_integrated == 0
        assert result.target_paths == []

    def test_integrate_preserves_user_files_with_different_names(self):
        """User-authored instruction files with different names are untouched."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM Python")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "my-custom.instructions.md").write_text("# My custom instructions")

        pkg_info = self._make_package_info(pkg)
        self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert (target_dir / "my-custom.instructions.md").read_text() == "# My custom instructions"
        assert (target_dir / "python.instructions.md").read_text() == "# APM Python"

    def test_integrate_target_paths_are_absolute(self):
        """Target paths in result are absolute Path objects for deployed_files tracking."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = self._make_package_info(pkg)
        result = self.integrator.integrate_package_instructions(pkg_info, self.project_root)

        assert len(result.target_paths) == 1
        tp = result.target_paths[0]
        assert tp.is_absolute()
        assert tp.relative_to(self.project_root).as_posix() == ".github/instructions/python.instructions.md"


class TestInstructionSyncIntegration:
    """Test sync_integration (manifest-based removal for uninstall)."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_removes_managed_files(self):
        """Removes files listed in managed_files from deployed_files manifest."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Python")
        (target_dir / "testing.instructions.md").write_text("# Testing")

        managed = {
            ".github/instructions/python.instructions.md",
            ".github/instructions/testing.instructions.md",
        }
        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=managed)

        assert result['files_removed'] == 2
        assert not (target_dir / "python.instructions.md").exists()
        assert not (target_dir / "testing.instructions.md").exists()

    def test_sync_preserves_unmanaged_files(self):
        """Files not in managed_files are preserved (user-authored)."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# APM Python")
        (target_dir / "my-custom.instructions.md").write_text("# User-authored")

        managed = {".github/instructions/python.instructions.md"}
        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=managed)

        assert result['files_removed'] == 1
        assert not (target_dir / "python.instructions.md").exists()
        assert (target_dir / "my-custom.instructions.md").exists()

    def test_sync_legacy_fallback_removes_all_instruction_files(self):
        """Without managed_files, falls back to glob removing all *.instructions.md."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Python")
        (target_dir / "testing.instructions.md").write_text("# Testing")

        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=None)

        assert result['files_removed'] == 2

    def test_sync_legacy_preserves_non_instruction_files(self):
        """Legacy glob only matches *.instructions.md — other files preserved."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Python")
        (target_dir / "README.md").write_text("# Readme")
        (target_dir / "notes.txt").write_text("notes")

        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=None)

        assert result['files_removed'] == 1
        assert (target_dir / "README.md").exists()
        assert (target_dir / "notes.txt").exists()

    def test_sync_handles_missing_instructions_dir(self):
        """Gracefully handles missing .github/instructions/."""
        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root)

        assert result['files_removed'] == 0
        assert result['errors'] == 0

    def test_sync_empty_managed_files_removes_nothing(self):
        """Empty managed_files set removes nothing."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Python")

        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=set())

        assert result['files_removed'] == 0
        assert (target_dir / "python.instructions.md").exists()

    def test_sync_skips_files_not_on_disk(self):
        """Managed files that don't exist on disk are gracefully skipped."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)

        managed = {".github/instructions/nonexistent.instructions.md"}
        apm_package = Mock()
        result = self.integrator.sync_integration(apm_package, self.project_root, managed_files=managed)

        assert result['files_removed'] == 0


class TestInstructionNameCollision:
    """Test behavior when APM instruction filenames collide with user files."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_package_info(self, package_dir, name="test-pkg"):
        package = APMPackage(
            name=name,
            version="1.0.0",
            package_path=package_dir,
        )
        resolved_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )
        return PackageInfo(
            package=package,
            install_path=package_dir,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
        )

    def test_install_overwrites_when_managed(self):
        """APM-managed file with same name is overwritten."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM Python standards")

        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)
        (target_dir / "python.instructions.md").write_text("# Old version")

        pkg_info = self._make_package_info(pkg)
        managed = {".github/instructions/python.instructions.md"}
        result = self.integrator.integrate_package_instructions(
            pkg_info, self.project_root, managed_files=managed
        )

        assert result.files_integrated == 1
        assert (target_dir / "python.instructions.md").read_text() == "# APM Python standards"

    def test_two_packages_same_instruction_name_last_wins(self):
        """When two packages deploy the same filename, last-installed wins."""
        target_dir = self.project_root / ".github" / "instructions"
        target_dir.mkdir(parents=True)

        # Package A installs first
        pkg_a = self.project_root / "pkg-a"
        inst_a = pkg_a / ".apm" / "instructions"
        inst_a.mkdir(parents=True)
        (inst_a / "python.instructions.md").write_text("# Package A rules")
        info_a = self._make_package_info(pkg_a, "pkg-a")
        self.integrator.integrate_package_instructions(info_a, self.project_root)

        # Package B installs second — same filename
        pkg_b = self.project_root / "pkg-b"
        inst_b = pkg_b / ".apm" / "instructions"
        inst_b.mkdir(parents=True)
        (inst_b / "python.instructions.md").write_text("# Package B rules")
        info_b = self._make_package_info(pkg_b, "pkg-b")
        self.integrator.integrate_package_instructions(info_b, self.project_root)

        # Last write wins
        assert (target_dir / "python.instructions.md").read_text() == "# Package B rules"


# ==================================================================
# Cursor Rules (.mdc) tests
# ==================================================================


class TestConvertToCursorRules:
    """Test the frontmatter conversion helper."""

    def test_maps_apply_to_to_globs(self):
        content = "---\napplyTo: 'src/**/*.py'\n---\n\n# Python rules"
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert 'globs: "src/**/*.py"' in result
        assert "applyTo" not in result

    def test_preserves_description(self):
        content = "---\napplyTo: '**/*.ts'\ndescription: TypeScript guidelines\n---\n\n# TS Rules"
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "description: TypeScript guidelines" in result
        assert 'globs: "**/*.ts"' in result

    def test_generates_description_from_heading(self):
        content = "---\napplyTo: '**/*.py'\n---\n\n# Python coding standards\n\nUse type hints."
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "description: Python coding standards" in result

    def test_generates_description_from_first_sentence(self):
        content = "---\napplyTo: '**'\n---\n\nAlways use descriptive names. Follow PEP8."
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "description: Always use descriptive names" in result

    def test_no_frontmatter(self):
        content = "# Simple rules\n\nJust some guidelines."
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert result.startswith("---\n")
        assert "description: Simple rules" in result
        # No globs when no applyTo
        assert "globs" not in result

    def test_body_preserved(self):
        content = "---\napplyTo: '**/*.py'\n---\n\n# Python rules\n\nUse type hints."
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "# Python rules" in result
        assert "Use type hints." in result

    def test_empty_apply_to_omits_globs(self):
        content = "---\ndescription: General rules\n---\n\n# Rules"
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "globs" not in result
        assert "description: General rules" in result


class TestCursorRulesIntegration:
    """Test integrate_package_instructions_cursor()."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skips_when_no_cursor_dir(self):
        """Returns empty result when .cursor/ doesn't exist."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert result.files_integrated == 0
        assert result.target_paths == []

    def test_deploys_when_cursor_dir_exists(self):
        """Deploys .mdc files when .cursor/ exists."""
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\n\n# Python rules"
        )

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert result.files_integrated == 1
        target = self.project_root / ".cursor" / "rules" / "python.mdc"
        assert target.exists()
        content = target.read_text()
        assert 'globs: "**/*.py"' in content
        assert "# Python rules" in content

    def test_creates_rules_subdirectory(self):
        """Creates .cursor/rules/ if it doesn't exist."""
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "test.instructions.md").write_text("# Test")

        pkg_info = _make_package_info(pkg)
        self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert (self.project_root / ".cursor" / "rules").is_dir()

    def test_filename_strips_instructions_md_adds_mdc(self):
        """Converts python.instructions.md → python.mdc."""
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "security.instructions.md").write_text("# Security")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert len(result.target_paths) == 1
        assert result.target_paths[0].name == "security.mdc"

    def test_multiple_files(self):
        """Integrates multiple instruction files as .mdc rules."""
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")
        (inst_dir / "testing.instructions.md").write_text("# Testing")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert result.files_integrated == 2
        rules_dir = self.project_root / ".cursor" / "rules"
        assert (rules_dir / "python.mdc").exists()
        assert (rules_dir / "testing.mdc").exists()

    def test_returns_empty_when_no_instruction_files(self):
        (self.project_root / ".cursor").mkdir()
        pkg = self.project_root / "package"
        pkg.mkdir()

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert result.files_integrated == 0
        assert result.target_paths == []

    def test_collision_detection_skips_user_file(self):
        """Skips user-authored .mdc file when not in managed_files."""
        (self.project_root / ".cursor").mkdir()
        rules_dir = self.project_root / ".cursor" / "rules"
        rules_dir.mkdir()
        (rules_dir / "python.mdc").write_text("# User rules")

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM rules")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(
            pkg_info, self.project_root, managed_files=set()
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert (rules_dir / "python.mdc").read_text() == "# User rules"

    def test_overwrites_managed_file(self):
        """Overwrites file when it's in managed_files."""
        (self.project_root / ".cursor").mkdir()
        rules_dir = self.project_root / ".cursor" / "rules"
        rules_dir.mkdir()
        (rules_dir / "python.mdc").write_text("# Old version")

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Updated")

        pkg_info = _make_package_info(pkg)
        managed = {".cursor/rules/python.mdc"}
        result = self.integrator.integrate_package_instructions_cursor(
            pkg_info, self.project_root, managed_files=managed
        )

        assert result.files_integrated == 1

    def test_target_paths_are_absolute(self):
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        assert len(result.target_paths) == 1
        tp = result.target_paths[0]
        assert tp.is_absolute()
        assert tp.relative_to(self.project_root).as_posix() == ".cursor/rules/python.mdc"

    def test_frontmatter_conversion_in_deployed_file(self):
        """End-to-end: applyTo converts to globs in deployed .mdc."""
        (self.project_root / ".cursor").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "ts.instructions.md").write_text(
            "---\napplyTo: 'src/**/*.ts'\ndescription: TypeScript rules\n---\n\n# TypeScript\n\nUse strict mode."
        )

        pkg_info = _make_package_info(pkg)
        self.integrator.integrate_package_instructions_cursor(pkg_info, self.project_root)

        deployed = (self.project_root / ".cursor" / "rules" / "ts.mdc").read_text()
        assert 'globs: "src/**/*.ts"' in deployed
        assert "description: TypeScript rules" in deployed
        assert "applyTo" not in deployed
        assert "# TypeScript" in deployed
        assert "Use strict mode." in deployed


class TestCursorRulesSyncIntegration:
    """Test sync_integration_cursor (manifest-based removal)."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_removes_managed_mdc_files(self):
        rules_dir = self.project_root / ".cursor" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.mdc").write_text("# Python")
        (rules_dir / "testing.mdc").write_text("# Testing")

        managed = {
            ".cursor/rules/python.mdc",
            ".cursor/rules/testing.mdc",
        }
        apm_package = Mock()
        result = self.integrator.sync_integration_cursor(apm_package, self.project_root, managed_files=managed)

        assert result["files_removed"] == 2
        assert not (rules_dir / "python.mdc").exists()
        assert not (rules_dir / "testing.mdc").exists()

    def test_sync_preserves_unmanaged_files(self):
        rules_dir = self.project_root / ".cursor" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.mdc").write_text("# APM")
        (rules_dir / "my-custom.mdc").write_text("# User-authored")

        managed = {".cursor/rules/python.mdc"}
        apm_package = Mock()
        result = self.integrator.sync_integration_cursor(apm_package, self.project_root, managed_files=managed)

        assert result["files_removed"] == 1
        assert (rules_dir / "my-custom.mdc").exists()

    def test_sync_legacy_fallback_removes_all_mdc(self):
        rules_dir = self.project_root / ".cursor" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.mdc").write_text("# Python")
        (rules_dir / "testing.mdc").write_text("# Testing")

        apm_package = Mock()
        result = self.integrator.sync_integration_cursor(apm_package, self.project_root, managed_files=None)

        assert result["files_removed"] == 2

    def test_sync_handles_missing_rules_dir(self):
        apm_package = Mock()
        result = self.integrator.sync_integration_cursor(apm_package, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0


# ======================================================================
# Claude Code Rules (.md with paths: frontmatter)
# ======================================================================


class TestConvertToClaudeRules:
    """Test the _convert_to_claude_rules() frontmatter conversion helper."""

    def test_maps_apply_to_to_paths(self):
        content = "---\napplyTo: 'src/**/*.py'\n---\n\n# Python rules"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" in result
        assert '  - "src/**/*.py"' in result
        assert "applyTo" not in result

    def test_preserves_body(self):
        content = "---\napplyTo: '**/*.ts'\n---\n\n# TypeScript\n\nUse strict mode."
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "# TypeScript" in result
        assert "Use strict mode." in result

    def test_no_frontmatter_returns_body_only(self):
        content = "# Simple rules\n\nJust some guidelines."
        result = InstructionIntegrator._convert_to_claude_rules(content)
        # No paths key when no applyTo
        assert "paths:" not in result
        assert "---" not in result
        assert "# Simple rules" in result
        assert "Just some guidelines." in result

    def test_no_apply_to_omits_paths(self):
        content = "---\ndescription: General rules\n---\n\n# Rules"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" not in result
        # Frontmatter stripped, body returned
        assert "# Rules" in result

    def test_description_field_stripped_from_frontmatter(self):
        """Claude rules use paths: only; description is not a valid key."""
        content = "---\napplyTo: '**/*.py'\ndescription: Python guidelines\n---\n\n# Python"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" in result
        assert '  - "**/*.py"' in result
        # description should NOT appear in Claude frontmatter
        assert "description:" not in result

    def test_empty_apply_to_returns_body(self):
        content = "---\napplyTo: ''\n---\n\n# Rules"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" not in result
        assert "# Rules" in result

    def test_quoted_apply_to_double(self):
        content = '---\napplyTo: "src/api/**/*.ts"\n---\n\n# API rules'
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert '  - "src/api/**/*.ts"' in result


class TestClaudeRulesIntegration:
    """Test integrate_package_instructions_claude()."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skips_when_no_claude_dir(self):
        """Returns empty result when .claude/ doesn't exist."""
        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert result.files_integrated == 0
        assert result.target_paths == []

    def test_deploys_when_claude_dir_exists(self):
        """Deploys .md files when .claude/ exists."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\n\n# Python rules"
        )

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert result.files_integrated == 1
        target = self.project_root / ".claude" / "rules" / "python.md"
        assert target.exists()
        content = target.read_text()
        assert "paths:" in content
        assert '  - "**/*.py"' in content
        assert "# Python rules" in content

    def test_creates_rules_subdirectory(self):
        """Creates .claude/rules/ if it doesn't exist."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "test.instructions.md").write_text("# Test")

        pkg_info = _make_package_info(pkg)
        self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert (self.project_root / ".claude" / "rules").is_dir()

    def test_filename_strips_instructions_md_adds_md(self):
        """Converts python.instructions.md -> python.md."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "security.instructions.md").write_text("# Security")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert len(result.target_paths) == 1
        assert result.target_paths[0].name == "security.md"

    def test_multiple_files(self):
        """Integrates multiple instruction files as .md rules."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")
        (inst_dir / "testing.instructions.md").write_text("# Testing")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert result.files_integrated == 2
        rules_dir = self.project_root / ".claude" / "rules"
        assert (rules_dir / "python.md").exists()
        assert (rules_dir / "testing.md").exists()

    def test_returns_empty_when_no_instruction_files(self):
        (self.project_root / ".claude").mkdir()
        pkg = self.project_root / "package"
        pkg.mkdir()

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert result.files_integrated == 0
        assert result.target_paths == []

    def test_collision_detection_skips_user_file(self):
        """Skips user-authored .md file when not in managed_files."""
        (self.project_root / ".claude").mkdir()
        rules_dir = self.project_root / ".claude" / "rules"
        rules_dir.mkdir()
        (rules_dir / "python.md").write_text("# User rules")

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# APM rules")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(
            pkg_info, self.project_root, managed_files=set()
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert (rules_dir / "python.md").read_text() == "# User rules"

    def test_overwrites_managed_file(self):
        """Overwrites file when it's in managed_files."""
        (self.project_root / ".claude").mkdir()
        rules_dir = self.project_root / ".claude" / "rules"
        rules_dir.mkdir()
        (rules_dir / "python.md").write_text("# Old version")

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Updated")

        pkg_info = _make_package_info(pkg)
        managed = {".claude/rules/python.md"}
        result = self.integrator.integrate_package_instructions_claude(
            pkg_info, self.project_root, managed_files=managed
        )

        assert result.files_integrated == 1

    def test_target_paths_are_absolute(self):
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text("# Python")

        pkg_info = _make_package_info(pkg)
        result = self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        assert len(result.target_paths) == 1
        tp = result.target_paths[0]
        assert tp.is_absolute()
        assert tp.relative_to(self.project_root).as_posix() == ".claude/rules/python.md"

    def test_frontmatter_conversion_in_deployed_file(self):
        """End-to-end: applyTo converts to paths in deployed .md."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "ts.instructions.md").write_text(
            "---\napplyTo: 'src/**/*.ts'\ndescription: TypeScript rules\n---\n\n# TypeScript\n\nUse strict mode."
        )

        pkg_info = _make_package_info(pkg)
        self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        deployed = (self.project_root / ".claude" / "rules" / "ts.md").read_text()
        assert "paths:" in deployed
        assert '  - "src/**/*.ts"' in deployed
        assert "applyTo" not in deployed
        # description is NOT in Claude rules frontmatter
        assert "description:" not in deployed
        assert "# TypeScript" in deployed
        assert "Use strict mode." in deployed

    def test_unconditional_rule_has_no_frontmatter(self):
        """Instructions without applyTo become unconditional rules."""
        (self.project_root / ".claude").mkdir()

        pkg = self.project_root / "package"
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "general.instructions.md").write_text(
            "---\ndescription: General guidelines\n---\n\n# General\n\nAlways lint."
        )

        pkg_info = _make_package_info(pkg)
        self.integrator.integrate_package_instructions_claude(pkg_info, self.project_root)

        deployed = (self.project_root / ".claude" / "rules" / "general.md").read_text()
        assert "---" not in deployed
        assert "# General" in deployed
        assert "Always lint." in deployed


class TestClaudeRulesSyncIntegration:
    """Test sync_integration_claude (manifest-based removal)."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_removes_managed_md_files(self):
        rules_dir = self.project_root / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.md").write_text("# Python")
        (rules_dir / "testing.md").write_text("# Testing")

        managed = {
            ".claude/rules/python.md",
            ".claude/rules/testing.md",
        }
        apm_package = Mock()
        result = self.integrator.sync_integration_claude(apm_package, self.project_root, managed_files=managed)

        assert result["files_removed"] == 2
        assert not (rules_dir / "python.md").exists()
        assert not (rules_dir / "testing.md").exists()

    def test_sync_preserves_unmanaged_files(self):
        rules_dir = self.project_root / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.md").write_text("# APM")
        (rules_dir / "my-custom.md").write_text("# User-authored")

        managed = {".claude/rules/python.md"}
        apm_package = Mock()
        result = self.integrator.sync_integration_claude(apm_package, self.project_root, managed_files=managed)

        assert result["files_removed"] == 1
        assert (rules_dir / "my-custom.md").exists()

    def test_sync_legacy_fallback_preserves_user_files(self):
        """Legacy fallback (managed_files=None) does NOT glob-delete .md files
        under .claude/rules/ because that would destroy user-authored rules."""
        rules_dir = self.project_root / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.md").write_text("# Python")
        (rules_dir / "testing.md").write_text("# Testing")

        apm_package = Mock()
        result = self.integrator.sync_integration_claude(apm_package, self.project_root, managed_files=None)

        assert result["files_removed"] == 0
        assert (rules_dir / "python.md").exists()
        assert (rules_dir / "testing.md").exists()

    def test_sync_handles_missing_rules_dir(self):
        apm_package = Mock()
        result = self.integrator.sync_integration_claude(apm_package, self.project_root)

        assert result["files_removed"] == 0
        assert result["errors"] == 0


