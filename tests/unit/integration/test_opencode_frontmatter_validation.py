"""Tests for OpenCode frontmatter validate-and-warn (Phase 1 of #581).

Covers both the pure validator and the install-time integration that
fires it before copy_agent() writes the file to .opencode/agents/.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apm_cli.integration import AgentIntegrator
from apm_cli.integration.opencode_frontmatter import validate_opencode_frontmatter
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference
from apm_cli.utils.diagnostics import DiagnosticCollector


def _make_package_info(pkg_dir: Path) -> PackageInfo:
    package = APMPackage(name="test-pkg", version="1.0.0", package_path=pkg_dir)
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=pkg_dir,
        resolved_reference=resolved_ref,
        installed_at="2024-01-01T00:00:00",
    )


def _warning_messages(diagnostics: DiagnosticCollector) -> list[str]:
    # Surface raw warning messages so tests can assert on substrings.
    return [d.message for d in diagnostics._diagnostics if d.category == "warning"]


class TestValidateOpencodeFrontmatter:
    """Pure unit tests for validate_opencode_frontmatter()."""

    def test_tools_as_dict_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": True, "Grep": False}},
            Path("agent.md"),
        )
        assert msgs == []

    def test_tools_as_list_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read", "Grep"]},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "bad.agent.md" in msgs[0]
        assert "tools" in msgs[0]
        assert "list" in msgs[0]

    def test_tools_as_string_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": "Read, Grep, Glob"},
            Path("claude.agent.md"),
        )
        assert len(msgs) == 1
        assert "tools" in msgs[0]
        assert "str" in msgs[0]

    def test_tools_dict_with_non_bool_value_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": "yes"}},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "non-boolean" in msgs[0]

    def test_color_hex_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"color": "#aabbcc"},
            Path("agent.md"),
        )
        assert msgs == []

    def test_color_short_hex_no_warning(self):
        msgs = validate_opencode_frontmatter({"color": "#abc"}, Path("a.md"))
        assert msgs == []

    def test_color_theme_enum_no_warning(self):
        for theme in ("primary", "secondary", "accent", "success", "warning", "error", "info"):
            assert validate_opencode_frontmatter({"color": theme}, Path("a.md")) == []

    def test_color_named_not_in_enum_warns(self):
        msgs = validate_opencode_frontmatter(
            {"color": "cyan"},
            Path("cyan.agent.md"),
        )
        assert len(msgs) == 1
        assert "color" in msgs[0]
        assert "cyan" in msgs[0]

    def test_color_non_string_warns(self):
        msgs = validate_opencode_frontmatter({"color": 123}, Path("a.md"))
        assert len(msgs) == 1
        assert "color" in msgs[0]

    def test_empty_frontmatter_no_warning(self):
        assert validate_opencode_frontmatter({}, Path("a.md")) == []
        assert validate_opencode_frontmatter(None, Path("a.md")) == []

    def test_multiple_problems_each_warned(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"], "color": "magenta"},
            Path("a.md"),
        )
        assert len(msgs) == 2

    def test_messages_are_ascii(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["x"], "color": "magenta"},
            Path("a.md"),
        )
        for m in msgs:
            m.encode("ascii")  # raises if non-ASCII


class TestOpencodeInstallEmitsWarnings:
    """End-to-end: integrate_agents_for_target() emits diagnostics.warn()
    for OpenCode-incompatible frontmatter before deploying the file."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()
        # OpenCode has auto_create=False and detect_by_dir=True; create
        # the marker directory so integration runs.
        (self.project_root / ".opencode").mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_agent(self, frontmatter: str) -> Path:
        pkg = self.project_root / "package"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        agent_path = agents_dir / "demo.agent.md"
        agent_path.write_text(f"---\n{frontmatter}\n---\n\n# Demo\n")
        return pkg

    def test_tools_as_list_emits_warning(self):
        pkg = self._write_agent("tools:\n  - Read\n  - Grep\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        msgs = _warning_messages(diagnostics)
        assert any("tools" in m and "demo.agent.md" in m for m in msgs), msgs

    def test_tools_as_dict_no_warning(self):
        pkg = self._write_agent("tools:\n  Read: true\n  Grep: false\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs

    def test_color_named_emits_warning(self):
        pkg = self._write_agent('color: "cyan"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert any("color" in m and "cyan" in m for m in msgs), msgs

    def test_color_hex_no_warning(self):
        pkg = self._write_agent('color: "#aabbcc"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs

    def test_file_is_still_deployed_when_warning_emitted(self):
        # Warnings must NOT block install: file lands in .opencode/agents/
        # so users can fix the source and reinstall, and so other valid
        # agents in the same package are not held up by the bad one.
        pkg = self._write_agent("tools:\n  - Read\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        deployed = self.project_root / ".opencode" / "agents" / "demo.md"
        assert deployed.exists()

    def test_malformed_yaml_does_not_crash(self):
        pkg = self._write_agent("tools: [unclosed\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        # Should not raise; validator gets empty fm when YAML is invalid.
        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

    @pytest.mark.parametrize("target_name", ["copilot", "claude", "codex", "windsurf", "cursor"])
    def test_non_opencode_targets_do_not_emit_opencode_warning(self, target_name):
        # The validation is scoped to format_id == "opencode_agent"; other
        # targets must not emit OpenCode-specific warnings even when the
        # frontmatter would be invalid for OpenCode.
        pkg = self._write_agent("tools:\n  - Read\ncolor: cyan\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()
        target = KNOWN_TARGETS[target_name]
        # Create marker dir for targets that need it.
        marker = self.project_root / target.root_dir
        marker.mkdir(exist_ok=True)

        self.integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs
