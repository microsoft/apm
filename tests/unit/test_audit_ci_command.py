"""Tests for the ``apm audit --ci`` CLI integration."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache

# -- Fixtures -------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the APMPackage parse cache between tests."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _setup_clean_project(project: Path) -> None:
    """Create a fully consistent project (all CI checks pass)."""
    apm_yml = textwrap.dedent("""\
        name: test-project
        version: '1.0.0'
        dependencies:
          apm:
            - owner/repo#v1.0.0
    """)
    lockfile = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: owner/repo
            resolved_ref: v1.0.0
            deployed_files:
              - .github/prompts/test.md
    """)
    (project / "apm.yml").write_text(apm_yml, encoding="utf-8")
    (project / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    prompts_dir = project / ".github" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "test.md").write_text("Clean content\n", encoding="utf-8")


def _setup_failing_project(project: Path) -> None:
    """Create a project with ref mismatch (CI check will fail)."""
    apm_yml = textwrap.dedent("""\
        name: test-project
        version: '1.0.0'
        dependencies:
          apm:
            - owner/repo#v2.0.0
    """)
    lockfile = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: owner/repo
            resolved_ref: v1.0.0
            deployed_files:
              - .github/prompts/test.md
    """)
    (project / "apm.yml").write_text(apm_yml, encoding="utf-8")
    (project / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    prompts_dir = project / ".github" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "test.md").write_text("content\n", encoding="utf-8")


# -- Tests ----------------------------------------------------------


class TestCIFlagExists:
    def test_ci_flag_in_help(self, runner):
        result = runner.invoke(audit, ["--help"])
        assert result.exit_code == 0
        assert "--ci" in result.output


class TestCIIncompatibleFlags:
    def test_ci_with_strip(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "--strip"])
        assert result.exit_code != 0

    def test_ci_with_dry_run(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "--dry-run"])
        assert result.exit_code != 0

    def test_ci_with_file(self, runner, tmp_path):
        test_file = tmp_path / "dummy.md"
        test_file.write_text("hello", encoding="utf-8")
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "--file", str(test_file)])
        assert result.exit_code != 0

    def test_ci_with_package(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "some-package"])
        assert result.exit_code != 0


class TestCIExitCodes:
    def test_exit_0_all_pass(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])
        assert result.exit_code == 0

    def test_exit_1_on_failure(self, runner, tmp_path):
        _setup_failing_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])
        assert result.exit_code == 1


class TestCIOutputFormats:
    def test_json_output(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "-f", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "passed" in data
        assert "checks" in data
        assert "summary" in data
        assert data["passed"] is True

    def test_sarif_output(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "-f", "sarif"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["version"] == "2.1.0"
        assert "runs" in data

    def test_json_output_with_failures(self, runner, tmp_path):
        _setup_failing_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "-f", "json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False
        assert data["summary"]["failed"] > 0

    def test_text_output_shows_checks(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])
        assert result.exit_code == 0
        assert "passed" in result.output.lower() or "check" in result.output.lower()

    def test_output_to_file(self, runner, tmp_path):
        _setup_clean_project(tmp_path)
        outfile = tmp_path / "report.json"
        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "-f", "json", "-o", str(outfile)])
        assert result.exit_code == 0
        assert outfile.exists()
        data = json.loads(outfile.read_text(encoding="utf-8"))
        assert data["passed"] is True
