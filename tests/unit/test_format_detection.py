"""Unit tests for apm_cli.models.format_detection.

Covers the composition-model classes introduced in issue #782:
- Per-format evidence dataclasses
- ApmYmlDetector, SkillMdDetector, HookJsonDetector, ClaudePluginDetector
- PackageFormatRegistry
- NormalizationPlanner
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.format_detection import (
    ApmYmlDetector,
    ApmYmlFormatEvidence,
    ClaudePluginDetector,
    ClaudePluginFormatEvidence,
    DetectionReport,
    FormatDetector,
    HookJsonDetector,
    HookJsonFormatEvidence,
    NormalizationPlanner,
    PackageFormatRegistry,
    SkillMdDetector,
    SkillMdFormatEvidence,
)
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# ApmYmlDetector
# ---------------------------------------------------------------------------


class TestApmYmlDetector:
    """Tests for ApmYmlDetector."""

    def test_returns_none_when_no_apm_yml(self, tmp_path: Path) -> None:
        ev = ApmYmlDetector().detect(tmp_path)
        assert ev is None

    def test_returns_evidence_when_apm_yml_present(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ev = ApmYmlDetector().detect(tmp_path)
        assert isinstance(ev, ApmYmlFormatEvidence)
        assert ev.apm_yml_path == tmp_path / "apm.yml"

    def test_has_apm_dir_false_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ev = ApmYmlDetector().detect(tmp_path)
        assert ev is not None
        assert ev.has_apm_dir is False

    def test_has_apm_dir_true_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        (tmp_path / ".apm").mkdir()
        ev = ApmYmlDetector().detect(tmp_path)
        assert ev is not None
        assert ev.has_apm_dir is True

    def test_declares_dependencies_true_with_apm_deps(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(
            "name: pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - other\n"
        )
        ev = ApmYmlDetector().detect(tmp_path)
        assert ev is not None
        assert ev.declares_dependencies is True

    def test_declares_dependencies_false_without_deps(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ev = ApmYmlDetector().detect(tmp_path)
        assert ev is not None
        assert ev.declares_dependencies is False


# ---------------------------------------------------------------------------
# SkillMdDetector
# ---------------------------------------------------------------------------


class TestSkillMdDetector:
    """Tests for SkillMdDetector."""

    def test_returns_none_when_no_skill_md_and_no_nested(self, tmp_path: Path) -> None:
        ev = SkillMdDetector().detect(tmp_path)
        assert ev is None

    def test_returns_evidence_when_root_skill_md_present(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        ev = SkillMdDetector().detect(tmp_path)
        assert isinstance(ev, SkillMdFormatEvidence)
        assert ev.skill_md_path == tmp_path / "SKILL.md"
        assert ev.nested_skill_dirs == ()

    def test_returns_none_skill_md_path_when_only_nested(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill")
        ev = SkillMdDetector().detect(tmp_path)
        assert isinstance(ev, SkillMdFormatEvidence)
        assert ev.skill_md_path is None
        assert "my-skill" in ev.nested_skill_dirs

    def test_detects_both_root_and_nested(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        skill_dir = tmp_path / "skills" / "nested"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# nested")
        ev = SkillMdDetector().detect(tmp_path)
        assert isinstance(ev, SkillMdFormatEvidence)
        assert ev.skill_md_path is not None
        assert "nested" in ev.nested_skill_dirs

    def test_nested_dirs_sorted(self, tmp_path: Path) -> None:
        for name in ("beta", "alpha", "gamma"):
            d = tmp_path / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("# skill")
        ev = SkillMdDetector().detect(tmp_path)
        assert ev is not None
        assert ev.nested_skill_dirs == ("alpha", "beta", "gamma")


# ---------------------------------------------------------------------------
# HookJsonDetector
# ---------------------------------------------------------------------------


class TestHookJsonDetector:
    """Tests for HookJsonDetector."""

    def test_returns_none_when_no_hooks(self, tmp_path: Path) -> None:
        ev = HookJsonDetector().detect(tmp_path)
        assert ev is None

    def test_returns_evidence_for_hooks_dir(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "hook.json").write_text("{}")
        ev = HookJsonDetector().detect(tmp_path)
        assert isinstance(ev, HookJsonFormatEvidence)
        assert hooks in ev.hooks_dirs_found

    def test_returns_evidence_for_apm_hooks(self, tmp_path: Path) -> None:
        apm_hooks = tmp_path / ".apm" / "hooks"
        apm_hooks.mkdir(parents=True)
        (apm_hooks / "hook.json").write_text("{}")
        ev = HookJsonDetector().detect(tmp_path)
        assert isinstance(ev, HookJsonFormatEvidence)
        assert ev.hooks_dirs_found  # at least one found

    def test_returns_none_when_hooks_dir_has_no_json(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "README.md").write_text("# hooks")
        ev = HookJsonDetector().detect(tmp_path)
        assert ev is None


# ---------------------------------------------------------------------------
# ClaudePluginDetector
# ---------------------------------------------------------------------------


class TestClaudePluginDetector:
    """Tests for ClaudePluginDetector."""

    def test_returns_none_when_no_plugin(self, tmp_path: Path) -> None:
        ev = ClaudePluginDetector().detect(tmp_path)
        assert ev is None

    def test_returns_evidence_for_plugin_json(self, tmp_path: Path) -> None:
        (tmp_path / "plugin.json").write_text("{}")
        ev = ClaudePluginDetector().detect(tmp_path)
        assert isinstance(ev, ClaudePluginFormatEvidence)
        assert ev.plugin_json_path == tmp_path / "plugin.json"
        assert ev.has_claude_plugin_dir is False

    def test_returns_evidence_for_claude_plugin_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".claude-plugin").mkdir()
        ev = ClaudePluginDetector().detect(tmp_path)
        assert isinstance(ev, ClaudePluginFormatEvidence)
        assert ev.plugin_json_path is None
        assert ev.has_claude_plugin_dir is True

    def test_plugin_dirs_present_enumerated(self, tmp_path: Path) -> None:
        (tmp_path / "plugin.json").write_text("{}")
        (tmp_path / "agents").mkdir()
        (tmp_path / "commands").mkdir()
        ev = ClaudePluginDetector().detect(tmp_path)
        assert ev is not None
        assert "agents" in ev.plugin_dirs_present
        assert "commands" in ev.plugin_dirs_present

    def test_plugin_dirs_absent_without_manifest(self, tmp_path: Path) -> None:
        """Detector returns None when only dir signals but no manifest."""
        (tmp_path / "agents").mkdir()
        (tmp_path / "skills").mkdir()
        ev = ClaudePluginDetector().detect(tmp_path)
        assert ev is None


# ---------------------------------------------------------------------------
# FormatDetector protocol conformance
# ---------------------------------------------------------------------------


class TestFormatDetectorProtocol:
    """Verify all detectors satisfy the FormatDetector protocol."""

    @pytest.mark.parametrize(
        "detector",
        [ApmYmlDetector(), SkillMdDetector(), HookJsonDetector(), ClaudePluginDetector()],
    )
    def test_conforms_to_protocol(self, detector: object) -> None:
        assert isinstance(detector, FormatDetector)


# ---------------------------------------------------------------------------
# PackageFormatRegistry
# ---------------------------------------------------------------------------


class TestPackageFormatRegistry:
    """Tests for PackageFormatRegistry.detect()."""

    def test_empty_dir_all_none(self, tmp_path: Path) -> None:
        report = PackageFormatRegistry().detect(tmp_path)
        assert isinstance(report, DetectionReport)
        assert report.apm_yml is None
        assert report.skill_md is None
        assert report.hook_json is None
        assert report.claude_plugin is None

    def test_apm_yml_detected(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        report = PackageFormatRegistry().detect(tmp_path)
        assert report.apm_yml is not None
        assert report.claude_plugin is None

    def test_skill_md_detected(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        report = PackageFormatRegistry().detect(tmp_path)
        assert report.skill_md is not None
        assert report.skill_md.skill_md_path is not None

    def test_claude_plugin_detected(self, tmp_path: Path) -> None:
        (tmp_path / "plugin.json").write_text("{}")
        report = PackageFormatRegistry().detect(tmp_path)
        assert report.claude_plugin is not None
        assert report.claude_plugin.plugin_json_path is not None

    def test_hook_json_detected(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.json").write_text("{}")
        report = PackageFormatRegistry().detect(tmp_path)
        assert report.hook_json is not None

    def test_all_detectors_run_independently(self, tmp_path: Path) -> None:
        """All detectors run even when one finds evidence."""
        (tmp_path / "plugin.json").write_text("{}")
        (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.json").write_text("{}")
        report = PackageFormatRegistry().detect(tmp_path)
        assert report.claude_plugin is not None
        assert report.skill_md is not None
        assert report.hook_json is not None


# ---------------------------------------------------------------------------
# NormalizationPlanner
# ---------------------------------------------------------------------------


class TestNormalizationPlanner:
    """Tests for NormalizationPlanner.plan() cascade."""

    def _make_report(
        self,
        *,
        apm_yml: ApmYmlFormatEvidence | None = None,
        skill_md: SkillMdFormatEvidence | None = None,
        hook_json: HookJsonFormatEvidence | None = None,
        claude_plugin: ClaudePluginFormatEvidence | None = None,
    ) -> DetectionReport:
        return DetectionReport(
            apm_yml=apm_yml,
            skill_md=skill_md,
            hook_json=hook_json,
            claude_plugin=claude_plugin,
        )

    def _plugin_evidence(
        self,
        plugin_json_path: Path | None = None,
        has_claude_plugin_dir: bool = False,
    ) -> ClaudePluginFormatEvidence:
        return ClaudePluginFormatEvidence(
            plugin_json_path=plugin_json_path,
            has_claude_plugin_dir=has_claude_plugin_dir,
            plugin_dirs_present=(),
        )

    def _apm_yml_evidence(
        self,
        has_apm_dir: bool = False,
        declares_dependencies: bool = False,
    ) -> ApmYmlFormatEvidence:
        return ApmYmlFormatEvidence(
            apm_yml_path=Path("/fake/apm.yml"),
            has_apm_dir=has_apm_dir,
            declares_dependencies=declares_dependencies,
        )

    def _skill_md_evidence(
        self,
        has_root: bool = True,
        nested: tuple[str, ...] = (),
    ) -> SkillMdFormatEvidence:
        return SkillMdFormatEvidence(
            skill_md_path=Path("/fake/SKILL.md") if has_root else None,
            nested_skill_dirs=nested,
        )

    def test_marketplace_plugin_via_plugin_json(self, tmp_path: Path) -> None:
        plugin_path = tmp_path / "plugin.json"
        report = self._make_report(
            claude_plugin=self._plugin_evidence(plugin_json_path=plugin_path)
        )
        pkg_type, plugin_json = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_json == plugin_path

    def test_marketplace_plugin_via_claude_plugin_dir(self) -> None:
        report = self._make_report(claude_plugin=self._plugin_evidence(has_claude_plugin_dir=True))
        pkg_type, plugin_json = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_json is None

    def test_hybrid(self) -> None:
        report = self._make_report(
            apm_yml=self._apm_yml_evidence(has_apm_dir=True),
            skill_md=self._skill_md_evidence(has_root=True),
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.HYBRID

    def test_claude_skill(self) -> None:
        report = self._make_report(skill_md=self._skill_md_evidence(has_root=True))
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.CLAUDE_SKILL

    def test_skill_bundle(self) -> None:
        report = self._make_report(
            skill_md=self._skill_md_evidence(has_root=False, nested=("skill-a",))
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.SKILL_BUNDLE

    def test_apm_package_with_apm_dir(self) -> None:
        report = self._make_report(apm_yml=self._apm_yml_evidence(has_apm_dir=True))
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_apm_package_with_declared_deps(self) -> None:
        report = self._make_report(apm_yml=self._apm_yml_evidence(declares_dependencies=True))
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_invalid_apm_yml_no_apm_dir_no_deps(self) -> None:
        report = self._make_report(
            apm_yml=self._apm_yml_evidence(has_apm_dir=False, declares_dependencies=False)
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.INVALID

    def test_hook_package(self) -> None:
        report = self._make_report(
            hook_json=HookJsonFormatEvidence(hooks_dirs_found=(Path("/fake/hooks"),))
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.HOOK_PACKAGE

    def test_invalid_empty_report(self) -> None:
        report = self._make_report()
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.INVALID

    def test_plugin_wins_over_hybrid_signals(self) -> None:
        """Claude plugin beats hybrid when all signals present (cascade step 1 wins)."""
        report = self._make_report(
            claude_plugin=self._plugin_evidence(plugin_json_path=Path("/fake/plugin.json")),
            apm_yml=self._apm_yml_evidence(has_apm_dir=True),
            skill_md=self._skill_md_evidence(has_root=True),
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.MARKETPLACE_PLUGIN

    def test_skill_bundle_wins_over_apm_only(self) -> None:
        """Nested skills beat plain apm.yml (cascade step 4 before step 5)."""
        report = self._make_report(
            apm_yml=self._apm_yml_evidence(has_apm_dir=True),
            skill_md=self._skill_md_evidence(has_root=False, nested=("s",)),
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.SKILL_BUNDLE
