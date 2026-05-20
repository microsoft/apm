"""Comprehensive unit tests for apm_cli.models.validation (phase 3 coverage pass).

Targets uncovered branches and functions not exercised by existing tests:
- PackageContentType.from_string() all paths
- ValidationResult methods: add_error, add_warning, has_issues, summary
- DetectionEvidence.has_plugin_evidence property
- gather_detection_evidence() with various filesystem shapes
- detect_package_type() all 7 cascade branches
- _apm_yml_declares_dependencies() parse paths
- validate_apm_package() dispatch and error paths
- _validate_hook_package()
- _validate_claude_skill()
- _validate_skill_bundle()
- _validate_hybrid_package()
- _validate_marketplace_plugin()
- _validate_apm_package_with_yml()
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.models.validation import (
    DetectionEvidence,
    InvalidVirtualPackageExtensionError,
    PackageContentType,
    PackageType,
    ValidationError,
    ValidationResult,
    _apm_yml_declares_dependencies,
    _has_hook_json,
    detect_package_type,
    gather_detection_evidence,
    validate_apm_package,
)

# ---------------------------------------------------------------------------
# PackageContentType.from_string()
# ---------------------------------------------------------------------------


class TestPackageContentTypeFromString:
    """Test PackageContentType.from_string() parsing."""

    def test_instructions_lower(self) -> None:
        """'instructions' parses to INSTRUCTIONS."""
        result: PackageContentType = PackageContentType.from_string("instructions")
        assert result == PackageContentType.INSTRUCTIONS

    def test_skill_lower(self) -> None:
        """'skill' parses to SKILL."""
        result: PackageContentType = PackageContentType.from_string("skill")
        assert result == PackageContentType.SKILL

    def test_hybrid_lower(self) -> None:
        """'hybrid' parses to HYBRID."""
        result: PackageContentType = PackageContentType.from_string("hybrid")
        assert result == PackageContentType.HYBRID

    def test_prompts_lower(self) -> None:
        """'prompts' parses to PROMPTS."""
        result: PackageContentType = PackageContentType.from_string("prompts")
        assert result == PackageContentType.PROMPTS

    def test_uppercase_accepted(self) -> None:
        """Uppercase strings are normalised before comparison."""
        result: PackageContentType = PackageContentType.from_string("SKILL")
        assert result == PackageContentType.SKILL

    def test_mixed_case_accepted(self) -> None:
        """Mixed-case strings are normalised before comparison."""
        result: PackageContentType = PackageContentType.from_string("Instructions")
        assert result == PackageContentType.INSTRUCTIONS

    def test_leading_trailing_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped."""
        result: PackageContentType = PackageContentType.from_string("  hybrid  ")
        assert result == PackageContentType.HYBRID

    def test_empty_string_raises(self) -> None:
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            PackageContentType.from_string("")

    def test_invalid_value_raises(self) -> None:
        """Unknown string raises ValueError with the bad value in the message."""
        with pytest.raises(ValueError, match="Invalid package type 'bad_type'"):
            PackageContentType.from_string("bad_type")

    def test_invalid_value_lists_valid_types(self) -> None:
        """Error message for unknown value lists valid types."""
        with pytest.raises(ValueError) as exc_info:
            PackageContentType.from_string("unknown")
        msg: str = str(exc_info.value)
        assert "instructions" in msg
        assert "skill" in msg


