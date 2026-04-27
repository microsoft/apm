"""End-to-end integration tests for multi-package `apm uninstall`.

Covers gap U3: `apm uninstall pkg1 pkg2 ...` is documented but never
integration-tested. The engine handles list iteration; today only single-pkg
paths are tested.

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
Uses two real public APM packages from GitHub:
  - microsoft/apm-sample-package
  - github/awesome-copilot/skills/aspire
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


PKG_A = "microsoft/apm-sample-package"
PKG_B = "github/awesome-copilot/skills/aspire"


@pytest.fixture
def apm_command():
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "uninstall-multi-test"
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


def _write_apm_yml(project_dir, packages):
    config = {
        "name": "uninstall-multi-test",
        "version": "1.0.0",
        "dependencies": {"apm": packages, "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _read_yaml(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lock_dep_keys(lockfile):
    """Return the set of dependency identifiers present in the lockfile."""
    if not lockfile or "dependencies" not in lockfile:
        return set()
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        return {entry.get("repo_url", "") for entry in deps if isinstance(entry, dict)}
    if isinstance(deps, dict):
        return set(deps.keys())
    return set()


def _deployed_files_for(lockfile, repo_substr):
    """Return deployed_files for first lockfile dep whose identifier matches substr."""
    if not lockfile or "dependencies" not in lockfile:
        return []
    deps = lockfile["dependencies"]
    entries = deps.values() if isinstance(deps, dict) else deps
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("repo_url", "")
        if repo_substr in ident:
            return entry.get("deployed_files", []) or []
    return []


class TestUninstallMultiplePackages:
    """Verify that `apm uninstall pkg1 pkg2` removes both in a single command."""

    def test_uninstall_multiple_packages_in_one_command(self, temp_project, apm_command):
        _write_apm_yml(temp_project, [PKG_A, PKG_B])
        result_install = _run_apm(apm_command, ["install"], temp_project)
        assert result_install.returncode == 0, (
            f"Install failed:\nSTDOUT: {result_install.stdout}\nSTDERR: {result_install.stderr}"
        )

        lockfile_before = _read_yaml(temp_project / "apm.lock.yaml")
        files_a_before = [
            f
            for f in _deployed_files_for(lockfile_before, "apm-sample-package")
            if (temp_project / f).exists()
        ]
        files_b_before = [
            f
            for f in _deployed_files_for(lockfile_before, "awesome-copilot")
            if (temp_project / f).exists()
        ]
        if not files_a_before or not files_b_before:
            pytest.skip("One of the packages deployed no files; cannot verify cleanup")

        result_un = _run_apm(apm_command, ["uninstall", PKG_A, PKG_B], temp_project)
        assert result_un.returncode == 0, (
            f"Uninstall failed:\nSTDOUT: {result_un.stdout}\nSTDERR: {result_un.stderr}"
        )

        manifest_after = _read_yaml(temp_project / "apm.yml")
        apm_deps_after = manifest_after.get("dependencies", {}).get("apm") or []
        deps_text = yaml.dump(apm_deps_after)
        assert "apm-sample-package" not in deps_text, (
            f"PKG_A still in apm.yml after multi-uninstall: {apm_deps_after}"
        )
        assert "awesome-copilot" not in deps_text, (
            f"PKG_B still in apm.yml after multi-uninstall: {apm_deps_after}"
        )

        lockfile_after = _read_yaml(temp_project / "apm.lock.yaml")
        keys_after = _lock_dep_keys(lockfile_after)
        joined_keys = " ".join(keys_after)
        assert "apm-sample-package" not in joined_keys, (
            f"PKG_A still in apm.lock after multi-uninstall: {keys_after}"
        )
        assert "awesome-copilot" not in joined_keys, (
            f"PKG_B still in apm.lock after multi-uninstall: {keys_after}"
        )

        for rel_path in files_a_before + files_b_before:
            assert not (temp_project / rel_path).exists(), (
                f"Deployed file {rel_path} not cleaned up by multi-uninstall"
            )

    def test_uninstall_partial_unknown_continues_safely(self, temp_project, apm_command):
        """Engine warns on unknown package but still removes the known one (exit 0)."""
        _write_apm_yml(temp_project, [PKG_A])
        result_install = _run_apm(apm_command, ["install"], temp_project)
        assert result_install.returncode == 0, (
            f"Install failed:\nSTDOUT: {result_install.stdout}\nSTDERR: {result_install.stderr}"
        )

        result_un = _run_apm(
            apm_command,
            ["uninstall", PKG_A, "some/unknown-pkg-xyz789"],
            temp_project,
        )
        assert result_un.returncode == 0, (
            f"Partial-unknown uninstall failed:\nSTDOUT: {result_un.stdout}\nSTDERR: {result_un.stderr}"
        )

        combined = (result_un.stdout + result_un.stderr).lower()
        assert "not found" in combined or "unknown" in combined or "warning" in combined, (
            f"Expected a not-found warning for unknown package; output:\n{result_un.stdout}\n{result_un.stderr}"
        )

        manifest_after = _read_yaml(temp_project / "apm.yml")
        apm_deps_after = manifest_after.get("dependencies", {}).get("apm") or []
        assert "apm-sample-package" not in yaml.dump(apm_deps_after), (
            f"Known package not removed when batched with unknown one: {apm_deps_after}"
        )
