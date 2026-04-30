"""Tests for ``apm audit --ci --policy`` CLI integration."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy

# -- Fixtures -------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _setup_clean_project(project: Path) -> None:
    """Create a project that passes all baseline + default policy checks."""
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


def _write_policy_file(project: Path, **overrides) -> Path:
    """Write a minimal policy file and return its path."""
    import yaml

    data = {
        "name": "test-policy",
        "version": "1.0.0",
        "enforcement": "block",
    }
    data.update(overrides)
    policy_path = project / "apm-policy.yml"
    policy_path.write_text(yaml.dump(data), encoding="utf-8")
    return policy_path


# -- Tests ----------------------------------------------------------


class TestCiWithPolicyFlag:
    def test_ci_with_policy_file(self, runner, tmp_path, monkeypatch):
        """--ci --policy <file> runs both baseline and policy checks."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--policy", str(policy_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    def test_ci_with_policy_json_output(self, runner, tmp_path, monkeypatch):
        """JSON output includes both baseline + policy checks."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--policy", str(policy_path), "-f", "json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Baseline: up to 7 checks, Policy: 17 checks -> total > 7 when
        # policy evaluation actually ran.  Asserting > 7 (not > 6) catches
        # the regression where only baseline checks are returned.
        assert data["summary"]["total"] > 7

    def test_ci_with_policy_deny_fails(self, runner, tmp_path, monkeypatch):
        """Policy deny list causing failure -> exit 1."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        import yaml

        policy_data = {
            "name": "strict-policy",
            "version": "1.0.0",
            "enforcement": "block",
            "dependencies": {"deny": ["owner/*"]},
        }
        policy_path = tmp_path / "strict-policy.yml"
        policy_path.write_text(yaml.dump(policy_data), encoding="utf-8")

        result = runner.invoke(
            audit,
            ["--ci", "--policy", str(policy_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 1


class TestCiWithPolicyOrg:
    def test_ci_policy_org_discovery(self, runner, tmp_path, monkeypatch):
        """--ci --policy org triggers discover_policy with 'org' override."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(policy=ApmPolicy(), source="org:test/.github")

        with patch(
            "apm_cli.policy.discovery.discover_policy", return_value=mock_result
        ) as mock_disc:
            result = runner.invoke(
                audit,
                ["--ci", "--policy", "org"],
                catch_exceptions=False,
            )
            mock_disc.assert_called_once()
            call_kwargs = mock_disc.call_args
            assert call_kwargs.kwargs.get("policy_override") == "org"
            assert result.exit_code == 0


class TestCiPolicyNotFound:
    def test_policy_not_found_still_runs_baseline(self, runner, tmp_path, monkeypatch):
        """If policy fetch returns not-found (no error), baseline runs alone."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(error=None, policy=None)

        with patch("apm_cli.policy.discovery.discover_policy", return_value=mock_result):
            result = runner.invoke(
                audit,
                ["--ci", "--policy", "org"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0


class TestCiPolicyFetchError:
    def test_fetch_error_exits_1(self, runner, tmp_path, monkeypatch):
        """If policy fetch has an error AND project opts in to fail-closed, exit 1."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        # #829: post-warn-default behaviour requires opting in via
        # policy.fetch_failure_default=block to fail closed on fetch error.
        (tmp_path / "apm.yml").write_text(
            "name: test-project\nversion: '1.0.0'\n"
            "dependencies:\n  apm:\n    - owner/repo#v1.0.0\n"
            "policy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )

        mock_result = PolicyFetchResult(error="Network timeout")

        with patch("apm_cli.policy.discovery.discover_policy", return_value=mock_result):
            result = runner.invoke(
                audit,
                ["--ci", "--policy", "org"],
                catch_exceptions=False,
            )
            assert result.exit_code == 1
            assert "Policy fetch failed" in result.output


class TestNoCacheFlag:
    def test_no_cache_flag_accepted(self, runner, tmp_path, monkeypatch):
        """--no-cache flag is accepted and passed to discover_policy."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(policy=ApmPolicy(), source="org:test/.github")

        with patch(
            "apm_cli.policy.discovery.discover_policy", return_value=mock_result
        ) as mock_disc:
            result = runner.invoke(
                audit,
                ["--ci", "--policy", "org", "--no-cache"],
                catch_exceptions=False,
            )
            mock_disc.assert_called_once()
            assert mock_disc.call_args.kwargs.get("no_cache") is True
            assert result.exit_code == 0

    def test_no_cache_without_policy(self, runner, tmp_path, monkeypatch):
        """--no-cache without --policy doesn't error (just ignored)."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-cache"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0


class TestCiWithoutPolicy:
    def test_baseline_only(self, runner, tmp_path, monkeypatch):
        """--ci without --policy runs baseline only."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "-f", "json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Only baseline checks (max 8 incl. skill-subset + includes-consent)
        assert data["summary"]["total"] <= 8
