"""Unit tests for apm_cli.models.format_detection (pure logic cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.format_detection import (
    AgentPluginFormatEvidence,
    ApmYmlDetector,
    ApmYmlFormatEvidence,
    ClaudePluginDetector,
    ClaudePluginFormatEvidence,
    DetectionReport,
    FormatDetector,
    HookJsonDetector,
    HookJsonFormatEvidence,
    NormalizationPlanner,
    SkillMdDetector,
    SkillMdFormatEvidence,
)
from apm_cli.models.validation import PackageType

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# ApmYmlDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AgentPluginDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SkillMdDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HookJsonDetector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ClaudePluginDetector
# ---------------------------------------------------------------------------


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
        agent_plugin: AgentPluginFormatEvidence | None = None,
        claude_plugin: ClaudePluginFormatEvidence | None = None,
    ) -> DetectionReport:
        return DetectionReport(
            apm_yml=apm_yml,
            skill_md=skill_md,
            hook_json=hook_json,
            agent_plugin=agent_plugin,
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

    def _agent_plugin_evidence(
        self,
        plugin_json_path: Path | None = None,
        supported: bool = True,
    ) -> AgentPluginFormatEvidence:
        return AgentPluginFormatEvidence(
            plugin_json_path=plugin_json_path,
            schema_id="https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
            if plugin_json_path is not None
            else None,
            supported=supported,
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

    def test_hybrid_wins_over_plugin_signal_when_apm_yml_is_eligible(self) -> None:
        report = self._make_report(
            claude_plugin=self._plugin_evidence(plugin_json_path=Path("/fake/plugin.json")),
            apm_yml=self._apm_yml_evidence(has_apm_dir=True),
            skill_md=self._skill_md_evidence(has_root=True),
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.HYBRID

    def test_skill_bundle_wins_over_apm_only(self) -> None:
        """Nested skills beat plain apm.yml (cascade step 4 before step 5)."""
        report = self._make_report(
            apm_yml=self._apm_yml_evidence(has_apm_dir=True),
            skill_md=self._skill_md_evidence(has_root=False, nested=("s",)),
        )
        pkg_type, _ = NormalizationPlanner().plan(report)
        assert pkg_type == PackageType.SKILL_BUNDLE
