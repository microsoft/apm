"""End-to-end install tests for ``policy.dependencies.require_pinned_constraint``.

Mirrors the call pattern of ``test_policy_install_e2e.py`` but focused on
the pinned-constraint policy field added by #1491.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = pytest.mark.component

_PATCH_DISCOVER_GATE = "apm_cli.policy.discovery.discover_policy_with_chain"
_PATCH_DISCOVER_PREFLIGHT = "apm_cli.policy.install_preflight.discover_policy_with_chain"
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"


def _fetch(policy: ApmPolicy) -> PolicyFetchResult:
    return PolicyFetchResult(
        policy=policy,
        source="org:test-org/.github",
        cached=False,
        error=None,
        cache_age_seconds=None,
        cache_stale=False,
        fetch_error=None,
        outcome="found",
    )


def _write_apm_yml(path: Path, deps: list) -> None:
    data = {
        "name": "test-project",
        "version": "1.0.0",
        "dependencies": {"apm": deps},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _policy(enforcement: str) -> ApmPolicy:
    return ApmPolicy(
        enforcement=enforcement,
        dependencies=DependencyPolicy(require_pinned_constraint=True),
    )


@pytest.fixture()
def project(tmp_path):
    orig_cwd = os.getcwd()
    project_dir = tmp_path / "pinned-e2e"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    (project_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    os.chdir(project_dir)
    yield project_dir, CliRunner()
    os.chdir(orig_cwd)


def _invoke_install(runner: CliRunner):
    from apm_cli.cli import cli

    return runner.invoke(cli, ["install"])


class TestRequirePinnedConstraintE2E:
    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_block_mode_aborts_install_on_unbounded_dep(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/some-skills"],  # NO_REF
        )
        fetch = _fetch(_policy("block"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        assert result.exit_code != 0, (
            f"Expected non-zero exit, got {result.exit_code}\n{result.output}"
        )
        out = result.output.lower()
        # Diagnostic detail must mention the offending dep and the reason.
        assert "test-org/some-skills" in out, out
        assert "no ref" in out or "unbounded" in out or "pinned" in out, out
        assert not (project_dir / "apm.lock.yaml").exists(), (
            "Lockfile should NOT exist after blocked install"
        )

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_warn_mode_emits_warning_without_aborting(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/some-skills"],
        )
        fetch = _fetch(_policy("warn"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        # Install should not abort because of the pinning violation.
        assert "blocked by org policy" not in result.output.lower(), result.output
        # Warning must surface the violation.
        out = result.output.lower()
        assert "test-org/some-skills" in out, out
