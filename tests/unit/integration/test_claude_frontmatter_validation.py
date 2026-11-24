"""Tests for the Claude Code ``tools`` allowlist diagnostic (req-tg-006, #5356)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apm_cli.integration import AgentIntegrator
from apm_cli.integration.claude_frontmatter import (
    MAX_UNRECOGNIZED_SHOWN,
    check_claude_tools,
    claude_tools_messages,
    is_claude_tool,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference
from apm_cli.utils.diagnostics import (
    CATEGORY_AGENT_LOSSY_COMPILATION,
    CATEGORY_WARNING,
    DiagnosticCollector,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("req-tg-006")]


class TestIsClaudeTool:
    @pytest.mark.parametrize(
        "entry",
        [
            "Bash",
            "Read",
            "Agent",
            "TaskCreate",
            "TaskUpdate",
            "mcp__github",
            "mcp__github__*",
            "mcp__github__get_issue",
            "Bash(git status *)",
            " Grep ",
        ],
    )
    def test_accepts_claude_vocabulary(self, entry):
        assert is_claude_tool(entry)

    @pytest.mark.parametrize(
        "entry", ["bash", "view", "edit", "sql", "", "Unknown(x)", "mcp_github"]
    )
    def test_rejects_foreign_or_malformed_names(self, entry):
        assert not is_claude_tool(entry)


class TestCheckClaudeTools:
    def test_absent_or_null_tools_does_not_restrict(self):
        assert not check_claude_tools({}).restricts
        assert not check_claude_tools({"tools": None}).restricts
        assert not check_claude_tools(None).restricts

    def test_comma_string_and_list_are_equivalent(self):
        as_list = check_claude_tools({"tools": ["Read", "bash"]})
        as_str = check_claude_tools({"tools": "Read, bash"})
        assert as_list == as_str
        assert as_list.recognized == ("Read",)
        assert as_list.unrecognized == ("bash",)
        assert not as_list.resolves_to_nothing

    def test_all_foreign_names_resolve_to_nothing(self):
        check = check_claude_tools({"tools": ["bash", "view", "edit"]})
        assert check.resolves_to_nothing

    @pytest.mark.parametrize(
        "tools", ["Agent(worker, reviewer)", ["Agent(worker, reviewer)"]], ids=["string", "list"]
    )
    def test_a_comma_inside_a_permission_specifier_stays_one_entry(self, tools):
        check = check_claude_tools({"tools": tools})
        assert check.recognized == ("Agent(worker, reviewer)",)
        assert check.unrecognized == ()
        assert claude_tools_messages(check, "a.md") is None

    def test_non_list_shape_is_malformed(self):
        assert check_claude_tools({"tools": {"Read": True}}).malformed
        assert check_claude_tools({"tools": 7}).malformed


class TestClaudeToolsMessages:
    def test_no_message_when_everything_resolves(self):
        assert claude_tools_messages(check_claude_tools({"tools": ["Read"]}), "a.md") is None

    def test_zero_tool_message_names_refusal(self):
        message, detail = claude_tools_messages(
            check_claude_tools({"tools": ["bash", "view"]}), "demo.agent.md"
        )
        assert "demo.agent.md" in message
        assert "no entry" in message and "bash, view" in message
        assert "refuses to spawn" in message
        assert detail.startswith("Fix:")

    def test_partial_message_lists_only_unrecognized(self):
        message, _ = claude_tools_messages(check_claude_tools({"tools": ["Read", "sql"]}), "d.md")
        assert "(sql)" in message and "narrower access" in message

    def test_bounded_names_and_ascii_rendering(self):
        tools = [f"t{i}" for i in range(MAX_UNRECOGNIZED_SHOWN + 3)] + ["caf\u00e9"]
        message, _ = claude_tools_messages(check_claude_tools({"tools": tools}), "d.md")
        assert f"and {len(tools) - MAX_UNRECOGNIZED_SHOWN} more" in message
        assert "\u00e9" not in message

    def test_malformed_message(self):
        message, detail = claude_tools_messages(check_claude_tools({"tools": {"a": 1}}), "d.md")
        assert "could not be verified" in message
        assert detail.startswith("Fix:")


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


class TestClaudeInstallEmitsDiagnostic:
    """integrate_agents_for_target(claude) emits req-tg-006 diagnostics without altering the file."""

    def setup_method(self):
        self.project_root = Path(tempfile.mkdtemp())
        self.integrator = AgentIntegrator()
        # The claude target is detect_by_dir; create the marker so integration runs.
        (self.project_root / ".claude").mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.project_root, ignore_errors=True)

    def _write_agent(self, frontmatter: str) -> Path:
        pkg = self.project_root / "package"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "demo.agent.md").write_text(f"---\n{frontmatter}\n---\n\n# Demo\n")
        return pkg

    def _run(self, frontmatter: str) -> DiagnosticCollector:
        diagnostics = DiagnosticCollector()
        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            _make_package_info(self._write_agent(frontmatter)),
            self.project_root,
            diagnostics=diagnostics,
        )
        assert result.files_integrated == 1
        return diagnostics

    def test_foreign_tools_emit_lossy_diagnostic_and_keep_file_verbatim(self):
        diagnostics = self._run("name: demo\ntools: [bash, view, edit]\n")
        losses = diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION, [])
        assert len(losses) == 1
        assert "test-pkg" in losses[0].package
        assert "demo.agent.md" in losses[0].message and "bash, view, edit" in losses[0].message
        assert losses[0].detail.startswith("Fix:")
        deployed = (self.project_root / ".claude" / "agents" / "demo.md").read_text()
        assert "tools: [bash, view, edit]" in deployed

    def test_claude_tools_are_silent(self):
        diagnostics = self._run("name: demo\ntools:\n  - Read\n  - Bash(git *)\n  - mcp__gh__*\n")
        assert not diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION)
        assert not [
            d
            for d in diagnostics.by_category().get(CATEGORY_WARNING, [])
            if "Claude agent" in d.message
        ]

    def test_no_tools_field_is_silent(self):
        diagnostics = self._run("name: demo\ndescription: d\n")
        assert not diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION)

    def test_malformed_tools_emit_warning_not_lossy(self):
        diagnostics = self._run("name: demo\ntools:\n  Read: true\n")
        assert not diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION)
        warnings = [
            d
            for d in diagnostics.by_category().get(CATEGORY_WARNING, [])
            if "Claude agent" in d.message
        ]
        assert len(warnings) == 1 and "could not be verified" in warnings[0].message
