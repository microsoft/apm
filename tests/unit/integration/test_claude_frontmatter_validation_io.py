"""Claude agent ``tools`` diagnostic at the install boundary (req-tg-006, #5356).

Filesystem cases: the diagnostic fires on write and on a retained identical
file, stays silent for native names, planning or collision skips, and never
alters the deployed agent.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.integration import AgentIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference
from apm_cli.utils.diagnostics import (
    CATEGORY_AGENT_LOSSY_COMPILATION,
    CATEGORY_WARNING,
    DiagnosticCollector,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("req-tg-006", "P-OWN-claude-agent-tools-diagnostic"),
]


def _make_package_info(pkg_dir: Path) -> PackageInfo:
    package = APMPackage(name="test-pkg", version="1.0.0", package_path=pkg_dir)
    ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=pkg_dir,
        resolved_reference=ref,
        installed_at="2024-01-01T00:00:00",
    )


def _lossy(diagnostics: DiagnosticCollector) -> list:
    return diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION, [])


def _claude_warnings(diagnostics: DiagnosticCollector) -> list:
    return [
        d
        for d in diagnostics.by_category().get(CATEGORY_WARNING, [])
        if "Claude agent" in d.message
    ]


class TestClaudeInstallEmitsDiagnostic:
    def setup_method(self):
        self.project_root = Path(tempfile.mkdtemp())
        self.integrator = AgentIntegrator()
        # The claude target is detect_by_dir; create the marker so integration runs.
        (self.project_root / ".claude").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.project_root, ignore_errors=True)

    def _write_agent(self, frontmatter: str, *, bom: bool = False) -> Path:
        pkg = self.project_root / "package"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        prefix = "\ufeff" if bom else ""
        (agents_dir / "demo.agent.md").write_text(
            f"{prefix}---\n{frontmatter}\n---\n\n# Demo\n", encoding="utf-8"
        )
        return pkg

    def _integrate(self, pkg: Path) -> tuple[DiagnosticCollector, object]:
        diagnostics = DiagnosticCollector()
        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            _make_package_info(pkg),
            self.project_root,
            diagnostics=diagnostics,
        )
        return diagnostics, result

    def _run(self, frontmatter: str, **kwargs) -> DiagnosticCollector:
        diagnostics, result = self._integrate(self._write_agent(frontmatter, **kwargs))
        assert result.files_integrated == 1
        return diagnostics

    def test_foreign_tools_emit_lossy_diagnostic_and_keep_file_verbatim(self):
        diagnostics = self._run("name: demo\ntools: [bash, view, edit]\n")
        losses = _lossy(diagnostics)
        assert len(losses) == 1
        assert "test-pkg" in losses[0].package
        assert "demo.agent.md" in losses[0].message and "bash, view, edit" in losses[0].message
        assert losses[0].detail.startswith("Fix:")
        deployed = (self.project_root / ".claude" / "agents" / "demo.md").read_text()
        assert "tools: [bash, view, edit]" in deployed

    def test_bom_prefixed_agent_is_still_diagnosed(self):
        diagnostics = self._run("name: demo\ntools: [bash, view]\n", bom=True)
        assert len(_lossy(diagnostics)) == 1

    def test_claude_tools_are_silent(self):
        diagnostics = self._run("name: demo\ntools:\n  - Read\n  - Bash(git *)\n  - mcp__gh__*\n")
        assert not _lossy(diagnostics)
        assert not _claude_warnings(diagnostics)

    def test_no_tools_field_is_silent(self):
        diagnostics = self._run("name: demo\ndescription: d\n")
        assert not _lossy(diagnostics)
        assert not _claude_warnings(diagnostics)

    def test_malformed_tools_emit_default_visible_warning(self):
        diagnostics = self._run("name: demo\ntools:\n  Read: true\n")
        assert not _lossy(diagnostics)
        warnings = _claude_warnings(diagnostics)
        assert len(warnings) == 1
        assert "could not be verified" in warnings[0].message
        assert "Fix:" in warnings[0].message

    def test_non_mapping_frontmatter_warns_unverified(self):
        diagnostics = self._run("- tools: [bash]")
        warnings = _claude_warnings(diagnostics)
        assert len(warnings) == 1
        assert "not a YAML mapping" in warnings[0].message and "Fix:" in warnings[0].message

    def test_invalid_yaml_frontmatter_warns_unverified(self):
        diagnostics = self._run("tools: [bash")
        warnings = _claude_warnings(diagnostics)
        assert len(warnings) == 1
        assert "invalid YAML" in warnings[0].message and "Fix:" in warnings[0].message

    def test_retained_identical_agent_is_diagnosed_on_reinstall(self):
        pkg = self._write_agent("name: demo\ntools: [bash, view]\n")
        first, result = self._integrate(pkg)
        assert result.files_integrated == 1
        assert len(_lossy(first)) == 1

        second, result = self._integrate(pkg)
        assert result.files_integrated == 0
        assert result.files_adopted + result.files_skipped == 1
        assert len(_lossy(second)) == 1, "identical retained file must still be diagnosed"

    def test_collision_skip_stays_silent(self):
        pkg = self._write_agent("name: demo\ntools: [bash, view]\n")
        target = self.project_root / ".claude" / "agents" / "demo.md"
        target.parent.mkdir(parents=True)
        target.write_text("# hand-written, not APM's\n", encoding="utf-8")

        diagnostics, result = self._integrate(pkg)
        assert result.files_integrated == 0
        assert not _lossy(diagnostics)
