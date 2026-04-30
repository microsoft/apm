"""End-to-end integration tests for `apm install --dry-run`.

Covers gap G2: presentation/dry_run.py (extracted in PR #764) was not
exercised against the binary. This test exists in part because a latent
NameError on the orphan-preview path slipped through review until it was
hardened.

Uses the real `microsoft/apm-sample-package` from GitHub. Requires
GITHUB_APM_PAT or GITHUB_TOKEN for API access.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    """Path to the APM CLI executable (PATH first, then venv fallback)."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Temp APM project with .github/ for VSCode target detection."""
    project_dir = tmp_path / "dry-run-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=180):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_apm_yml(project_dir, apm_packages, mcp_packages=None):
    config = {
        "name": "dry-run-test",
        "version": "1.0.0",
        "dependencies": {
            "apm": apm_packages,
            "mcp": mcp_packages or [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _assert_no_install_artifacts(project_dir):
    """Dry-run must not create lockfile or deploy any files."""
    assert not (project_dir / "apm.lock.yaml").exists(), "Dry-run created apm.lock.yaml"
    assert not (project_dir / "apm.lock").exists(), "Dry-run created legacy apm.lock"
    assert not (project_dir / "apm_modules").exists(), "Dry-run populated apm_modules/"
    copilot_instructions = project_dir / ".github" / "copilot-instructions.md"
    assert not copilot_instructions.exists(), "Dry-run wrote .github/copilot-instructions.md"


class TestInstallDryRunE2E:
    """End-to-end coverage for `apm install --dry-run`."""

    def test_install_dry_run_lists_apm_dependencies_without_changes(
        self, temp_project, apm_command
    ):
        """Dry-run prints the preview banner, lists the APM dep, and writes nothing."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["install", "--dry-run"], temp_project)
        assert result.returncode == 0, (
            f"Dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out, f"Missing 'Dry run mode' banner:\n{out}"
        assert "Dry run complete" in out, f"Missing 'Dry run complete' footer:\n{out}"
        assert "APM dependencies" in out, f"Missing APM dependencies header:\n{out}"
        assert "microsoft/apm-sample-package" in out, (
            f"Dep repo_url not mentioned in dry-run output:\n{out}"
        )

        _assert_no_install_artifacts(temp_project)

    def test_install_dry_run_with_only_packages_filter(self, temp_project, apm_command):
        """`--only=apm` suppresses MCP-dependency listing in the dry-run preview."""
        _write_apm_yml(
            temp_project,
            apm_packages=["microsoft/apm-sample-package"],
            mcp_packages=["io.github.github/github-mcp-server"],
        )

        result = _run_apm(apm_command, ["install", "--dry-run", "--only=apm"], temp_project)
        assert result.returncode == 0, (
            f"Filtered dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out
        assert "APM dependencies" in out, f"APM section missing under --only=apm:\n{out}"
        assert "microsoft/apm-sample-package" in out
        assert "MCP dependencies" not in out, (
            f"MCP section should be hidden under --only=apm:\n{out}"
        )
        assert "github-mcp-server" not in out, f"MCP dep leaked into --only=apm dry-run:\n{out}"

        _assert_no_install_artifacts(temp_project)

    def test_install_dry_run_previews_orphan_removals(self, temp_project, apm_command):
        """After a real install, removing the dep + dry-run reports orphan files
        and keeps them on disk (the orphan-preview NameError regression test)."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        real = _run_apm(apm_command, ["install"], temp_project)
        assert real.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {real.stdout}\nSTDERR: {real.stderr}"
        )

        lock_path = temp_project / "apm.lock.yaml"
        assert lock_path.exists(), "apm.lock.yaml not created by initial install"
        with open(lock_path, encoding="utf-8") as f:
            lockfile = yaml.safe_load(f)

        deployed_files = []
        for entry in lockfile.get("dependencies") or []:
            if entry.get("repo_url") == "microsoft/apm-sample-package":
                deployed_files = [
                    f for f in (entry.get("deployed_files") or []) if (temp_project / f).exists()
                ]
                break
        if not deployed_files:
            pytest.skip("apm-sample-package deployed no files; cannot verify orphans")

        _write_apm_yml(temp_project, [])

        result = _run_apm(apm_command, ["install", "--dry-run"], temp_project)
        assert result.returncode == 0, (
            f"Orphan dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out
        assert "Dry run complete" in out
        assert "Files that would be removed" in out, f"Orphan-removal preview missing:\n{out}"

        for rel_path in deployed_files:
            full = temp_project / rel_path
            assert full.exists(), f"Dry-run unexpectedly deleted orphan file: {rel_path}"
