"""End-to-end tests for #1159 audit silent-skip fix using real ``git init``.

Unit tests in ``tests/unit/test_audit_ci_auto_discovery.py`` mock
``discover_policy_with_chain``. These tests exercise the real
discovery path with a real git working tree, so the wiring between
``git remote`` introspection, ``discover_policy_with_chain`` and the
audit CLI is end-to-end verified (no mocks on the discovery layer).

The "no_git_remote" outcome is the canonical CI scenario for #1159:
``apm audit --ci`` running in a checkout that does not expose an
``origin`` remote (e.g. shallow CI clones, ephemeral worktrees, or
projects pulled via tarball) silently passed pre-fix even when the
project apm.yml asked for fail-closed.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _git_init(path: Path) -> None:
    """Initialise a real git repo with no remote configured."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _setup_clean_project(project: Path, *, fetch_failure_default: str | None = None) -> None:
    apm_yml_lines = [
        "name: test-project",
        "version: '1.0.0'",
        "dependencies:",
        "  apm:",
        "    - owner/repo#v1.0.0",
    ]
    if fetch_failure_default is not None:
        apm_yml_lines += [
            "policy:",
            f"  fetch_failure_default: {fetch_failure_default}",
        ]
    (project / "apm.yml").write_text("\n".join(apm_yml_lines) + "\n", encoding="utf-8")
    lockfile = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: owner/repo
            resolved_ref: v1.0.0
            deployed_files:
              - .github/prompts/test.md
    """)
    (project / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    prompts_dir = project / ".github" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "test.md").write_text("Clean content\n", encoding="utf-8")


class TestAuditCiNoGitRemoteE2E:
    """Real ``git init`` without a remote -> outcome=no_git_remote."""

    def test_default_warn_emits_stderr_and_exits_zero(self, runner, tmp_path, monkeypatch):
        """Default warn: [!] on stderr; clean JSON on stdout; exit 0."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "[!]" in result.stderr
        assert "Could not determine org from git remote" in result.stderr
        # JSON on stdout must remain parseable end-to-end.
        data = json.loads(result.stdout)
        assert "summary" in data

    def test_block_fails_closed_with_exit_one(self, runner, tmp_path, monkeypatch):
        """policy.fetch_failure_default=block: [x] + exit 1."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _setup_clean_project(tmp_path, fetch_failure_default="block")

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "[x]" in result.stderr
        assert "policy.fetch_failure_default=block" in result.stderr


class TestAuditCiNoGitDirE2E:
    """No git directory at all -> outcome=no_git_remote (same surface)."""

    def test_warn_when_not_a_git_repo(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No git init -- bare directory.
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "[!]" in result.stderr
        assert "Could not determine org from git remote" in result.stderr


class TestAuditCiNoGitRemoteSarifE2E:
    """SARIF stdout cleanliness on no_git_remote -- TC-2 from #1164 review.

    Mirrors the JSON cleanliness probe in
    ``TestAuditCiNoGitRemoteE2E.test_default_warn_emits_stderr_and_exits_zero``
    but with ``-f sarif``.  The new ``[!]`` warning MUST land on stderr
    so the SARIF document on stdout stays valid for GitHub Code Scanning
    upload (``codeql/upload-sarif``).
    """

    def test_warn_emits_clean_sarif_on_stdout(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "sarif"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "[!]" in result.stderr
        assert "Could not determine org from git remote" in result.stderr
        # SARIF on stdout must be parseable end-to-end.
        sarif = json.loads(result.stdout)
        assert sarif.get("$schema", "").endswith("sarif-schema-2.1.0.json") or (
            sarif.get("version") == "2.1.0"
        )
        assert "runs" in sarif
        assert isinstance(sarif["runs"], list)

    def test_block_emits_clean_stderr_on_sarif_format(self, runner, tmp_path, monkeypatch):
        """Block path with -f sarif: [x] on stderr, exit 1, no stdout pollution."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _setup_clean_project(tmp_path, fetch_failure_default="block")

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "sarif"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "[x]" in result.stderr
        assert "policy.fetch_failure_default=block" in result.stderr
        # Block path exits before SARIF is rendered, so stdout must be empty.
        assert result.stdout.strip() == ""