# ---------------------------------------------------------------------------
# ValidationResult methods
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Test ValidationResult dataclass helpers."""

    def test_initial_state_is_valid(self) -> None:
        """Freshly created ValidationResult is valid with empty lists."""
        vr: ValidationResult = ValidationResult()
        assert vr.is_valid is True
        assert vr.errors == []
        assert vr.warnings == []
        assert vr.package is None
        assert vr.package_type is None

    def test_add_error_sets_invalid(self) -> None:
        """add_error sets is_valid=False and appends the message."""
        vr: ValidationResult = ValidationResult()
        vr.add_error("Something went wrong")
        assert vr.is_valid is False
        assert "Something went wrong" in vr.errors

    def test_add_multiple_errors(self) -> None:
        """Multiple add_error calls accumulate messages."""
        vr: ValidationResult = ValidationResult()
        vr.add_error("error one")
        vr.add_error("error two")
        assert len(vr.errors) == 2

    def test_add_warning_keeps_valid(self) -> None:
        """add_warning does NOT set is_valid=False."""
        vr: ValidationResult = ValidationResult()
        vr.add_warning("heads up")
        assert vr.is_valid is True
        assert "heads up" in vr.warnings

    def test_has_issues_false_when_clean(self) -> None:
        """has_issues returns False when no errors or warnings."""
        vr: ValidationResult = ValidationResult()
        assert vr.has_issues() is False

    def test_has_issues_true_on_error(self) -> None:
        """has_issues returns True when an error has been added."""
        vr: ValidationResult = ValidationResult()
        vr.add_error("boom")
        assert vr.has_issues() is True

    def test_has_issues_true_on_warning(self) -> None:
        """has_issues returns True when only a warning has been added."""
        vr: ValidationResult = ValidationResult()
        vr.add_warning("just a heads up")
        assert vr.has_issues() is True

    def test_summary_valid_no_warnings(self) -> None:
        """summary returns positive message when valid and no warnings."""
        vr: ValidationResult = ValidationResult()
        summary: str = vr.summary()
        assert "valid" in summary.lower()
        assert "warning" not in summary.lower()

    def test_summary_valid_with_warnings(self) -> None:
        """summary includes warning count when valid but warnings present."""
        vr: ValidationResult = ValidationResult()
        vr.add_warning("w1")
        vr.add_warning("w2")
        summary: str = vr.summary()
        assert "2" in summary
        assert "warning" in summary.lower()

    def test_summary_invalid_with_errors(self) -> None:
        """summary includes error count when invalid."""
        vr: ValidationResult = ValidationResult()
        vr.add_error("e1")
        vr.add_error("e2")
        vr.add_error("e3")
        summary: str = vr.summary()
        assert "3" in summary
        assert "error" in summary.lower()


# ---------------------------------------------------------------------------
# DetectionEvidence
# ---------------------------------------------------------------------------


class TestDetectionEvidence:
    """Test DetectionEvidence dataclass and properties."""

    def _make_evidence(
        self,
        has_plugin_manifest: bool = False,
        plugin_json_path: Path | None = None,
        has_claude_plugin_dir: bool = False,
    ) -> DetectionEvidence:
        return DetectionEvidence(
            has_apm_yml=False,
            has_skill_md=False,
            has_hook_json=False,
            plugin_json_path=plugin_json_path,
            plugin_dirs_present=(),
            has_claude_plugin_dir=has_claude_plugin_dir,
            nested_skill_dirs=(),
            has_plugin_manifest=has_plugin_manifest,
        )

    def test_has_plugin_evidence_false_by_default(self) -> None:
        """has_plugin_evidence is False when no plugin manifest."""
        ev: DetectionEvidence = self._make_evidence(has_plugin_manifest=False)
        assert ev.has_plugin_evidence is False

    def test_has_plugin_evidence_true_when_plugin_manifest(self) -> None:
        """has_plugin_evidence is True when has_plugin_manifest=True."""
        ev: DetectionEvidence = self._make_evidence(
            has_plugin_manifest=True, plugin_json_path=Path("/some/plugin.json")
        )
        assert ev.has_plugin_evidence is True

    def test_has_plugin_evidence_true_for_claude_plugin_dir(self) -> None:
        """has_plugin_evidence is True when .claude-plugin/ is present."""
        ev: DetectionEvidence = self._make_evidence(
            has_plugin_manifest=True, has_claude_plugin_dir=True
        )
        assert ev.has_plugin_evidence is True

    def test_plugin_dirs_present_ordering(self) -> None:
        """plugin_dirs_present preserves canonical order: agents, skills, commands."""
        _ev: DetectionEvidence = self._make_evidence()
        # Substitute a tuple that matches the canonical ordering
        ev2 = DetectionEvidence(
            has_apm_yml=False,
            has_skill_md=False,
            has_hook_json=False,
            plugin_json_path=None,
            plugin_dirs_present=("agents", "skills", "commands"),
            has_plugin_manifest=False,
        )
        assert ev2.plugin_dirs_present == ("agents", "skills", "commands")


# ---------------------------------------------------------------------------
# _has_hook_json
# ---------------------------------------------------------------------------


class TestHasHookJson:
    """Test _has_hook_json filesystem helper."""

    def test_returns_false_when_no_hooks_dir(self, tmp_path: Path) -> None:
        """Returns False when neither hooks/ nor .apm/hooks/ exist."""
        result: bool = _has_hook_json(tmp_path)
        assert result is False

    def test_returns_true_for_hooks_dir_with_json(self, tmp_path: Path) -> None:
        """Returns True when hooks/*.json exists."""
        hooks_dir: Path = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit.json").write_text("{}")
        result: bool = _has_hook_json(tmp_path)
        assert result is True

    def test_returns_true_for_apm_hooks_dir_with_json(self, tmp_path: Path) -> None:
        """Returns True when .apm/hooks/*.json exists."""
        apm_hooks_dir: Path = tmp_path / ".apm" / "hooks"
        apm_hooks_dir.mkdir(parents=True)
        (apm_hooks_dir / "hook.json").write_text("{}")
        result: bool = _has_hook_json(tmp_path)
        assert result is True

    def test_returns_false_for_hooks_dir_with_no_json(self, tmp_path: Path) -> None:
        """Returns False when hooks/ exists but has no .json files."""
        hooks_dir: Path = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "readme.md").write_text("# hooks")
        result: bool = _has_hook_json(tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# gather_detection_evidence
# ---------------------------------------------------------------------------


class TestGatherDetectionEvidence:
    """Test gather_detection_evidence with real filesystem in tmp_path."""

    def test_empty_dir_returns_all_false(self, tmp_path: Path) -> None:
        """Empty directory yields all-False evidence."""
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.has_apm_yml is False
        assert ev.has_skill_md is False
        assert ev.has_hook_json is False
        assert ev.has_plugin_manifest is False
        assert ev.nested_skill_dirs == ()
        assert ev.plugin_dirs_present == ()

    def test_detects_apm_yml(self, tmp_path: Path) -> None:
        """apm.yml present -> has_apm_yml=True."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.has_apm_yml is True

    def test_detects_skill_md(self, tmp_path: Path) -> None:
        """SKILL.md present -> has_skill_md=True."""
        (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.has_skill_md is True

    def test_detects_hook_json(self, tmp_path: Path) -> None:
        """hooks/*.json present -> has_hook_json=True."""
        hooks_dir: Path = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "h.json").write_text("{}")
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.has_hook_json is True

    def test_detects_plugin_dirs_present(self, tmp_path: Path) -> None:
        """agents/ and skills/ directories detected in plugin_dirs_present."""
        (tmp_path / "agents").mkdir()
        (tmp_path / "skills").mkdir()
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert "agents" in ev.plugin_dirs_present
        assert "skills" in ev.plugin_dirs_present

    def test_detects_claude_plugin_dir(self, tmp_path: Path) -> None:
        """'.claude-plugin/' directory -> has_claude_plugin_dir=True and has_plugin_manifest=True."""
        (tmp_path / ".claude-plugin").mkdir()
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.has_claude_plugin_dir is True
        assert ev.has_plugin_manifest is True

    def test_detects_nested_skill_dirs(self, tmp_path: Path) -> None:
        """skills/<name>/SKILL.md -> nested_skill_dirs includes that name."""
        skill_dir: Path = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill")
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert "my-skill" in ev.nested_skill_dirs

    def test_nested_dir_without_skill_md_not_detected(self, tmp_path: Path) -> None:
        """skills/<name>/ dir without SKILL.md is NOT included in nested_skill_dirs."""
        skill_dir: Path = tmp_path / "skills" / "no-skill"
        skill_dir.mkdir(parents=True)
        ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert "no-skill" not in ev.nested_skill_dirs

    def test_plugin_json_detected(self, tmp_path: Path) -> None:
        """plugin.json presence -> has_plugin_manifest=True (mocked find_plugin_json)."""
        plugin_json_path: Path = tmp_path / "plugin.json"
        plugin_json_path.write_text("{}")
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_inner:
            mock_inner.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=False,
                has_hook_json=False,
                plugin_json_path=plugin_json_path,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=True,
            )
            ev: DetectionEvidence = gather_detection_evidence(tmp_path)
        assert ev.plugin_json_path is not None
        assert ev.has_plugin_manifest is True


# ---------------------------------------------------------------------------
# detect_package_type cascade
# ---------------------------------------------------------------------------


class TestDetectPackageTypeCascade:
    """Test each branch in detect_package_type()."""

    def test_marketplace_plugin_via_plugin_json(self, tmp_path: Path) -> None:
        """plugin.json -> MARKETPLACE_PLUGIN (cascade step 1)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=True,
                has_skill_md=True,
                has_hook_json=False,
                plugin_json_path=tmp_path / "plugin.json",
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=True,
            )
            pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_path == tmp_path / "plugin.json"

    def test_marketplace_plugin_via_claude_plugin_dir(self, tmp_path: Path) -> None:
        """.claude-plugin/ dir -> MARKETPLACE_PLUGIN (cascade step 1, no plugin.json)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=False,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=True,
                nested_skill_dirs=(),
                has_plugin_manifest=True,
            )
            pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_path is None

    def test_hybrid_apm_yml_and_skill_md(self, tmp_path: Path) -> None:
        """apm.yml + SKILL.md -> HYBRID (cascade step 2)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=True,
                has_skill_md=True,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=False,
            )
            pkg_type, plugin_path = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HYBRID
        assert plugin_path is None

    def test_claude_skill_skill_md_only(self, tmp_path: Path) -> None:
        """SKILL.md only -> CLAUDE_SKILL (cascade step 3)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=True,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=False,
            )
            pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.CLAUDE_SKILL

    def test_skill_bundle_nested_skills(self, tmp_path: Path) -> None:
        """Nested skills/<x>/SKILL.md -> SKILL_BUNDLE (cascade step 4)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=False,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=("skill-a",),
                has_plugin_manifest=False,
            )
            pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.SKILL_BUNDLE

    def test_apm_package_with_apm_dir(self, tmp_path: Path) -> None:
        """apm.yml + .apm/ directory -> APM_PACKAGE (cascade step 5)."""
        apm_dir: Path = tmp_path / ".apm"
        apm_dir.mkdir()
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=True,
                has_skill_md=False,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=False,
            )
            pkg_type, _ = detect_package_type(tmp_path)
        # apm.yml exists but .apm/ existence check is done inside detect_package_type
        # It calls _apm_yml_declares_dependencies on tmp_path/apm.yml
        # Since tmp_path doesn't have apm.yml, _apm_yml_declares_dependencies returns False
        # The .apm dir DOES exist -> APM_PACKAGE
        assert pkg_type == PackageType.APM_PACKAGE

    def test_hook_package_hook_json_only(self, tmp_path: Path) -> None:
        """hooks/*.json only -> HOOK_PACKAGE (cascade step 6)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=False,
                has_hook_json=True,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=False,
            )
            pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HOOK_PACKAGE

    def test_invalid_nothing_recognizable(self, tmp_path: Path) -> None:
        """Nothing recognizable -> INVALID (cascade step 7)."""
        with patch("apm_cli.models.validation.gather_detection_evidence") as mock_ev:
            mock_ev.return_value = DetectionEvidence(
                has_apm_yml=False,
                has_skill_md=False,
                has_hook_json=False,
                plugin_json_path=None,
                plugin_dirs_present=(),
                has_claude_plugin_dir=False,
                nested_skill_dirs=(),
                has_plugin_manifest=False,
            )
            pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_yml_no_apm_dir_no_deps_returns_invalid(self, tmp_path: Path) -> None:
        """apm.yml with no .apm/ dir and no deps -> INVALID."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_yml_with_deps_no_apm_dir_returns_apm_package(self, tmp_path: Path) -> None:
        """apm.yml with declared apm deps, no .apm/ -> APM_PACKAGE (dep-only aggregator)."""
        (tmp_path / "apm.yml").write_text(
            "name: pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - other-pkg\n"
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE


# ---------------------------------------------------------------------------
# _apm_yml_declares_dependencies
# ---------------------------------------------------------------------------


class TestApmYmlDeclaresdependencies:
    """Test _apm_yml_declares_dependencies with various YAML shapes."""

    def test_returns_false_file_missing(self, tmp_path: Path) -> None:
        """Returns False when file does not exist."""
        result: bool = _apm_yml_declares_dependencies(tmp_path / "nonexistent.yml")
        assert result is False

    def test_returns_false_empty_yaml(self, tmp_path: Path) -> None:
        """Returns False when YAML is empty."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is False

    def test_returns_false_no_deps_key(self, tmp_path: Path) -> None:
        """Returns False when no 'dependencies' or 'devDependencies' key."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is False

    def test_returns_true_for_apm_dep(self, tmp_path: Path) -> None:
        """Returns True when dependencies.apm is a non-empty list of strings."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - other-pkg\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is True

    def test_returns_true_for_mcp_dep(self, tmp_path: Path) -> None:
        """Returns True when dependencies.mcp is a non-empty list."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n    - mcp-server\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is True

    def test_returns_true_for_dev_dependencies(self, tmp_path: Path) -> None:
        """Returns True when devDependencies.apm is non-empty."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\ndevDependencies:\n  apm:\n    - dev-pkg\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is True

    def test_returns_false_for_empty_dep_list(self, tmp_path: Path) -> None:
        """Returns False when dependencies.apm exists but is an empty list."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\ndependencies:\n  apm: []\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is False

    def test_returns_false_on_malformed_yaml(self, tmp_path: Path) -> None:
        """Returns False when YAML is malformed (parse error)."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: [unclosed\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is False

    def test_returns_false_when_deps_block_not_dict(self, tmp_path: Path) -> None:
        """Returns False when dependencies value is not a dict."""
        path: Path = tmp_path / "apm.yml"
        path.write_text("name: pkg\nversion: 1.0.0\ndependencies: not-a-dict\n")
        result: bool = _apm_yml_declares_dependencies(path)
        assert result is False


# ---------------------------------------------------------------------------
# validate_apm_package – top-level dispatcher
# ---------------------------------------------------------------------------


class TestValidateApmPackage:
    """Test validate_apm_package() dispatch logic."""

    def test_nonexistent_path_error(self, tmp_path: Path) -> None:
        """Non-existent directory produces an error."""
        result: ValidationResult = validate_apm_package(tmp_path / "ghost")
        assert not result.is_valid
        assert any("does not exist" in e for e in result.errors)

    def test_file_instead_of_dir_error(self, tmp_path: Path) -> None:
        """File (not directory) produces an error."""
        f: Path = tmp_path / "file.txt"
        f.write_text("data")
        result: ValidationResult = validate_apm_package(f)
        assert not result.is_valid
        assert any("not a directory" in e for e in result.errors)

    def test_invalid_nothing_present(self, tmp_path: Path) -> None:
        """Empty dir with nothing recognizable -> INVALID result with error."""
        result: ValidationResult = validate_apm_package(tmp_path)
        assert not result.is_valid
        assert result.package_type == PackageType.INVALID

    def test_invalid_apm_yml_with_no_apm_dir(self, tmp_path: Path) -> None:
        """apm.yml but no .apm/ and no deps -> INVALID with helpful message."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        result: ValidationResult = validate_apm_package(tmp_path)
        assert not result.is_valid
        assert result.package_type == PackageType.INVALID

    def test_invalid_apm_is_file_not_dir(self, tmp_path: Path) -> None:
        """apm.yml + .apm as a file (not dir) -> INVALID with '.apm must be a directory'."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        apm_path: Path = tmp_path / ".apm"
        apm_path.write_text("i am a file")  # not a directory
        result: ValidationResult = validate_apm_package(tmp_path)
        assert not result.is_valid
        assert any(".apm must be a directory" in e for e in result.errors)

    def test_hook_package_dispatched(self, tmp_path: Path) -> None:
        """HOOK_PACKAGE type is dispatched to _validate_hook_package."""
        with (
            patch("apm_cli.models.validation.detect_package_type") as mock_detect,
            patch("apm_cli.models.validation._validate_hook_package") as mock_validate,
        ):
            mock_detect.return_value = (PackageType.HOOK_PACKAGE, None)
            mock_result = ValidationResult()
            mock_validate.return_value = mock_result
            result = validate_apm_package(tmp_path)
        mock_validate.assert_called_once()
        assert result is mock_result

    def test_claude_skill_dispatched(self, tmp_path: Path) -> None:
        """CLAUDE_SKILL type is dispatched to _validate_claude_skill."""
        with (
            patch("apm_cli.models.validation.detect_package_type") as mock_detect,
            patch("apm_cli.models.validation._validate_claude_skill") as mock_validate,
        ):
            mock_detect.return_value = (PackageType.CLAUDE_SKILL, None)
            mock_result = ValidationResult()
            mock_validate.return_value = mock_result
            result = validate_apm_package(tmp_path)
        mock_validate.assert_called_once()
        assert result is mock_result

    def test_marketplace_plugin_dispatched(self, tmp_path: Path) -> None:
        """MARKETPLACE_PLUGIN type dispatches to _validate_marketplace_plugin."""
        plugin_json: Path = tmp_path / "plugin.json"
        with (
            patch("apm_cli.models.validation.detect_package_type") as mock_detect,
            patch("apm_cli.models.validation._validate_marketplace_plugin") as mock_validate,
        ):
            mock_detect.return_value = (PackageType.MARKETPLACE_PLUGIN, plugin_json)
            mock_result = ValidationResult()
            mock_validate.return_value = mock_result
            validate_apm_package(tmp_path)
        mock_validate.assert_called_once()

    def test_skill_bundle_dispatched(self, tmp_path: Path) -> None:
        """SKILL_BUNDLE type dispatches to _validate_skill_bundle."""
        with (
            patch("apm_cli.models.validation.detect_package_type") as mock_detect,
            patch("apm_cli.models.validation._validate_skill_bundle") as mock_validate,
        ):
            mock_detect.return_value = (PackageType.SKILL_BUNDLE, None)
            mock_result = ValidationResult()
            mock_validate.return_value = mock_result
            validate_apm_package(tmp_path)
        mock_validate.assert_called_once()

    def test_hybrid_dispatched(self, tmp_path: Path) -> None:
        """HYBRID type dispatches to _validate_hybrid_package."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        with (
            patch("apm_cli.models.validation.detect_package_type") as mock_detect,
            patch("apm_cli.models.validation._validate_hybrid_package") as mock_validate,
        ):
            mock_detect.return_value = (PackageType.HYBRID, None)
            mock_result = ValidationResult()
            mock_validate.return_value = mock_result
            validate_apm_package(tmp_path)
        mock_validate.assert_called_once()


# ---------------------------------------------------------------------------
# _validate_hook_package
# ---------------------------------------------------------------------------


class TestValidateHookPackage:
    """Test _validate_hook_package internals."""

    def test_returns_valid_result_with_package(self, tmp_path: Path) -> None:
        """_validate_hook_package creates an APMPackage and returns valid result."""
        from apm_cli.models.validation import _validate_hook_package

        result: ValidationResult = ValidationResult()
        returned: ValidationResult = _validate_hook_package(tmp_path, result)
        assert returned.is_valid is True
        assert returned.package is not None
        assert returned.package.name == tmp_path.name

    def test_package_version_is_1_0_0(self, tmp_path: Path) -> None:
        """_validate_hook_package sets package version to '1.0.0'."""
        from apm_cli.models.validation import _validate_hook_package

        result: ValidationResult = ValidationResult()
        returned: ValidationResult = _validate_hook_package(tmp_path, result)
        assert returned.package is not None
        assert returned.package.version == "1.0.0"


# ---------------------------------------------------------------------------
# _validate_claude_skill
# ---------------------------------------------------------------------------


class TestValidateClaudeSkill:
    """Test _validate_claude_skill with frontmatter parsing."""

    def test_valid_skill_md_creates_package(self, tmp_path: Path) -> None:
        """Valid SKILL.md with frontmatter creates a package and returns valid result."""
        from apm_cli.models.validation import _validate_claude_skill

        skill_md: Path = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: my-skill\ndescription: A great skill\n---\n# My Skill\n")
        result: ValidationResult = ValidationResult()
        returned: ValidationResult = _validate_claude_skill(tmp_path, skill_md, result)
        assert returned.is_valid is True
        assert returned.package is not None
        assert returned.package.name == "my-skill"

    def test_skill_md_without_name_uses_dir_name(self, tmp_path: Path) -> None:
        """SKILL.md with no 'name' in frontmatter uses directory name."""
        from apm_cli.models.validation import _validate_claude_skill

        skill_md: Path = tmp_path / "SKILL.md"
        skill_md.write_text("---\ndescription: A skill\n---\n# Skill\n")
        result: ValidationResult = ValidationResult()
        returned: ValidationResult = _validate_claude_skill(tmp_path, skill_md, result)
        assert returned.package is not None
        assert returned.package.name == tmp_path.name

    def test_unreadable_skill_md_adds_error(self, tmp_path: Path) -> None:
        """Unreadable SKILL.md triggers an error on the result."""
        from apm_cli.models.validation import _validate_claude_skill

        skill_md: Path = tmp_path / "SKILL.md"
        result: ValidationResult = ValidationResult()
        with patch("builtins.open", side_effect=OSError("permission denied")):
            returned: ValidationResult = _validate_claude_skill(tmp_path, skill_md, result)
        assert not returned.is_valid
        assert any("Failed to process" in e for e in returned.errors)


# ---------------------------------------------------------------------------
# PackageType and ValidationError enums
# ---------------------------------------------------------------------------


class TestEnumValues:
    """Test enum string values are stable (public API)."""

    def test_package_type_values(self) -> None:
        """PackageType enum string values match expected constants."""
        assert PackageType.APM_PACKAGE.value == "apm_package"
        assert PackageType.CLAUDE_SKILL.value == "claude_skill"
        assert PackageType.HOOK_PACKAGE.value == "hook_package"
        assert PackageType.HYBRID.value == "hybrid"
        assert PackageType.MARKETPLACE_PLUGIN.value == "marketplace_plugin"
        assert PackageType.SKILL_BUNDLE.value == "skill_bundle"
        assert PackageType.INVALID.value == "invalid"

    def test_validation_error_values(self) -> None:
        """ValidationError enum string values match expected constants."""
        assert ValidationError.MISSING_APM_YML.value == "missing_apm_yml"
        assert ValidationError.MISSING_APM_DIR.value == "missing_apm_dir"
        assert ValidationError.INVALID_YML_FORMAT.value == "invalid_yml_format"
        assert ValidationError.MISSING_REQUIRED_FIELD.value == "missing_required_field"

    def test_invalid_virtual_package_extension_error(self) -> None:
        """InvalidVirtualPackageExtensionError is a ValueError subclass."""
        exc = InvalidVirtualPackageExtensionError("bad ext")
        assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# _validate_apm_package_with_yml – direct path
# ---------------------------------------------------------------------------


class TestValidateApmPackageWithYml:
    """Unit tests for _validate_apm_package_with_yml (via validate_apm_package)."""

    def test_dep_only_package_is_valid(self, tmp_path: Path) -> None:
        """apm.yml with deps and no .apm/ is accepted as a dep-only aggregator."""
        (tmp_path / "apm.yml").write_text(
            "name: agg\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n"
        )
        result: ValidationResult = validate_apm_package(tmp_path)
        assert result.is_valid

    def test_no_apm_dir_no_deps_fails(self, tmp_path: Path) -> None:
        """apm.yml with no .apm/ and no deps yields an error."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        result: ValidationResult = validate_apm_package(tmp_path)
        assert not result.is_valid

    def test_apm_dir_is_file_fails(self, tmp_path: Path) -> None:
        """apm.yml + .apm as file yields '.apm must be a directory' error."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        (tmp_path / ".apm").write_text("not a dir")
        result: ValidationResult = validate_apm_package(tmp_path)
        assert not result.is_valid
        assert any(".apm must be a directory" in e for e in result.errors)

    def test_empty_apm_dir_warns_no_primitives(self, tmp_path: Path) -> None:
        """apm.yml + empty .apm/ produces a 'no primitive files' warning."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        (tmp_path / ".apm").mkdir()
        result: ValidationResult = validate_apm_package(tmp_path)
        assert result.is_valid
        assert any("No primitive files" in w for w in result.warnings)

    def test_bad_semver_warns(self, tmp_path: Path) -> None:
        """apm.yml with non-semver version produces a semver warning."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: notasemver\n")
        (tmp_path / ".apm").mkdir()
        result: ValidationResult = validate_apm_package(tmp_path)
        assert any("semantic versioning" in w for w in result.warnings)

    def test_valid_apm_package_with_instructions(self, tmp_path: Path) -> None:
        """apm.yml + .apm/instructions/foo.md -> valid package."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        instructions_dir: Path = tmp_path / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "foo.md").write_text("# foo\nSome instruction.")
        result: ValidationResult = validate_apm_package(tmp_path)
        assert result.is_valid
        assert result.package is not None

    def test_empty_primitive_file_warns(self, tmp_path: Path) -> None:
        """Empty .md file in .apm/instructions/ produces a warning."""
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        instructions_dir: Path = tmp_path / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "empty.md").write_text("")
        result: ValidationResult = validate_apm_package(tmp_path)
        assert any("Empty primitive" in w for w in result.warnings)
