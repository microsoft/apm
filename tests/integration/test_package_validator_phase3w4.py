"""Integration tests for apm_cli.deps.package_validator -- phase3w4.

Targets lines/branches currently missing coverage in package_validator.py.
All tests are hermetic: filesystem operations use tmp_path, no network I/O.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from apm_cli.deps.package_validator import PackageValidator, stamp_plugin_version
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_pkg(root: Path, *, version: str = "1.0.0", description: str = "d") -> None:
    """Write a minimal valid apm.yml at *root*."""
    (root / "apm.yml").write_text(
        f"name: test-pkg\nversion: {version}\ndescription: {description}\n",
        encoding="utf-8",
    )


def _make_apm_dir(root: Path) -> Path:
    apm_dir = root / ".apm"
    apm_dir.mkdir(exist_ok=True)
    return apm_dir


def _add_primitive(apm_dir: Path, kind: str, name: str, content: str = "# content") -> Path:
    d = apm_dir / kind
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# validate_package (delegates to base_validate_apm_package)
# ---------------------------------------------------------------------------


class TestValidatePackage:
    def test_valid_package_returns_valid_result(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        _make_apm_dir(tmp_path)
        v = PackageValidator()
        result = v.validate_package(tmp_path)
        assert result.is_valid

    def test_missing_apm_yml_returns_invalid(self, tmp_path):
        # no apm.yml written
        v = PackageValidator()
        result = v.validate_package(tmp_path)
        assert not result.is_valid


# ---------------------------------------------------------------------------
# validate_package_structure
# ---------------------------------------------------------------------------


class TestValidatePackageStructure:
    def test_nonexistent_directory(self, tmp_path):
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path / "ghost")
        assert not result.is_valid
        assert any("does not exist" in e for e in result.errors)

    def test_path_is_not_a_directory(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        v = PackageValidator()
        result = v.validate_package_structure(f)
        assert not result.is_valid
        assert any("not a directory" in e for e in result.errors)

    def test_missing_apm_yml(self, tmp_path):
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("apm.yml" in e for e in result.errors)

    def test_invalid_apm_yml_content(self, tmp_path):
        (tmp_path / "apm.yml").write_text("{invalid: [yaml: content}", encoding="utf-8")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any("Invalid apm.yml" in e for e in result.errors)

    def test_apm_package_type_missing_apm_dir(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        # No .apm directory -- APM_PACKAGE type requires it
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any(".apm" in e for e in result.errors)

    def test_apm_dir_is_file_not_dir(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        (tmp_path / ".apm").write_text("not a directory")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert not result.is_valid
        assert any(".apm must be a directory" in e for e in result.errors)

    def test_apm_dir_exists_no_primitives_warns(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        _make_apm_dir(tmp_path)
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid  # no hard error
        assert any("No primitive files" in w for w in result.warnings)

    def test_with_instructions_primitive(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "instructions", "my.instructions.md")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid
        assert not result.warnings

    def test_with_chatmode_primitive(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "agents", "my.agent.md")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_with_context_primitive(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "contexts", "my.context.md")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_with_prompt_primitive(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "prompts", "my.prompt.md")
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_with_hooks_in_apm_dir(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "my-hook.json").write_text('{"type":"shell"}')
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid
        assert not result.warnings

    def test_with_root_hooks_dir(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        _make_apm_dir(tmp_path)
        # root hooks/ counts as primitives
        hooks_root = tmp_path / "hooks"
        hooks_root.mkdir()
        (hooks_root / "my-hook.json").write_text('{"type":"shell"}')
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid

    def test_empty_primitive_file_warns(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        inst_dir = apm_dir / "instructions"
        inst_dir.mkdir()
        (inst_dir / "empty.instructions.md").write_text("   \n  ")  # whitespace-only
        v = PackageValidator()
        result = v.validate_package_structure(tmp_path)
        assert result.is_valid  # empty file is a warning, not an error
        assert any("Empty primitive file" in w for w in result.warnings)

    def test_hybrid_package_type_no_apm_dir_ok(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        # Create a CLAUDE_SKILL or HYBRID signal (SKILL.md at root)
        (tmp_path / "SKILL.md").write_text("# Skill")
        v = PackageValidator()
        # Should not error about missing .apm/ for non-APM_PACKAGE types
        result = v.validate_package_structure(tmp_path)
        # HYBRID/CLAUDE_SKILL types are fine without .apm
        assert not any(".apm/" in e for e in result.errors)


# ---------------------------------------------------------------------------
# _validate_primitive_file
# ---------------------------------------------------------------------------


class TestValidatePrimitiveFile:
    def test_readable_nonempty_file_no_warning(self, tmp_path):
        f = tmp_path / "test.instructions.md"
        f.write_text("# Some content", encoding="utf-8")
        v = PackageValidator()
        from apm_cli.models.apm_package import ValidationResult

        result = ValidationResult()
        v._validate_primitive_file(f, result)
        assert not result.warnings

    def test_empty_file_adds_warning(self, tmp_path):
        f = tmp_path / "empty.instructions.md"
        f.write_text("", encoding="utf-8")
        v = PackageValidator()
        from apm_cli.models.apm_package import ValidationResult

        result = ValidationResult()
        v._validate_primitive_file(f, result)
        assert any("Empty primitive file" in w for w in result.warnings)

    def test_unreadable_file_adds_warning(self, tmp_path):
        f = tmp_path / "bad.instructions.md"
        f.write_text("content", encoding="utf-8")
        v = PackageValidator()
        from apm_cli.models.apm_package import ValidationResult

        result = ValidationResult()
        # Simulate read failure
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            v._validate_primitive_file(f, result)
        assert any("Could not read primitive file" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# validate_primitive_structure
# ---------------------------------------------------------------------------


class TestValidatePrimitiveStructure:
    def test_missing_apm_dir_returns_issue(self, tmp_path):
        v = PackageValidator()
        issues = v.validate_primitive_structure(tmp_path / ".apm")
        assert any("Missing .apm" in i for i in issues)

    def test_empty_apm_dir_reports_no_primitives(self, tmp_path):
        apm_dir = _make_apm_dir(tmp_path)
        v = PackageValidator()
        issues = v.validate_primitive_structure(apm_dir)
        assert any("No primitive files" in i for i in issues)

    def test_valid_instructions_file_no_issues(self, tmp_path):
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "instructions", "my.instructions.md")
        v = PackageValidator()
        issues = v.validate_primitive_structure(apm_dir)
        assert issues == []

    def test_invalid_filename_reported(self, tmp_path):
        apm_dir = _make_apm_dir(tmp_path)
        # filename missing expected suffix
        _add_primitive(apm_dir, "instructions", "bad-name.md")
        v = PackageValidator()
        issues = v.validate_primitive_structure(apm_dir)
        assert any("Invalid primitive file name" in i for i in issues)

    def test_primitive_type_as_file_not_dir(self, tmp_path):
        apm_dir = _make_apm_dir(tmp_path)
        # instructions is a file, not a directory
        (apm_dir / "instructions").write_text("oops")
        v = PackageValidator()
        issues = v.validate_primitive_structure(apm_dir)
        assert any("should be a directory" in i for i in issues)

    def test_all_primitive_types_detected(self, tmp_path):
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "agents", "my.agent.md")
        _add_primitive(apm_dir, "contexts", "my.context.md")
        _add_primitive(apm_dir, "prompts", "my.prompt.md")
        v = PackageValidator()
        issues = v.validate_primitive_structure(apm_dir)
        assert issues == []


# ---------------------------------------------------------------------------
# _is_valid_primitive_name
# ---------------------------------------------------------------------------


class TestIsValidPrimitiveName:
    def test_valid_instructions_name(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.instructions.md", "instructions") is True

    def test_valid_chatmode_name(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.agent.md", "agents") is True

    def test_valid_context_name(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.context.md", "contexts") is True

    def test_valid_prompt_name(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.prompt.md", "prompts") is True

    def test_name_without_md_extension(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.instructions", "instructions") is False

    def test_name_with_spaces(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my file.instructions.md", "instructions") is False

    def test_name_wrong_suffix(self):
        v = PackageValidator()
        assert v._is_valid_primitive_name("my.agent.md", "instructions") is False

    def test_name_unknown_primitive_type_passes(self):
        v = PackageValidator()
        # Unknown type has no expected suffix, so only basic checks apply
        assert v._is_valid_primitive_name("anything.md", "unknown_type") is True


# ---------------------------------------------------------------------------
# get_package_info_summary
# ---------------------------------------------------------------------------


class TestGetPackageInfoSummary:
    def test_invalid_package_returns_none(self, tmp_path):
        # No apm.yml -- invalid
        v = PackageValidator()
        assert v.get_package_info_summary(tmp_path) is None

    def test_valid_package_returns_name_version(self, tmp_path):
        _make_minimal_pkg(tmp_path, description="My tool")
        _make_apm_dir(tmp_path)
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        assert summary is not None
        assert "test-pkg" in summary
        assert "1.0.0" in summary
        assert "My tool" in summary

    def test_summary_without_description(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: bare-pkg\nversion: 2.0.0\n", encoding="utf-8")
        _make_apm_dir(tmp_path)
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        assert "bare-pkg" in summary
        assert "2.0.0" in summary

    def test_summary_with_primitives_count(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        _add_primitive(apm_dir, "instructions", "a.instructions.md")
        _add_primitive(apm_dir, "prompts", "b.prompt.md")
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        assert "(2 primitives)" in summary

    def test_summary_with_apm_hooks(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hook.json").write_text("{}")
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        assert "(1 primitives)" in summary

    def test_summary_root_hooks_no_double_count(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        _make_apm_dir(tmp_path)
        # root hooks/ without .apm/hooks/ -- should be counted
        root_hooks = tmp_path / "hooks"
        root_hooks.mkdir()
        (root_hooks / "hook.json").write_text("{}")
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        assert "(1 primitives)" in summary

    def test_summary_root_hooks_not_double_counted_when_apm_hooks_exists(self, tmp_path):
        _make_minimal_pkg(tmp_path)
        apm_dir = _make_apm_dir(tmp_path)
        hooks_apm = apm_dir / "hooks"
        hooks_apm.mkdir()
        (hooks_apm / "hook.json").write_text("{}")
        root_hooks = tmp_path / "hooks"
        root_hooks.mkdir()
        (root_hooks / "hook.json").write_text("{}")
        v = PackageValidator()
        summary = v.get_package_info_summary(tmp_path)
        # Should count 1 from .apm/hooks and NOT add root hooks (double-count guard)
        assert "(1 primitives)" in summary


# ---------------------------------------------------------------------------
# stamp_plugin_version
# ---------------------------------------------------------------------------


class TestStampPluginVersion:
    def test_none_package_is_noop(self, tmp_path):
        # Should not raise
        stamp_plugin_version(None, PackageType.MARKETPLACE_PLUGIN, "abc1234", tmp_path)

    def test_non_marketplace_plugin_type_is_noop(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="0.0.0")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.APM_PACKAGE, "abc1234def", tmp_path)
        # version should NOT change
        assert pkg.version == "0.0.0"

    def test_non_zero_version_is_noop(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="1.2.3")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, "abc1234def", tmp_path)
        assert pkg.version == "1.2.3"

    def test_unknown_commit_is_noop(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="0.0.0")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, "unknown", tmp_path)
        assert pkg.version == "0.0.0"

    def test_none_commit_is_noop(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="0.0.0")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, None, tmp_path)
        assert pkg.version == "0.0.0"

    def test_stamps_short_sha_on_package(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="0.0.0")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, "abc1234def567", tmp_path)
        assert pkg.version == "abc1234"  # first 7 chars

    def test_stamps_short_sha_in_apm_yml(self, tmp_path):
        _make_minimal_pkg(tmp_path, version="0.0.0")
        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, "deadbeef0", tmp_path)
        import yaml

        with open(tmp_path / "apm.yml") as f:
            data = yaml.safe_load(f)
        assert data["version"] == "deadbee"  # first 7 chars of "deadbeef0"

    def test_stamp_no_apm_yml_on_disk(self, tmp_path):
        # package obj but no apm.yml on disk -- should not raise
        pkg = APMPackage(name="test", version="0.0.0")
        stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, "abc1234def", tmp_path)
        # in-memory version stamped but disk write skipped
        assert pkg.version == "abc1234"
