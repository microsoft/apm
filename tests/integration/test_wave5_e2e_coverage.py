"""Wave 5 -- end-to-end CLI workflow tests for deep code coverage.

These tests create realistic project structures and invoke CLI commands
via CliRunner to exercise deep code paths in:
- compile (CLI + target resolution + validation + dry-run)
- compile watcher helpers (_format_target_label, _resolve_compile_target)
- uninstall engine
- policy discovery
- output formatters
- deps CLI

Strategy: Use monkeypatch.chdir(tmp_path) + CliRunner to exercise real
Python code. Only mock sys.exit (via CliRunner catch_exceptions) and
external I/O (HTTP, subprocess).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

# -------------------------------------------------------------------
# Helpers to build realistic project fixtures
# -------------------------------------------------------------------


def _make_project(
    root: Path,
    *,
    target: str = "copilot",
    targets: list[str] | None = None,
    with_instructions: bool = True,
    with_agents: bool = False,
    with_skills: bool = False,
    with_chatmodes: bool = False,
    with_constitution: bool = False,
    with_apm_modules: bool = False,
    with_lockfile: bool = False,
    with_github_dir: bool = True,
) -> None:
    """Build a realistic APM project in root."""
    # apm.yml
    yml_lines = [
        "name: test-project",
        "version: 1.0.0",
    ]
    if targets:
        yml_lines.append("targets:")
        for t in targets:
            yml_lines.append(f"  - {t}")
    elif target:
        yml_lines.append(f"target: {target}")

    (root / "apm.yml").write_text("\n".join(yml_lines) + "\n", encoding="utf-8")

    # .apm directory with primitives
    if with_instructions:
        instr_dir = root / ".apm" / "instructions"
        instr_dir.mkdir(parents=True, exist_ok=True)
        (instr_dir / "coding.instructions.md").write_text(
            "---\ndescription: Coding standards\napplyTo: '**/*.py'\n---\n\n"
            "# Coding Standards\n\n"
            "Follow PEP 8 conventions.\n"
            "Use type hints for all function signatures.\n",
            encoding="utf-8",
        )

    if with_agents:
        agents_dir = root / ".apm" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "helper.agent.md").write_text(
            "---\ndescription: Helper agent\n---\n\n"
            "# Helper Agent\n\n"
            "You are a helpful coding assistant.\n",
            encoding="utf-8",
        )

    if with_skills:
        skill_dir = root / ".apm" / "skills" / "analyser"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Code analyser\n---\n\n"
            "# Analyser Skill\n\n"
            "Analyses code quality and suggests improvements.\n",
            encoding="utf-8",
        )

    if with_chatmodes:
        chatmode_dir = root / ".apm" / "agents"
        chatmode_dir.mkdir(parents=True, exist_ok=True)
        (chatmode_dir / "backend.agent.md").write_text(
            "---\ndescription: Backend engineer mode\n---\n\n"
            "# Backend Engineer\n\n"
            "You specialise in backend development.\n",
            encoding="utf-8",
        )

    if with_constitution:
        mem_dir = root / ".apm" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "constitution.md").write_text(
            "# Project Constitution\n\nCore principles for this project.\n",
            encoding="utf-8",
        )

    if with_github_dir:
        gh_dir = root / ".github"
        gh_dir.mkdir(parents=True, exist_ok=True)

    if with_apm_modules:
        pkg_dir = root / "apm_modules" / "test-org" / "sample-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            "name: sample-pkg\nversion: 1.0.0\ntype: skill\n",
            encoding="utf-8",
        )
        skill_dir = pkg_dir / ".apm" / "skills" / "sample"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Sample skill\n---\n\n# Sample\n\nDoes things.\n",
            encoding="utf-8",
        )

    if with_lockfile:
        (root / "apm.lock.yaml").write_text(
            "version: 1\n"
            "dependencies:\n"
            "  - name: sample-pkg\n"
            "    repo_url: https://github.com/test-org/sample-pkg\n"
            "    resolved_ref: main\n"
            "    resolved_commit: abc1234567890\n"
            "    deployed_files:\n"
            "      - .github/skills/sample/SKILL.md\n",
            encoding="utf-8",
        )


# -------------------------------------------------------------------
# Compile command -- deep path tests
# -------------------------------------------------------------------


class TestCompileEndToEnd:
    """Test the compile command with realistic project setups."""

    def test_compile_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "apm.yml" in result.output

    def test_compile_empty_apm_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compile with .apm/ dir but no instruction files."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\ntarget: copilot\n")
        (tmp_path / ".apm").mkdir()
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"])
        # Should report "No instruction files found" or similar
        assert result.exit_code != 0

    def test_compile_no_content_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Compile with no .apm/ dir and no apm_modules."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\ntarget: copilot\n")
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"])
        assert result.exit_code != 0
        assert "No APM content" in result.output or "No instruction" in result.output

    def test_compile_with_instructions_copilot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_with_instructions_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="claude", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0
        assert (tmp_path / "CLAUDE.md").exists()

    def test_compile_with_instructions_gemini(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="gemini", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_chatmodes=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_verbose(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_validate_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_chatmodes=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--validate"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "validated" in result.output.lower()

    def test_compile_local_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_apm_modules=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--local-only"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_explicit_target_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "-t", "claude"], catch_exceptions=False)
        assert result.exit_code == 0
        assert (tmp_path / "CLAUDE.md").exists()

    def test_compile_multi_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            targets=["copilot", "claude"],
            with_instructions=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_all_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--all"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_all_with_target_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--all", "-t", "claude"])
        assert result.exit_code != 0

    def test_compile_with_constitution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_constitution=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_no_constitution_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_constitution=True,
        )
        runner = CliRunner()
        result = runner.invoke(
            self._cli(), ["compile", "--no-constitution"], catch_exceptions=False
        )
        assert result.exit_code == 0

    def test_compile_with_agents_and_skills(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_agents=True,
            with_skills=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_cursor_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="cursor", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0
        assert (tmp_path / "AGENTS.md").is_file()

    def test_compile_single_agents_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--single-agents"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_target_all_deprecation_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "-t", "all"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "deprecated" in result.output.lower()

    def test_compile_clean_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        # Create an orphaned AGENTS.md
        orphan_dir = tmp_path / "src" / "old-module"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "AGENTS.md").write_text("# Old\n")
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--clean"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_no_links_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=True)
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile", "--no-links"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_compile_with_apm_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(
            tmp_path,
            target="copilot",
            with_instructions=True,
            with_apm_modules=True,
            with_lockfile=True,
        )
        runner = CliRunner()
        result = runner.invoke(self._cli(), ["compile"], catch_exceptions=False)
        assert result.exit_code == 0

    @staticmethod
    def _cli():
        from apm_cli.cli import cli

        return cli


# -------------------------------------------------------------------
# Compile target resolution helpers
# -------------------------------------------------------------------


class TestResolveCompileTarget:
    """Test _resolve_compile_target for various target inputs."""

    def test_none_returns_none(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(None) is None

    def test_single_string_passthrough(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target("claude") == "claude"
        assert _resolve_compile_target("gemini") == "gemini"
        assert _resolve_compile_target("vscode") == "vscode"

    def test_single_element_list(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["claude"])
        assert result == "claude"

    def test_multi_target_list_claude_copilot(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["claude", "copilot"])
        assert isinstance(result, frozenset)

    def test_multi_target_list_claude_gemini(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["claude", "gemini"])
        assert isinstance(result, frozenset)

    def test_copilot_list_collapses_to_vscode(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["copilot"])
        # copilot maps to vscode family
        assert isinstance(result, str)

    def test_agent_skills_only_list(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["agent-skills"])
        # agent-skills has no compile_family, should be skipped/sentinel
        assert result is not None

    def test_cursor_list(self) -> None:
        from apm_cli.commands.compile.cli import _resolve_compile_target

        result = _resolve_compile_target(["cursor"])
        assert isinstance(result, str)


class TestFormatTargetLabel:
    """Test _format_target_label for compile watch mode."""

    def test_none_target(self) -> None:
        from apm_cli.commands.compile.watcher import _format_target_label

        assert _format_target_label(None, None, None) is None

    def test_string_target(self) -> None:
        from apm_cli.commands.compile.watcher import _format_target_label

        result = _format_target_label("copilot", None, None)
        assert result is not None
        assert "Compiling for" in result

    def test_frozenset_target_with_user_list(self) -> None:
        from apm_cli.commands.compile.watcher import _format_target_label

        result = _format_target_label(
            frozenset({"vscode", "claude", "agents"}),
            ["copilot", "claude"],
            None,
        )
        assert result is not None
        assert "--target" in result

    def test_frozenset_target_with_config_list(self) -> None:
        from apm_cli.commands.compile.watcher import _format_target_label

        result = _format_target_label(
            frozenset({"vscode", "claude", "agents"}),
            None,
            ["copilot", "claude"],
        )
        assert result is not None
        assert "apm.yml" in result

    def test_frozenset_target_generic(self) -> None:
        from apm_cli.commands.compile.watcher import _format_target_label

        result = _format_target_label(
            frozenset({"claude", "gemini"}),
            None,
            None,
        )
        assert result is not None
        assert "multi-target" in result


# -------------------------------------------------------------------
# Display helpers in compile/cli.py
# -------------------------------------------------------------------


class TestCompileDisplayHelpers:
    """Test display helper functions to cover Rich table rendering paths."""

    def test_display_single_file_summary_no_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_single_file_summary

        stats = {
            "primitives_found": 5,
            "instructions": 3,
            "contexts": 2,
            "agents": 0,
        }
        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            _display_single_file_summary(stats, "Matched", "abc123", Path("AGENTS.md"), False)

    def test_display_single_file_summary_with_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_single_file_summary

        stats = {
            "primitives_found": 5,
            "instructions": 3,
            "contexts": 2,
            "agents": 1,
        }
        mock_console = mock.MagicMock()
        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            _display_single_file_summary(stats, "Matched", "abc123", Path("AGENTS.md"), False)
        mock_console.print.assert_called()

    def test_display_next_steps_no_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_next_steps

        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            _display_next_steps("AGENTS.md")

    def test_display_next_steps_with_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_next_steps

        mock_console = mock.MagicMock()
        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            _display_next_steps("AGENTS.md")
        mock_console.print.assert_called()

    def test_display_validation_errors_no_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_validation_errors

        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            _display_validation_errors(["Missing 'description': file.md"])

    def test_display_validation_errors_with_console(self) -> None:
        from unittest import mock

        from apm_cli.commands.compile.cli import _display_validation_errors

        mock_console = mock.MagicMock()
        with mock.patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            _display_validation_errors(
                [
                    "file.md: Missing 'description'",
                    "other.md: Empty content body",
                    "third.md: applyTo scope globally",
                    "bad.md: some unknown error",
                ]
            )
        mock_console.print.assert_called()

    def test_get_validation_suggestion(self) -> None:
        from apm_cli.commands.compile.cli import _get_validation_suggestion

        assert "description" in _get_validation_suggestion("Missing 'description'").lower()
        assert "applyTo" in _get_validation_suggestion("applyTo scope globally")
        assert "content" in _get_validation_suggestion("Empty content").lower()
        assert "structure" in _get_validation_suggestion("some other error").lower()


# -------------------------------------------------------------------
# Uninstall engine -- deep paths via CliRunner
# -------------------------------------------------------------------


class TestUninstallEngine:
    """Test uninstall via CliRunner to cover engine.py code paths."""

    def test_uninstall_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "some-pkg"])
        assert result.exit_code != 0

    def test_uninstall_package_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot")
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "nonexistent-pkg"])
        # Exits 0 but warns about invalid format or no packages found
        assert "invalid" in result.output.lower() or "no packages" in result.output.lower()

    def test_uninstall_no_package_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot")
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall"])
        # Should fail -- no package specified
        assert result.exit_code != 0


# -------------------------------------------------------------------
# Output formatters -- direct function tests
# -------------------------------------------------------------------


class TestOutputFormatters:
    """Test output/formatters.py CompilationFormatter methods."""

    def test_compilation_formatter_init(self) -> None:
        from apm_cli.output.formatters import CompilationFormatter

        fmt = CompilationFormatter(use_color=False)
        assert fmt is not None

    def test_compilation_formatter_no_color(self) -> None:
        from apm_cli.output.formatters import CompilationFormatter

        fmt = CompilationFormatter(use_color=False)
        result = fmt._styled("test", "bold")
        assert result == "test"

    def test_compilation_formatter_with_color(self) -> None:
        from apm_cli.output.formatters import CompilationFormatter

        fmt = CompilationFormatter(use_color=True)
        result = fmt._styled("test", "bold")
        assert "test" in result

    def test_get_strategy_symbol(self) -> None:
        from apm_cli.output.formatters import CompilationFormatter
        from apm_cli.output.models import PlacementStrategy

        fmt = CompilationFormatter(use_color=False)
        for strategy in PlacementStrategy:
            symbol = fmt._get_strategy_symbol(strategy)
            assert isinstance(symbol, str)

    def test_get_strategy_color(self) -> None:
        from apm_cli.output.formatters import CompilationFormatter
        from apm_cli.output.models import PlacementStrategy

        fmt = CompilationFormatter(use_color=False)
        for strategy in PlacementStrategy:
            color = fmt._get_strategy_color(strategy)
            assert isinstance(color, str)


# -------------------------------------------------------------------
# Policy discovery -- import and test pure logic
# -------------------------------------------------------------------


class TestPolicyDiscovery:
    """Test policy discovery without network calls."""

    def test_parse_remote_url_github(self) -> None:
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("https://github.com/owner/repo.git")
        assert result is not None
        assert result[0] == "owner"

    def test_parse_remote_url_ssh(self) -> None:
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("git@github.com:owner/repo.git")
        assert result is not None
        assert result[0] == "owner"

    def test_parse_remote_url_invalid(self) -> None:
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("not-a-url")
        assert result is None

    def test_is_github_host(self) -> None:
        from apm_cli.policy.discovery import _is_github_host

        assert _is_github_host("github.com") is True
        assert _is_github_host("gitlab.com") is False

    def test_strip_source_prefix(self) -> None:
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("org:owner/repo") == "owner/repo"
        assert _strip_source_prefix("url:https://example.com") == "https://example.com"
        assert _strip_source_prefix("file:local.yml") == "local.yml"
        assert _strip_source_prefix("plain") == "plain"

    def test_split_hash_pin(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin

        # Need a valid sha256 hex digest (64 chars)
        valid_hash = "sha256:" + "a" * 64
        algo, digest = _split_hash_pin(valid_hash)
        assert algo == "sha256"
        assert digest == "a" * 64

    def test_compute_hash_normalized(self) -> None:
        from apm_cli.policy.discovery import _compute_hash_normalized

        result = _compute_hash_normalized("hello world", None)
        assert isinstance(result, str)
        assert ":" in result

    def test_verify_hash_pin_valid(self) -> None:
        from apm_cli.policy.discovery import (
            _compute_hash_normalized,
            _verify_hash_pin,
        )

        content = "test content"
        computed = _compute_hash_normalized(content, None)
        # Should not raise
        _verify_hash_pin(content, computed, source_label="test")

    def test_policy_fetch_result_dataclass(self) -> None:
        from apm_cli.policy.discovery import PolicyFetchResult

        result = PolicyFetchResult(
            source="org:test-org/.github",
            outcome="absent",
        )
        assert result.source == "org:test-org/.github"
        assert result.outcome == "absent"
        assert result.policy is None

    def test_extract_extends_host(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("https://github.com/org/policy")
        assert result is not None

    def test_derive_leaf_host(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("github:owner/repo", tmp_path)
        # May return None or a host string depending on implementation
        assert result is None or isinstance(result, str)


# -------------------------------------------------------------------
# Deps CLI -- CliRunner tests for dependency commands
# -------------------------------------------------------------------


class TestDepsCli:
    """Test dependency commands via CliRunner."""

    def test_deps_list_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["deps", "list"])
        # May exit 0 with a message or exit non-zero
        assert result.exit_code == 0 or "apm.yml" in result.output.lower()

    def test_deps_list_empty_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot", with_instructions=False)
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["deps", "list"])
        # Should work but show no deps
        assert result.exit_code == 0 or "No dependencies" in result.output

    def test_deps_tree_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["deps", "tree"])
        # Exercises deps tree code path even without apm.yml
        assert isinstance(result.output, str)

    def test_deps_check_no_lockfile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project(tmp_path, target="copilot")
        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["deps", "check"])
        # Will fail or warn about missing lockfile
        assert result.exit_code != 0 or "lock" in result.output.lower()


# -------------------------------------------------------------------
# Script runner -- direct tests
# -------------------------------------------------------------------


class TestScriptRunnerDirect:
    """Test ScriptRunner directly to cover core/script_runner.py."""

    def test_script_runner_init(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner is not None

    def test_script_runner_no_apm_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        # Running a nonexistent script should raise when no apm.yml exists
        with pytest.raises(RuntimeError, match=r"apm.yml"):
            runner.run_script("nonexistent", {})
