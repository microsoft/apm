from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.constants import APM_DIR
from apm_cli.models.validation import (
    DetectionEvidence,
    PackageContentType,
    PackageType,
    ValidationResult,
    _apm_yml_declares_dependencies,
    _validate_apm_package_with_yml,
    _validate_claude_skill,
    _validate_hook_package,
    _validate_hybrid_package,
    _validate_marketplace_plugin,
    _validate_skill_bundle,
    detect_package_type,
    gather_detection_evidence,
    validate_apm_package,
)


@pytest.fixture(autouse=True)
def _force_yaml_pure_python(monkeypatch):
    """Force frontmatter to use PyYAML's pure-Python SafeLoader.

    ``frontmatter.default_handlers`` imports ``SafeLoader`` from
    ``yaml.cyaml`` (the C extension). Coverage's C tracer can corrupt the
    CSafeLoader internal state when multiple C-extension modules are
    simultaneously instrumented, causing YAML node tags to resolve to
    ``None`` and raising ConstructorError. Substituting the pure-Python
    ``yaml.SafeLoader`` avoids this without changing any production code.
    """
    import frontmatter.default_handlers as _fdh

    monkeypatch.setattr(_fdh, "SafeLoader", yaml.SafeLoader)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_apm_yml(path: Path, extra: str = "") -> Path:
    return _write(
        path / "apm.yml",
        "name: test-package\nversion: 1.0.0\ndescription: Test package\n" + extra,
    )


def _write_skill_md(
    path: Path, *, name: str = "test-skill", description: str = "Skill description"
) -> Path:
    return _write(
        path / "SKILL.md",
        f"---\nname: {name}\ndescription: {description}\n---\n# Skill\n",
    )


