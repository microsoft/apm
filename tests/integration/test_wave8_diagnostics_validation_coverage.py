"""Wave 8 sprint -- push over 60% with diagnostics, validation, and auth helpers.

Targets the remaining ~104 lines+branches needed to cross 60%.
Focuses on pure-logic helpers in:
- apm_cli.utils.diagnostics (DiagnosticCollector full API + render)
- apm_cli.models.validation (PackageContentType, ValidationResult, DetectionEvidence,
  gather_detection_evidence, _has_hook_json)
- apm_cli.core.auth (AuthResolver helpers, HostInfo)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# DiagnosticCollector
# ---------------------------------------------------------------------------


class TestDiagnosticCollector:
    """Exercise every recording helper + query property."""

    def _make(self, verbose: bool = False):
        from apm_cli.utils.diagnostics import DiagnosticCollector

        return DiagnosticCollector(verbose=verbose)

    def test_empty_collector(self) -> None:
        dc = self._make()
        assert not dc.has_diagnostics
        assert dc.error_count == 0
        assert dc.security_count == 0
        assert dc.auth_count == 0
        assert dc.policy_count == 0
        assert dc.drift_count == 0
        assert not dc.has_critical_security
        assert dc.by_category() == {}

    def test_skip(self) -> None:
        dc = self._make()
        dc.skip("path/to/file", package="pkg")
        assert dc.has_diagnostics
        groups = dc.by_category()
        assert "collision" in groups
        assert groups["collision"][0].message == "path/to/file"

    def test_overwrite(self) -> None:
        dc = self._make()
        dc.overwrite("path/to/file", package="pkg", detail="overwritten by newer")
        groups = dc.by_category()
        assert "overwrite" in groups
        assert groups["overwrite"][0].detail == "overwritten by newer"

    def test_warn(self) -> None:
        dc = self._make()
        dc.warn("something odd", package="pkg")
        groups = dc.by_category()
        assert "warning" in groups

    def test_error(self) -> None:
        dc = self._make()
        dc.error("download failed", package="pkg")
        assert dc.error_count == 1

    def test_security_warning(self) -> None:
        dc = self._make()
        dc.security("hidden chars found", package="pkg", severity="warning")
        assert dc.security_count == 1
        assert not dc.has_critical_security

    def test_security_critical(self) -> None:
        dc = self._make()
        dc.security("injected chars", package="pkg", severity="critical")
        assert dc.has_critical_security

    def test_info(self) -> None:
        dc = self._make()
        dc.info("hint message", package="pkg")
        groups = dc.by_category()
        assert "info" in groups

    def test_policy(self) -> None:
        dc = self._make()
        dc.policy("blocked dep", package="pkg", severity="warning")
        assert dc.policy_count == 1

    def test_auth(self) -> None:
        dc = self._make()
        dc.auth("credential fallback", package="pkg")
        assert dc.auth_count == 1

    def test_drift(self) -> None:
        from apm_cli.utils.diagnostics import DRIFT_MODIFIED

        dc = self._make()
        dc.drift("path/file", kind=DRIFT_MODIFIED, package="pkg")
        assert dc.drift_count == 1

    def test_count_for_package(self) -> None:
        dc = self._make()
        dc.error("e1", package="a")
        dc.error("e2", package="b")
        dc.warn("w1", package="a")
        assert dc.count_for_package("a") == 2
        assert dc.count_for_package("a", category="error") == 1
        assert dc.count_for_package("b") == 1

    def test_render_summary_empty(self) -> None:
        dc = self._make()
        dc.render_summary()  # should not raise

    def test_render_summary_with_items(self) -> None:
        dc = self._make(verbose=True)
        dc.security("hidden chars", package="pkg", severity="critical")
        dc.security("mild chars", package="pkg", severity="warning")
        dc.security("noted", package="pkg", severity="info")
        dc.policy("blocked", package="pkg", severity="warning")
        dc.auth("fallback used", package="pkg")
        dc.drift("f.md", kind="modified", package="pkg")
        dc.skip("path/conflict", package="pkg")
        dc.overwrite("path/ow", package="pkg")
        dc.warn("general warn", package="pkg")
        dc.error("failure", package="pkg")
        dc.info("hint", package="pkg")
        with (
            patch("apm_cli.utils.diagnostics._rich_echo"),
            patch("apm_cli.utils.diagnostics._rich_warning"),
            patch("apm_cli.utils.diagnostics._rich_info"),
        ):
            dc.render_summary()

    def test_render_summary_non_verbose(self) -> None:
        dc = self._make(verbose=False)
        dc.security("hidden", package="p", severity="warning")
        dc.skip("conflict", package="p")
        dc.overwrite("ow", package="p")
        with (
            patch("apm_cli.utils.diagnostics._rich_echo"),
            patch("apm_cli.utils.diagnostics._rich_warning"),
            patch("apm_cli.utils.diagnostics._rich_info"),
        ):
            dc.render_summary()


class TestDiagnosticDataclass:
    """Diagnostic frozen dataclass."""

    def test_fields(self) -> None:
        from apm_cli.utils.diagnostics import Diagnostic

        d = Diagnostic(message="msg", category="error", package="pkg", detail="d", severity="high")
        assert d.message == "msg"
        assert d.category == "error"
        assert d.package == "pkg"
        assert d.detail == "d"
        assert d.severity == "high"


class TestDiagnosticConstants:
    """Category and drift constants."""

    def test_categories_defined(self) -> None:
        from apm_cli.utils.diagnostics import (
            CATEGORY_AUTH,
            CATEGORY_COLLISION,
            CATEGORY_DRIFT,
            CATEGORY_ERROR,
            CATEGORY_INFO,
            CATEGORY_OVERWRITE,
            CATEGORY_POLICY,
            CATEGORY_SECURITY,
            CATEGORY_WARNING,
        )

        cats = [
            CATEGORY_AUTH,
            CATEGORY_COLLISION,
            CATEGORY_DRIFT,
            CATEGORY_ERROR,
            CATEGORY_INFO,
            CATEGORY_OVERWRITE,
            CATEGORY_POLICY,
            CATEGORY_SECURITY,
            CATEGORY_WARNING,
        ]
        assert len(set(cats)) == 9

    def test_drift_kinds(self) -> None:
        from apm_cli.utils.diagnostics import DRIFT_MODIFIED, DRIFT_ORPHANED, DRIFT_UNINTEGRATED

        assert DRIFT_MODIFIED == "modified"
        assert DRIFT_UNINTEGRATED == "unintegrated"
        assert DRIFT_ORPHANED == "orphaned"


# ---------------------------------------------------------------------------
# models/validation.py
# ---------------------------------------------------------------------------


class TestPackageContentType:
    """PackageContentType enum parsing."""

    def test_from_string_instructions(self) -> None:
        from apm_cli.models.validation import PackageContentType

        assert PackageContentType.from_string("instructions") == PackageContentType.INSTRUCTIONS

    def test_from_string_skill(self) -> None:
        from apm_cli.models.validation import PackageContentType

        assert PackageContentType.from_string("skill") == PackageContentType.SKILL

    def test_from_string_hybrid(self) -> None:
        from apm_cli.models.validation import PackageContentType

        assert PackageContentType.from_string("hybrid") == PackageContentType.HYBRID

    def test_from_string_prompts(self) -> None:
        from apm_cli.models.validation import PackageContentType

        assert PackageContentType.from_string("prompts") == PackageContentType.PROMPTS

    def test_from_string_case_insensitive(self) -> None:
        from apm_cli.models.validation import PackageContentType

        assert PackageContentType.from_string("SKILL") == PackageContentType.SKILL
        assert PackageContentType.from_string("  Hybrid  ") == PackageContentType.HYBRID

    def test_from_string_empty_raises(self) -> None:
        from apm_cli.models.validation import PackageContentType

        with pytest.raises(ValueError, match=r"cannot be empty"):
            PackageContentType.from_string("")

    def test_from_string_invalid_raises(self) -> None:
        from apm_cli.models.validation import PackageContentType

        with pytest.raises(ValueError, match=r"Invalid package type"):
            PackageContentType.from_string("nonexistent")


class TestValidationResult:
    """ValidationResult dataclass behaviour."""

    def test_initial_state(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        assert vr.is_valid is True
        assert vr.errors == []
        assert vr.warnings == []
        assert not vr.has_issues()

    def test_add_error(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        vr.add_error("missing field")
        assert not vr.is_valid
        assert len(vr.errors) == 1
        assert vr.has_issues()

    def test_add_warning(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        vr.add_warning("deprecated")
        assert vr.is_valid
        assert len(vr.warnings) == 1
        assert vr.has_issues()

    def test_summary_valid(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        assert "[+]" in vr.summary()

    def test_summary_valid_with_warnings(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        vr.add_warning("w1")
        assert "[!]" in vr.summary()
        assert "1 warning" in vr.summary()

    def test_summary_invalid(self) -> None:
        from apm_cli.models.validation import ValidationResult

        vr = ValidationResult()
        vr.add_error("e1")
        vr.add_error("e2")
        assert "[x]" in vr.summary()
        assert "2 error" in vr.summary()


class TestPackageType:
    """PackageType enum members."""

    def test_members(self) -> None:
        from apm_cli.models.validation import PackageType

        assert PackageType.APM_PACKAGE.value == "apm_package"
        assert PackageType.CLAUDE_SKILL.value == "claude_skill"
        assert PackageType.HOOK_PACKAGE.value == "hook_package"
        assert PackageType.HYBRID.value == "hybrid"
        assert PackageType.MARKETPLACE_PLUGIN.value == "marketplace_plugin"
        assert PackageType.SKILL_BUNDLE.value == "skill_bundle"
        assert PackageType.INVALID.value == "invalid"


class TestValidationError:
    """ValidationError enum members."""

    def test_members(self) -> None:
        from apm_cli.models.validation import ValidationError

        assert ValidationError.MISSING_APM_YML.value == "missing_apm_yml"
        assert ValidationError.INVALID_YML_FORMAT.value == "invalid_yml_format"


class TestHasHookJson:
    """_has_hook_json helper."""

    def test_no_hooks_dir(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import _has_hook_json

        assert _has_hook_json(tmp_path) is False

    def test_hooks_dir_with_json(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import _has_hook_json

        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "hooks.json").write_text("{}")
        assert _has_hook_json(tmp_path) is True

    def test_apm_hooks_dir(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import _has_hook_json

        hooks = tmp_path / ".apm" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "hooks.json").write_text("{}")
        assert _has_hook_json(tmp_path) is True

    def test_hooks_dir_empty(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import _has_hook_json

        (tmp_path / "hooks").mkdir()
        assert _has_hook_json(tmp_path) is False


class TestDetectionEvidence:
    """DetectionEvidence dataclass and gather_detection_evidence."""

    def test_dataclass_fields(self) -> None:
        from apm_cli.models.validation import DetectionEvidence

        ev = DetectionEvidence(
            has_apm_yml=True,
            has_skill_md=False,
            has_hook_json=False,
            plugin_json_path=None,
            plugin_dirs_present=(),
        )
        assert ev.has_apm_yml is True
        assert not ev.has_plugin_evidence

    def test_has_plugin_evidence_with_manifest(self) -> None:
        from apm_cli.models.validation import DetectionEvidence

        ev = DetectionEvidence(
            has_apm_yml=False,
            has_skill_md=False,
            has_hook_json=False,
            plugin_json_path=Path("/tmp/plugin.json"),
            plugin_dirs_present=("skills",),
            has_plugin_manifest=True,
        )
        assert ev.has_plugin_evidence

    def test_gather_empty_dir(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        ev = gather_detection_evidence(tmp_path)
        assert not ev.has_apm_yml
        assert not ev.has_skill_md
        assert not ev.has_hook_json
        assert ev.plugin_json_path is None
        assert ev.plugin_dirs_present == ()

    def test_gather_with_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        (tmp_path / "apm.yml").write_text("name: test\n")
        ev = gather_detection_evidence(tmp_path)
        assert ev.has_apm_yml

    def test_gather_with_skill_md(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        (tmp_path / "SKILL.md").write_text("# Skill\n")
        ev = gather_detection_evidence(tmp_path)
        assert ev.has_skill_md

    def test_gather_with_plugin_dirs(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        (tmp_path / "skills").mkdir()
        (tmp_path / "agents").mkdir()
        ev = gather_detection_evidence(tmp_path)
        assert "skills" in ev.plugin_dirs_present
        assert "agents" in ev.plugin_dirs_present

    def test_gather_with_nested_skills(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        skills_dir = tmp_path / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# My Skill\n")
        ev = gather_detection_evidence(tmp_path)
        assert "my-skill" in ev.nested_skill_dirs

    def test_gather_with_claude_plugin_dir(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import gather_detection_evidence

        (tmp_path / ".claude-plugin").mkdir()
        ev = gather_detection_evidence(tmp_path)
        assert ev.has_claude_plugin_dir
        assert ev.has_plugin_manifest


class TestInvalidVirtualPackageExtensionError:
    """Custom exception type."""

    def test_is_value_error(self) -> None:
        from apm_cli.models.validation import InvalidVirtualPackageExtensionError

        with pytest.raises(ValueError):
            raise InvalidVirtualPackageExtensionError("bad ext")


class TestPluginDirs:
    """_PLUGIN_DIRS constant."""

    def test_ordering(self) -> None:
        from apm_cli.models.validation import _PLUGIN_DIRS

        assert _PLUGIN_DIRS == ("agents", "skills", "commands")


# ---------------------------------------------------------------------------
# core/auth.py -- HostInfo
# ---------------------------------------------------------------------------


class TestHostInfo:
    """HostInfo dataclass."""

    def test_basic(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        )
        assert hi.host == "github.com"
        assert hi.kind == "github"
        assert hi.has_public_repos is True

    def test_ghe(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="corp.ghe.com",
            kind="ghe_cloud",
            has_public_repos=False,
            api_base="https://api.corp.ghe.com",
        )
        assert hi.kind == "ghe_cloud"

    def test_ado(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="dev.azure.com",
            kind="ado",
            has_public_repos=True,
            api_base="https://dev.azure.com",
        )
        assert hi.kind == "ado"

    def test_port(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="bb.example.com",
            kind="generic",
            has_public_repos=False,
            api_base="https://bb.example.com",
            port=7999,
        )
        assert hi.port == 7999

    def test_display_name_no_port(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        )
        assert hi.display_name == "github.com"

    def test_display_name_with_port(self) -> None:
        from apm_cli.core.auth import HostInfo

        hi = HostInfo(
            host="bb.example.com",
            kind="generic",
            has_public_repos=False,
            api_base="https://bb.example.com",
            port=7999,
        )
        assert "7999" in hi.display_name


class TestAuthResolverClassifyHost:
    """AuthResolver.classify_host static method."""

    def test_github_com(self) -> None:
        from apm_cli.core.auth import AuthResolver

        info = AuthResolver.classify_host("github.com")
        assert info.kind == "github"

    def test_ghe_host(self) -> None:
        from apm_cli.core.auth import AuthResolver

        info = AuthResolver.classify_host("corp.ghe.com")
        assert info.kind == "ghe_cloud"

    def test_ado_host(self) -> None:
        from apm_cli.core.auth import AuthResolver

        info = AuthResolver.classify_host("dev.azure.com")
        assert info.kind == "ado"

    def test_unknown_host(self) -> None:
        from apm_cli.core.auth import AuthResolver

        info = AuthResolver.classify_host("gitlab.example.com")
        assert info.kind not in ("github", "ghe", "ado") or info.kind == "generic"
