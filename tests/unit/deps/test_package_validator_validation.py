"""Comprehensive unit tests for ``apm_cli.deps.package_validator``.

Coverage tests targeting the PackageValidator class and all its
helper methods.  All filesystem I/O is either performed on real ``tmp_path``
fixtures (fast, fully controlled) or patched out when testing internal helpers
in isolation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.package_validator import PackageValidator
from apm_cli.models.apm_package import APMPackage, ValidationResult
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_apm_yml(directory: Path, *, name: str = "my-pkg", version: str = "1.0.0") -> Path:
    """Write a minimal valid apm.yml into *directory* and return its path."""
    apm_yml = directory / "apm.yml"
    apm_yml.write_text(f"name: {name}\nversion: {version}\n", encoding="utf-8")
    return apm_yml


def _make_primitive(apm_dir: Path, ptype: str, filename: str, content: str = "# content") -> Path:
    """Create a primitive file under *apm_dir*/<ptype>/<filename>."""
    pdir = apm_dir / ptype
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# PackageValidator.validate_package  (thin delegation wrapper)
# ---------------------------------------------------------------------------


class TestValidatePackage:
    """Tests for the thin ``validate_package`` public entry point."""

    def test_delegates_to_base_validate(self, tmp_path: Path) -> None:
        """validate_package should return a ValidationResult from the base helper."""
        _make_apm_yml(tmp_path)
        validator = PackageValidator()
        result = validator.validate_package(tmp_path)
        assert isinstance(result, ValidationResult)

    def test_returns_invalid_for_nonexistent_path(self, tmp_path: Path) -> None:
        """validate_package should mark the result invalid for a missing directory."""
        validator = PackageValidator()
        result = validator.validate_package(tmp_path / "does_not_exist")
        assert not result.is_valid

    def test_result_is_validation_result_instance(self, tmp_path: Path) -> None:
        """Return type should always be ValidationResult."""
        validator = PackageValidator()
        result = validator.validate_package(tmp_path)
        assert isinstance(result, ValidationResult)


# ---------------------------------------------------------------------------
# PackageValidator.validate_package_structure — path existence checks
# ---------------------------------------------------------------------------


class TestValidatePackageStructurePathChecks:
    """Tests for the early-exit path guards in validate_package_structure."""

    def test_error_when_path_does_not_exist(self, tmp_path: Path) -> None:
        missing = tmp_path / "ghost"
        validator = PackageValidator()
        result = validator.validate_package_structure(missing)
        assert not result.is_valid
        assert any("does not exist" in e for e in result.errors)

    def test_error_when_path_is_a_file(self, tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hello", encoding="utf-8")
        validator = PackageValidator()
        result = validator.validate_package_structure(f)
        assert not result.is_valid
        assert any("not a directory" in e for e in result.errors)

    def test_error_when_apm_yml_missing(self, tmp_path: Path) -> None:
        validator = PackageValidator()
        result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("apm.yml" in e for e in result.errors)

    def test_error_when_apm_yml_is_invalid_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(":::invalid:::", encoding="utf-8")
        validator = PackageValidator()
        result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("apm.yml" in e for e in result.errors)


# ---------------------------------------------------------------------------
# validate_package_structure — .apm directory checks for APM_PACKAGE type
# ---------------------------------------------------------------------------


class TestValidatePackageStructureApmDir:
    """Tests for .apm/ directory validation rules."""

    def test_error_when_apm_dir_missing_for_apm_package_type(self, tmp_path: Path) -> None:
        _make_apm_yml(tmp_path)
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any(".apm" in e for e in result.errors)

    def test_error_when_apm_dir_is_a_file(self, tmp_path: Path) -> None:
        _make_apm_yml(tmp_path)
        apm_file = tmp_path / ".apm"
        apm_file.write_text("not a dir", encoding="utf-8")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any(".apm must be a directory" in e for e in result.errors)

    def test_no_apm_dir_error_for_hybrid_type(self, tmp_path: Path) -> None:
        """HYBRID packages are allowed to ship without .apm/."""
        _make_apm_yml(tmp_path)
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.HYBRID, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        # No error about missing .apm/
        assert not any(".apm" in e for e in result.errors)

    def test_no_apm_dir_error_for_claude_skill_type(self, tmp_path: Path) -> None:
        """CLAUDE_SKILL packages are allowed to ship without .apm/."""
        _make_apm_yml(tmp_path)
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.CLAUDE_SKILL, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert not any(".apm" in e for e in result.errors)

    def test_error_when_invalid_package_type_and_no_apm_dir(self, tmp_path: Path) -> None:
        """INVALID type should still require .apm/ (same guard)."""
        _make_apm_yml(tmp_path)
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.INVALID, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid


# ---------------------------------------------------------------------------
# validate_package_structure — primitive content checks
# ---------------------------------------------------------------------------


class TestValidatePackageStructurePrimitives:
    """Tests for primitive detection and the no-primitives warning."""

    def _setup_apm_package(self, tmp_path: Path) -> Path:
        """Return an apm_dir ready for primitive population."""
        _make_apm_yml(tmp_path)
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        return apm_dir

    def test_warning_when_no_primitives(self, tmp_path: Path) -> None:
        self._setup_apm_package(tmp_path)
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert result.is_valid
        assert any("No primitive" in w for w in result.warnings)

    def test_valid_with_instruction_primitive(self, tmp_path: Path) -> None:
        apm_dir = self._setup_apm_package(tmp_path)
        _make_primitive(apm_dir, "instructions", "my-pkg.instructions.md")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert result.is_valid
        assert not any("No primitive" in w for w in result.warnings)

    def test_valid_with_chatmode_primitive(self, tmp_path: Path) -> None:
        apm_dir = self._setup_apm_package(tmp_path)
        _make_primitive(apm_dir, "agents", "my.agent.md")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_valid_with_hooks_in_apm_dir(self, tmp_path: Path) -> None:
        apm_dir = self._setup_apm_package(tmp_path)
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "on_push.json").write_text("{}", encoding="utf-8")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert result.is_valid
        assert not any("No primitive" in w for w in result.warnings)

    def test_valid_with_root_hooks_dir(self, tmp_path: Path) -> None:
        self._setup_apm_package(tmp_path)
        root_hooks = tmp_path / "hooks"
        root_hooks.mkdir()
        (root_hooks / "handler.json").write_text("{}", encoding="utf-8")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_warning_added_for_empty_primitive_file(self, tmp_path: Path) -> None:
        apm_dir = self._setup_apm_package(tmp_path)
        _make_primitive(apm_dir, "instructions", "my-pkg.instructions.md", content="   ")
        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
                mock_parse.return_value = MagicMock(spec=APMPackage)
                validator = PackageValidator()
                result = validator.validate_package_structure(tmp_path)
        assert any("Empty primitive" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# PackageValidator._validate_primitive_file
# ---------------------------------------------------------------------------


class TestValidatePrimitiveFile:
    """Tests for the private file-level validator."""

    def test_no_warning_for_non_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "tool.instructions.md"
        p.write_text("# Instructions\nDo something.\n", encoding="utf-8")
        result = ValidationResult()
        PackageValidator()._validate_primitive_file(p, result)
        assert not result.warnings

    def test_warning_for_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "tool.instructions.md"
        p.write_text("   \n\t  ", encoding="utf-8")
        result = ValidationResult()
        PackageValidator()._validate_primitive_file(p, result)
        assert any("Empty primitive" in w for w in result.warnings)

    def test_warning_when_file_read_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.instructions.md"
        p.write_text("x", encoding="utf-8")
        result = ValidationResult()
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            PackageValidator()._validate_primitive_file(p, result)
        assert any("Could not read" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# PackageValidator.validate_primitive_structure
# ---------------------------------------------------------------------------


class TestValidatePrimitiveStructure:
    """Tests for the .apm directory structure validator."""

    def test_error_when_apm_dir_missing(self, tmp_path: Path) -> None:
        issues = PackageValidator().validate_primitive_structure(tmp_path / ".apm")
        assert issues
        assert any("Missing .apm" in i for i in issues)

    def test_no_issues_with_valid_instructions_dir(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "instructions", "pkg.instructions.md")
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert not issues

    def test_warning_when_no_md_files_found(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert any("No primitive" in i for i in issues)

    def test_invalid_name_flagged(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "instructions", "bad name.md")  # has a space
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert any("Invalid primitive" in i for i in issues)

    def test_wrong_suffix_flagged(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "agents", "tool.instructions.md")  # wrong suffix for chatmode
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert any("Invalid primitive" in i for i in issues)

    def test_primitive_type_dir_that_is_a_file_flagged(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        fake_instructions = apm_dir / "instructions"
        fake_instructions.write_text("I am not a dir", encoding="utf-8")
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert any("should be a directory" in i for i in issues)

    def test_valid_context_file(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "contexts", "env.context.md")
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert not issues

    def test_valid_prompt_file(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "prompts", "gen.prompt.md")
        issues = PackageValidator().validate_primitive_structure(apm_dir)
        assert not issues


# ---------------------------------------------------------------------------
# PackageValidator._is_valid_primitive_name
# ---------------------------------------------------------------------------


class TestIsValidPrimitiveName:
    """Tests for the filename validation helper."""

    @pytest.mark.parametrize(
        "filename,ptype,expected",
        [
            ("tool.instructions.md", "instructions", True),
            ("my-tool.agent.md", "agents", True),
            ("config.context.md", "contexts", True),
            ("gen.prompt.md", "prompts", True),
            # Wrong suffixes
            ("tool.agent.md", "instructions", False),
            ("tool.instructions.md", "agents", False),
            # Space in name
            ("my tool.instructions.md", "instructions", False),
            # Doesn't end with .md
            ("tool.instructions.txt", "instructions", False),
            # Unknown primitive type — no suffix check; just needs .md + no spaces
            ("any-name.md", "unknown_type", True),
        ],
    )
    def test_validation_cases(self, filename: str, ptype: str, expected: bool) -> None:
        result = PackageValidator()._is_valid_primitive_name(filename, ptype)
        assert result is expected


# ---------------------------------------------------------------------------
# PackageValidator.get_package_info_summary
# ---------------------------------------------------------------------------


class TestGetPackageInfoSummary:
    """Tests for the human-readable package summary builder."""

    def _make_valid_result(
        self,
        name: str = "my-pkg",
        version: str = "1.0.0",
        description: str | None = None,
    ) -> ValidationResult:
        result = ValidationResult()
        result.package = SimpleNamespace(name=name, version=version, description=description)
        return result

    def test_returns_none_for_invalid_package(self, tmp_path: Path) -> None:
        validator = PackageValidator()
        with patch.object(validator, "validate_package") as mock_vp:
            mock_vp.return_value = ValidationResult()
            # Default ValidationResult has is_valid=True but package=None
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is None

    def test_returns_none_when_validation_fails(self, tmp_path: Path) -> None:
        validator = PackageValidator()
        bad_result = ValidationResult()
        bad_result.add_error("broken")
        with patch.object(validator, "validate_package", return_value=bad_result):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is None

    def test_summary_includes_name_and_version(self, tmp_path: Path) -> None:
        # Create an empty .apm dir so primitive_count is initialized in source
        (tmp_path / ".apm").mkdir()
        validator = PackageValidator()
        vr = self._make_valid_result()
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "my-pkg" in summary
        assert "1.0.0" in summary

    def test_summary_includes_description_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".apm").mkdir()
        validator = PackageValidator()
        vr = self._make_valid_result(description="A great tool")
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "A great tool" in summary

    def test_summary_counts_primitives(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        _make_primitive(apm_dir, "instructions", "a.instructions.md")
        _make_primitive(apm_dir, "instructions", "b.instructions.md")
        validator = PackageValidator()
        vr = self._make_valid_result()
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "2 primitives" in summary

    def test_summary_counts_hooks_in_apm_dir(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook1.json").write_text("{}", encoding="utf-8")
        validator = PackageValidator()
        vr = self._make_valid_result()
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "1 primitives" in summary

    def test_summary_counts_root_hooks_dir_when_no_apm_hooks(self, tmp_path: Path) -> None:
        # .apm exists but has no hooks/ subdir — root hooks/ should be counted
        (tmp_path / ".apm").mkdir()
        root_hooks = tmp_path / "hooks"
        root_hooks.mkdir()
        (root_hooks / "handler.json").write_text("{}", encoding="utf-8")
        validator = PackageValidator()
        vr = self._make_valid_result()
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "1 primitives" in summary

    def test_summary_no_primitives_does_not_append_count(self, tmp_path: Path) -> None:
        """When primitive_count == 0, no count suffix should appear."""
        (tmp_path / ".apm").mkdir()
        validator = PackageValidator()
        vr = self._make_valid_result()
        with patch.object(validator, "validate_package", return_value=vr):
            summary = validator.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "primitives" not in summary


# ---------------------------------------------------------------------------
# PackageValidator.validate_package_structure — from_apm_yml error handling
# ---------------------------------------------------------------------------


class TestValidatePackageStructureYmlErrors:
    """Tests for ValueError / FileNotFoundError paths in apm.yml parsing."""

    def test_error_on_value_error_in_parse(self, tmp_path: Path) -> None:
        _make_apm_yml(tmp_path)
        with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
            mock_parse.side_effect = ValueError("bad field")
            validator = PackageValidator()
            result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("Invalid apm.yml" in e for e in result.errors)

    def test_error_on_file_not_found_in_parse(self, tmp_path: Path) -> None:
        _make_apm_yml(tmp_path)
        with patch("apm_cli.deps.package_validator.APMPackage.from_apm_yml") as mock_parse:
            mock_parse.side_effect = FileNotFoundError("gone")
            validator = PackageValidator()
            result = validator.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("Invalid apm.yml" in e for e in result.errors)