class TestPackageContentType:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("instructions", PackageContentType.INSTRUCTIONS),
            ("skill", PackageContentType.SKILL),
            ("hybrid", PackageContentType.HYBRID),
            ("prompts", PackageContentType.PROMPTS),
            (" SKILL ", PackageContentType.SKILL),
        ],
    )
    def test_from_string_accepts_valid_values(
        self, value: str, expected: PackageContentType
    ) -> None:
        assert PackageContentType.from_string(value) == expected

    def test_from_string_rejects_empty_value(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            PackageContentType.from_string("")

    def test_from_string_rejects_invalid_value(self) -> None:
        with pytest.raises(ValueError, match="Invalid package type 'unknown'"):
            PackageContentType.from_string("unknown")


class TestValidationResultHelpers:
    def test_add_error_marks_result_invalid(self) -> None:
        result = ValidationResult()
        result.add_error("boom")
        assert result.is_valid is False
        assert result.errors == ["boom"]

    def test_add_warning_preserves_validity(self) -> None:
        result = ValidationResult()
        result.add_warning("heads up")
        assert result.is_valid is True
        assert result.warnings == ["heads up"]

    def test_has_issues_false_when_clean(self) -> None:
        assert ValidationResult().has_issues() is False

    def test_has_issues_true_with_warning(self) -> None:
        result = ValidationResult()
        result.add_warning("warn")
        assert result.has_issues() is True

    def test_summary_for_clean_result(self) -> None:
        assert ValidationResult().summary() == "[+] Package is valid"

    def test_summary_for_valid_result_with_warning(self) -> None:
        result = ValidationResult()
        result.add_warning("warn")
        assert result.summary() == "[!] Package is valid with 1 warning(s)"

    def test_summary_for_invalid_result(self) -> None:
        result = ValidationResult()
        result.add_error("error")
        assert result.summary() == "[x] Package is invalid with 1 error(s)"


class TestDetectionEvidenceProperty:
    def test_has_plugin_evidence_false_without_manifest(self) -> None:
        evidence = DetectionEvidence(False, False, False, None, (), False, (), False)
        assert evidence.has_plugin_evidence is False

    def test_has_plugin_evidence_true_with_manifest(self) -> None:
        evidence = DetectionEvidence(False, False, False, None, (), True, (), True)
        assert evidence.has_plugin_evidence is True


class TestGatherDetectionEvidence:
    def test_gather_detection_evidence_empty_dir(self, tmp_path: Path) -> None:
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_apm_yml is False
        assert evidence.has_skill_md is False
        assert evidence.has_hook_json is False
        assert evidence.plugin_dirs_present == ()
        assert evidence.nested_skill_dirs == ()

    def test_gather_detection_evidence_detects_apm_skill_and_hooks(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write_skill_md(tmp_path)
        _write(tmp_path / "hooks" / "pre.json", "{}")
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_apm_yml is True
        assert evidence.has_skill_md is True
        assert evidence.has_hook_json is True

    def test_gather_detection_evidence_detects_plugin_directories_in_canonical_order(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "skills").mkdir()
        (tmp_path / "agents").mkdir()
        (tmp_path / "commands").mkdir()
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.plugin_dirs_present == ("agents", "skills", "commands")

    def test_gather_detection_evidence_detects_claude_plugin_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".claude-plugin").mkdir()
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.has_claude_plugin_dir is True
        assert evidence.has_plugin_manifest is True

    def test_gather_detection_evidence_detects_nested_skill_dirs(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills" / "alpha" / "SKILL.md", "# Alpha\n")
        _write(tmp_path / "skills" / "beta" / "SKILL.md", "# Beta\n")
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.nested_skill_dirs == ("alpha", "beta")

    def test_gather_detection_evidence_finds_plugin_json_via_helper(self, tmp_path: Path) -> None:
        plugin_json = _write(tmp_path / "plugin.json", "{}")
        evidence = gather_detection_evidence(tmp_path)
        assert evidence.plugin_json_path == plugin_json
        assert evidence.has_plugin_manifest is True


class TestDetectPackageType:
    def test_detect_marketplace_plugin(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude-plugin" / "plugin.json", "{}")
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_path == tmp_path / ".claude-plugin" / "plugin.json"

    def test_detect_hybrid(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write_skill_md(tmp_path)
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HYBRID
        assert plugin_path is None

    def test_detect_claude_skill(self, tmp_path: Path) -> None:
        _write_skill_md(tmp_path)
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.CLAUDE_SKILL
        assert plugin_path is None

    def test_detect_skill_bundle(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills" / "nested" / "SKILL.md", "# Nested\n")
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.SKILL_BUNDLE
        assert plugin_path is None

    def test_detect_apm_package_with_apm_dir(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        (tmp_path / APM_DIR).mkdir()
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE
        assert plugin_path is None

    def test_detect_apm_package_with_dependencies_only(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  apm:\n    - owner/repo\n")
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE
        assert plugin_path is None

    def test_detect_hook_package(self, tmp_path: Path) -> None:
        _write(tmp_path / "hooks" / "pre.json", "{}")
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HOOK_PACKAGE
        assert plugin_path is None

    def test_detect_invalid_with_apm_yml_but_no_apm_dir_or_dependencies(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID
        assert plugin_path is None

    def test_detect_invalid_without_any_signals(self, tmp_path: Path) -> None:
        pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID
        assert plugin_path is None


class TestDeclaredDependencies:
    def test_declares_dependencies_returns_false_on_yaml_parse_error(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", "name: [unterminated\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is False

    def test_declares_dependencies_returns_false_when_root_is_not_mapping(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "apm.yml", "- one\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is False

    def test_declares_dependencies_detects_apm_dependencies(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  apm:\n    - owner/repo\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is True

    def test_declares_dependencies_detects_mcp_dependencies(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  mcp:\n    - name: server\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is True

    def test_declares_dependencies_detects_dev_dependencies(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "devDependencies:\n  apm:\n    - owner/repo\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is True

    def test_declares_dependencies_returns_false_for_empty_lists(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  apm: []\n  mcp: []\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is False

    def test_declares_dependencies_returns_false_for_non_list_entries(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  apm: owner/repo\n")
        assert _apm_yml_declares_dependencies(tmp_path / "apm.yml") is False


class TestValidateApmPackage:
    def test_validate_apm_package_rejects_missing_directory(self, tmp_path: Path) -> None:
        result = validate_apm_package(tmp_path / "missing")
        assert result.is_valid is False
        assert "does not exist" in result.errors[0]

    def test_validate_apm_package_rejects_file_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "file.txt", "content")
        result = validate_apm_package(path)
        assert result.is_valid is False
        assert "not a directory" in result.errors[0]

    def test_validate_apm_package_reports_invalid_without_any_signals(self, tmp_path: Path) -> None:
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.INVALID
        assert any(
            "no apm.yml, SKILL.md, hooks, or plugin structure" in error for error in result.errors
        )

    def test_validate_apm_package_reports_invalid_when_apm_dir_is_file(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        _write(tmp_path / APM_DIR, "not a dir")
        result = validate_apm_package(tmp_path)
        assert result.is_valid is False
        assert result.errors == [".apm must be a directory"]

    def test_validate_apm_package_reports_invalid_when_apm_dir_missing(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        result = validate_apm_package(tmp_path)
        assert result.is_valid is False
        assert any("missing the required .apm/ directory" in error for error in result.errors)

    def test_validate_apm_package_dispatches_hook_package(self, tmp_path: Path) -> None:
        _write(tmp_path / "hooks" / "pre.json", "{}")
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.HOOK_PACKAGE
        assert result.package is not None

    def test_validate_apm_package_dispatches_claude_skill(self, tmp_path: Path) -> None:
        _write_skill_md(tmp_path, name="skill-name")
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.CLAUDE_SKILL
        assert result.package is not None
        assert result.package.name == "skill-name"

    def test_validate_apm_package_dispatches_skill_bundle(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md",
            "---\nname: alpha\ndescription: Alpha\n---\n",
        )
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.SKILL_BUNDLE
        assert result.package is not None

    def test_validate_apm_package_dispatches_hybrid(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write_skill_md(tmp_path)
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.HYBRID
        assert result.package is not None

    def test_validate_apm_package_dispatches_marketplace_plugin(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude-plugin" / "plugin.json", '{"name": "plugin-name"}')
        result = validate_apm_package(tmp_path)
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package is not None


class TestDirectValidators:
    def test_validate_hook_package_creates_package(self, tmp_path: Path) -> None:
        result = _validate_hook_package(tmp_path, ValidationResult())
        assert result.package is not None
        assert result.package.name == tmp_path.name
        assert result.package.type == PackageContentType.HYBRID

    def test_validate_claude_skill_reads_frontmatter(self, tmp_path: Path) -> None:
        skill_md = _write_skill_md(
            tmp_path, name="frontmatter-name", description="Frontmatter description"
        )
        result = _validate_claude_skill(tmp_path, skill_md, ValidationResult())
        assert result.package is not None
        assert result.package.name == "frontmatter-name"
        assert result.package.description == "Frontmatter description"
        assert result.package.type == PackageContentType.SKILL

    def test_validate_claude_skill_uses_directory_name_when_name_missing(
        self, tmp_path: Path
    ) -> None:
        skill_md = _write(
            tmp_path / "SKILL.md",
            "---\ndescription: Only description\n---\n# Skill\n",
        )
        result = _validate_claude_skill(tmp_path, skill_md, ValidationResult())
        assert result.package is not None
        assert result.package.name == tmp_path.name

    def test_validate_claude_skill_surfaces_exception(self, tmp_path: Path) -> None:
        skill_md = _write_skill_md(tmp_path)
        with patch("frontmatter.load", side_effect=ValueError("broken frontmatter")):
            result = _validate_claude_skill(tmp_path, skill_md, ValidationResult())
        assert result.is_valid is False
        assert "broken frontmatter" in result.errors[0]

    def test_validate_skill_bundle_requires_nested_skills(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.is_valid is False
        assert "no valid skills/<name>/SKILL.md" in result.errors[0]

    def test_validate_skill_bundle_rejects_path_traversal_name(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: desc\n---\n"
        )

        def fake_validate(path_str: str, *, context: str, **kwargs) -> None:
            if context == "skills/alpha":
                raise ValueError("Invalid skills/alpha '../': traversal sequence")

        with patch("apm_cli.utils.path_security.validate_path_segments", side_effect=fake_validate):
            result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.is_valid is False
        assert any("traversal sequence" in error for error in result.errors)

    def test_validate_skill_bundle_surfaces_ensure_path_within_error(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: desc\n---\n"
        )
        with patch(
            "apm_cli.utils.path_security.ensure_path_within", side_effect=ValueError("outside base")
        ):
            result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.is_valid is False
        assert "outside base" in result.errors[0]

    def test_validate_skill_bundle_surfaces_frontmatter_parse_error(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\n")
        with patch("frontmatter.load", side_effect=ValueError("bad frontmatter")):
            result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.is_valid is False
        assert "failed to parse frontmatter" in result.errors[0]

    def test_validate_skill_bundle_warns_on_name_mismatch_missing_description_and_non_ascii(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md",
            "---\nname: beta\ndescription: caf\u00e9\n---\n# Skill\n",
        )
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.package is not None
        assert any(
            "does not match directory name 'alpha'" in warning for warning in result.warnings
        )
        assert any("contains non-ASCII characters" in warning for warning in result.warnings)

    def test_validate_skill_bundle_warns_when_description_missing(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n# Skill\n")
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert any("missing 'description'" in warning for warning in result.warnings)

    def test_validate_skill_bundle_uses_apm_yml_when_present(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: desc\n---\n"
        )
        _write_apm_yml(tmp_path, "license: MIT\n")
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.package is not None
        assert result.package.name == "test-package"
        assert result.package.license == "MIT"

    def test_validate_skill_bundle_rejects_invalid_apm_yml(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: desc\n---\n"
        )
        _write(tmp_path / "apm.yml", "version: 1.0.0\n")
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.is_valid is False
        assert "Invalid apm.yml" in result.errors[0]

    def test_validate_skill_bundle_synthesises_package_without_apm_yml(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: desc\n---\n"
        )
        result = _validate_skill_bundle(tmp_path, ValidationResult())
        assert result.package is not None
        assert result.package.version == "0.0.0"
        assert result.package.type == PackageContentType.SKILL

    def test_validate_hybrid_with_apm_dir_uses_standard_validator(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write_skill_md(tmp_path)
        (tmp_path / APM_DIR).mkdir()
        sentinel = ValidationResult()
        with patch(
            "apm_cli.models.validation._validate_apm_package_with_yml",
            return_value=sentinel,
        ) as mock_validator:
            result = _validate_hybrid_package(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result is sentinel
        mock_validator.assert_called_once()

    def test_validate_hybrid_rejects_invalid_apm_yml(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", "version: 1.0.0\n")
        _write_skill_md(tmp_path)
        result = _validate_hybrid_package(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is False
        assert "Invalid apm.yml" in result.errors[0]

    def test_validate_hybrid_requires_skill_md(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        result = _validate_hybrid_package(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is False
        assert "missing SKILL.md" in result.errors[0]

    def test_validate_hybrid_warns_when_frontmatter_cannot_be_parsed(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        skill_md = _write_skill_md(tmp_path)
        with patch("frontmatter.load", side_effect=ValueError("bad frontmatter")):
            result = _validate_hybrid_package(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.package is not None
        assert any(str(skill_md.name) in warning for warning in result.warnings)

    def test_validate_hybrid_succeeds_without_apm_dir(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write_skill_md(tmp_path)
        result = _validate_hybrid_package(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.package is not None
        assert result.package.name == "test-package"

    def test_validate_apm_package_with_yml_rejects_invalid_apm_yml(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", "version: 1.0.0\n")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is False
        assert "Invalid apm.yml" in result.errors[0]

    def test_validate_apm_package_with_yml_accepts_dependency_only_package(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path, "dependencies:\n  apm:\n    - owner/repo\n")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is True
        assert result.errors == []

    def test_validate_apm_package_with_yml_rejects_missing_apm_dir_without_dependencies(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is False
        assert "Missing required directory" in result.errors[0]

    def test_validate_apm_package_with_yml_rejects_apm_dir_file(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        _write(tmp_path / APM_DIR, "file")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert result.is_valid is False
        assert result.errors == [".apm must be a directory"]

    def test_validate_apm_package_with_yml_warns_when_no_primitives_exist(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        (tmp_path / APM_DIR).mkdir()
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert any("No primitive files found" in warning for warning in result.warnings)

    def test_validate_apm_package_with_yml_warns_on_empty_primitive_file(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        _write(tmp_path / APM_DIR / "instructions" / "empty.md", "   ")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert any("Empty primitive file" in warning for warning in result.warnings)

    def test_validate_apm_package_with_yml_warns_when_primitive_file_cannot_be_read(
        self, tmp_path: Path
    ) -> None:
        _write_apm_yml(tmp_path)
        primitive = _write(tmp_path / APM_DIR / "instructions" / "readme.md", "content")
        original_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs):
            if self == primitive:
                raise OSError("denied")
            return original_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", fake_read_text):
            result = _validate_apm_package_with_yml(
                tmp_path, tmp_path / "apm.yml", ValidationResult()
            )
        assert any("Could not read primitive file" in warning for warning in result.warnings)

    def test_validate_apm_package_with_yml_uses_hooks_as_primitives(self, tmp_path: Path) -> None:
        _write_apm_yml(tmp_path)
        (tmp_path / APM_DIR).mkdir()
        _write(tmp_path / "hooks" / "pre.json", "{}")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert all("No primitive files found" not in warning for warning in result.warnings)

    def test_validate_apm_package_with_yml_warns_on_non_semver_version(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "apm.yml",
            "name: test-package\nversion: release-candidate\ndescription: desc\n",
        )
        _write(tmp_path / APM_DIR / "instructions" / "guide.md", "# Guide\n")
        result = _validate_apm_package_with_yml(tmp_path, tmp_path / "apm.yml", ValidationResult())
        assert any("doesn't follow semantic versioning" in warning for warning in result.warnings)

    def test_validate_marketplace_plugin_success(self, tmp_path: Path) -> None:
        plugin_json = _write(tmp_path / ".claude-plugin" / "plugin.json", '{"name": "plugin-name"}')
        result = _validate_marketplace_plugin(tmp_path, plugin_json, ValidationResult())
        assert result.package is not None
        assert result.package.name == "plugin-name"
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN

    def test_validate_marketplace_plugin_surfaces_exception(self, tmp_path: Path) -> None:
        logger = MagicMock()
        logger.warning("plugin failure observed")
        assert logger.warning.called
        with patch(
            "apm_cli.deps.plugin_parser.normalize_plugin_directory",
            side_effect=ValueError("boom"),
        ):
            result = _validate_marketplace_plugin(tmp_path, None, ValidationResult())
        assert result.is_valid is False
        assert "Failed to process Claude plugin: boom" in result.errors[0]
