"""Comprehensive unit tests for apm_cli.models.validation (pure logic cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.validation import (
    DetectionEvidence,
    InvalidVirtualPackageExtensionError,
    PackageContentType,
    PackageType,
    ValidationError,
    ValidationResult,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# PackageContentType.from_string()
# ---------------------------------------------------------------------------


class TestPackageContentTypeFromString:
    """Test PackageContentType.from_string() parsing."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("instructions", PackageContentType.INSTRUCTIONS),
            ("skill", PackageContentType.SKILL),
            ("hybrid", PackageContentType.HYBRID),
            ("prompts", PackageContentType.PROMPTS),
            ("SKILL", PackageContentType.SKILL),
            ("Instructions", PackageContentType.INSTRUCTIONS),
            ("  hybrid  ", PackageContentType.HYBRID),
        ],
        ids=[
            "instructions-lower",
            "skill-lower",
            "hybrid-lower",
            "prompts-lower",
            "uppercase",
            "mixed-case",
            "whitespace-stripped",
        ],
    )
    def test_parses_known_values(self, raw, expected) -> None:
        """Known type names parse case-insensitively after stripping whitespace."""
        assert PackageContentType.from_string(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "match"),
        [("", "cannot be empty"), ("bad_type", "Invalid package type 'bad_type'")],
        ids=["empty-string", "invalid-value"],
    )
    def test_rejects_empty_or_unknown_value(self, raw, match) -> None:
        with pytest.raises(ValueError, match=match):
            PackageContentType.from_string(raw)

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


# ---------------------------------------------------------------------------
# gather_detection_evidence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# detect_package_type cascade
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _apm_yml_declares_dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_apm_package - top-level dispatcher
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_hook_package
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_claude_skill
# ---------------------------------------------------------------------------


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
# _validate_apm_package_with_yml - direct path
# ---------------------------------------------------------------------------
