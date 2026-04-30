"""Tests for ``apm audit --ci`` policy auto-discovery (closes #827).

Mirrors the install pipeline behaviour: when ``--ci`` is set without
``--policy``, auto-discover the org policy via ``discover_policy_with_chain``
so CI catches sideloaded files (the "copy-paste bypass" defense). The
new ``--no-policy`` flag opts out of auto-discovery.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import (
    ApmPolicy,
    UnmanagedFilesPolicy,
)

# -- Fixtures -------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _setup_project_with_unmanaged_file(project: Path) -> None:
    """Project with a sideloaded prompt file that is NOT in the lockfile."""
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
              - .github/prompts/managed.md
    """)
    (project / "apm.yml").write_text(apm_yml, encoding="utf-8")
    (project / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    prompts_dir = project / ".github" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "managed.md").write_text("ok\n", encoding="utf-8")
    # Sideloaded file -- not in lockfile.
    (prompts_dir / "sideloaded.md").write_text("evil\n", encoding="utf-8")


def _make_policy_fetch_with_unmanaged_deny() -> PolicyFetchResult:
    """An auto-discovered policy that bans unmanaged files in .github/prompts."""
    policy = ApmPolicy(
        enforcement="block",
        unmanaged_files=UnmanagedFilesPolicy(
            action="deny",
            directories=(".github/prompts",),
        ),
    )
    return PolicyFetchResult(
        policy=policy,
        source="org:test-org/.github",
        cached=False,
        outcome="found",
    )


def _make_no_policy_fetch() -> PolicyFetchResult:
    """An auto-discovery result with no policy found."""
    return PolicyFetchResult(
        policy=None,
        source="",
        cached=False,
        outcome="absent",
    )


# -- Tests ----------------------------------------------------------


class TestAutoDiscoveryFlag:
    def test_no_policy_flag_in_help(self, runner):
        result = runner.invoke(audit, ["--help"])
        assert result.exit_code == 0
        assert "--no-policy" in result.output


class TestAutoDiscoveryRuns:
    """When --ci is set without --policy, auto-discovery runs."""

    @patch("apm_cli.policy.discovery.discover_policy_with_chain")
    def test_auto_discovery_finds_policy_runs_unmanaged_check(
        self, mock_discover, runner, tmp_path
    ):
        _setup_project_with_unmanaged_file(tmp_path)
        mock_discover.return_value = _make_policy_fetch_with_unmanaged_deny()

        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])

        # Auto-discovery should have been invoked.
        mock_discover.assert_called_once()
        # Sideloaded file violates the policy -> exit 1.
        assert result.exit_code == 1, result.output

    @patch("apm_cli.policy.discovery.discover_policy_with_chain")
    def test_auto_discovery_no_policy_baseline_only_passes(self, mock_discover, runner, tmp_path):
        _setup_project_with_unmanaged_file(tmp_path)
        mock_discover.return_value = _make_no_policy_fetch()

        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])

        mock_discover.assert_called_once()
        # Baseline-only (no unmanaged-file enforcement) -> exit 0.
        assert result.exit_code == 0, result.output


class TestAutoDiscoveryOptOut:
    """--no-policy disables auto-discovery."""

    @patch("apm_cli.policy.discovery.discover_policy_with_chain")
    def test_no_policy_skips_auto_discovery(self, mock_discover, runner, tmp_path):
        _setup_project_with_unmanaged_file(tmp_path)
        # Even though discovery would find a deny policy, --no-policy
        # means it must not be called.
        mock_discover.return_value = _make_policy_fetch_with_unmanaged_deny()

        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci", "--no-policy"])

        mock_discover.assert_not_called()
        assert result.exit_code == 0, result.output


class TestAutoDiscoveryFetchFailure:
    """fetch failure during auto-discovery honors fetch_failure_default."""

    @patch("apm_cli.policy.discovery.discover_policy_with_chain")
    def test_fetch_failure_warn_proceeds(self, mock_discover, runner, tmp_path):
        _setup_project_with_unmanaged_file(tmp_path)
        mock_discover.return_value = PolicyFetchResult(
            policy=None,
            source="org:foo/.github",
            outcome="cache_miss_fetch_fail",
            error="dns failure",
        )

        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])

        # Default warn -> proceed with baseline only.
        assert result.exit_code == 0, result.output

    @patch("apm_cli.policy.discovery.discover_policy_with_chain")
    def test_fetch_failure_block_exits_one(self, mock_discover, runner, tmp_path):
        _setup_project_with_unmanaged_file(tmp_path)
        # Add project-side opt-in to fail closed.
        apm_yml = (tmp_path / "apm.yml").read_text() + ("policy:\n  fetch_failure_default: block\n")
        (tmp_path / "apm.yml").write_text(apm_yml, encoding="utf-8")
        mock_discover.return_value = PolicyFetchResult(
            policy=None,
            source="org:foo/.github",
            outcome="cache_miss_fetch_fail",
            error="dns failure",
        )

        with patch("apm_cli.commands.audit.Path.cwd", return_value=tmp_path):
            result = runner.invoke(audit, ["--ci"])

        assert result.exit_code == 1, result.output
