"""End-to-end integration tests for diff-aware apm install.

Tests the complete manifest-as-source-of-truth lifecycle with real packages:
- Package removed from apm.yml: apm install cleans up deployed files and lockfile
- Package ref/version changed in apm.yml: apm install re-downloads without --update
- MCP config drift: apm install re-applies changed MCP server config (unit-tested;
  omitted from e2e since it requires a real runtime to be configured)

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
Uses real packages from GitHub:
  - microsoft/apm-sample-package (deployed prompts, agents, etc.)
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# Skip all tests if no GitHub token is available
pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project with .github/ for VSCode target detection."""
    project_dir = tmp_path / "diff-aware-install-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=180):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_apm_yml(project_dir, packages):
    """Write apm.yml with the given list of APM package specs."""
    config = {
        "name": "diff-aware-test",
        "version": "1.0.0",
        "dependencies": {
            "apm": packages,
            "mcp": [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _read_lockfile(project_dir):
    """Read and parse apm.lock from the project directory."""
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_locked_dep(lockfile, repo_url):
    """Get a dependency entry from lockfile by repo_url."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            if entry.get("repo_url") == repo_url:
                return entry
    return None


def _collect_deployed_files(project_dir, dep_entry):
    """Return existing deployed files from a lockfile dep entry."""
    if not dep_entry or not dep_entry.get("deployed_files"):
        return []
    return [f for f in dep_entry["deployed_files"] if (project_dir / f).exists()]


# ---------------------------------------------------------------------------
# Scenario 1: Package removed from manifest — apm install cleans up
# ---------------------------------------------------------------------------


class TestPackageRemovedFromManifest:
    """When a package is removed from apm.yml, apm install should clean up
    its deployed files and remove it from the lockfile."""

    def test_removed_package_files_cleaned_on_install(self, temp_project, apm_command):
        """Files deployed by a removed package disappear on the next apm install."""
        # ── Step 1: install the package ──
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        # ── Step 2: verify deployed files exist and are tracked ──
        lockfile_before = _read_lockfile(temp_project)
        assert lockfile_before is not None, "apm.lock was not created"
        dep_before = _get_locked_dep(lockfile_before, "microsoft/apm-sample-package")
        assert dep_before is not None, "Package not in lockfile after install"
        deployed_before = _collect_deployed_files(temp_project, dep_before)
        assert len(deployed_before) > 0, "No deployed files found on disk — cannot verify cleanup"

        # ── Step 3: remove the package from manifest ──
        _write_apm_yml(temp_project, [])

        # ── Step 4: run apm install (no packages) — should detect orphan ──
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install after removal failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # ── Step 5: verify deployed files are gone ──
        for rel_path in deployed_before:
            full_path = temp_project / rel_path
            assert not full_path.exists(), (
                f"Orphaned file {rel_path} was NOT cleaned up by apm install"
            )

    def test_removed_package_absent_from_lockfile_after_install(self, temp_project, apm_command):
        """After removing a package from apm.yml, apm install removes it from lockfile."""
        # ── Install ──
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        # ── Remove from manifest ──
        _write_apm_yml(temp_project, [])

        # ── Re-install ──
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install after removal failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # ── Verify lockfile no longer has the removed package ──
        lockfile_after = _read_lockfile(temp_project)
        if lockfile_after and lockfile_after.get("dependencies"):
            dep_after = _get_locked_dep(lockfile_after, "microsoft/apm-sample-package")
            assert dep_after is None, "Removed package still present in apm.lock after apm install"

    def test_remaining_package_unaffected_by_removal(self, temp_project, apm_command):
        """Files from packages still in the manifest are untouched."""
        # ── Install two packages ──
        _write_apm_yml(
            temp_project,
            [
                "microsoft/apm-sample-package",
                "github/awesome-copilot/skills/aspire",
            ],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile_before = _read_lockfile(temp_project)
        sample_dep = _get_locked_dep(lockfile_before, "microsoft/apm-sample-package")
        if not sample_dep or not _collect_deployed_files(temp_project, sample_dep):
            pytest.skip("apm-sample-package deployed no files, cannot verify")

        # ── Remove only apm-sample-package ──
        _write_apm_yml(temp_project, ["github/awesome-copilot/skills/aspire"])
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # ── apm-sample-package files should be gone ──
        for rel_path in sample_dep.get("deployed_files") or []:
            # The files that were deployed should no longer exist
            assert not (temp_project / rel_path).exists(), (
                f"Removed package file {rel_path} still on disk"
            )


# ---------------------------------------------------------------------------
# Scenario 2: Package ref changed — apm install re-downloads
# ---------------------------------------------------------------------------


class TestPackageRefChangedInManifest:
    """When the ref in apm.yml changes, apm install re-downloads without --update."""

    def test_ref_change_triggers_re_download(self, temp_project, apm_command):
        """Changing the ref in apm.yml from one value to another causes re-download."""
        # ── Step 1: install with an explicit commit-pinned ref ──
        # We install first without a ref (using default branch), so the lockfile
        # records the resolved_ref as the default branch or latest commit.
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        assert lockfile1 is not None, "apm.lock was not created"
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        assert dep1 is not None, "Package not in lockfile"
        original_commit = dep1.get("resolved_commit")
        assert original_commit, "No resolved_commit in lockfile after install"

        # ── Step 2: change ref to "main" explicitly (from unset → explicit branch) ──
        # This differs from the lockfile's resolved_ref (which may be None/default).
        # For the test to be meaningful we pick a known ref that EXISTS in the repo.
        # We use "main" — the primary branch — which definitely exists.
        _write_apm_yml(
            temp_project,
            [{"git": "https://github.com/microsoft/apm-sample-package.git", "ref": "main"}],
        )

        # ── Step 3: run install WITHOUT --update ──
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install with changed ref failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # ── Step 4: verify the package was re-processed ──
        # Even if the commit hash is the same (main hasn't changed), the install
        # must not silently skip the package — it must re-evaluate the ref.
        # We verify the lockfile was updated and the package directory still exists.
        lockfile2 = _read_lockfile(temp_project)
        assert lockfile2 is not None, "apm.lock missing after second install"
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        assert dep2 is not None, "Package disappeared from lockfile after ref change"

        # The re-download should write back to lockfile; package dir must exist
        package_dir = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
        assert package_dir.exists(), (
            "Package directory disappeared after re-download for ref change"
        )

    def test_no_ref_change_does_not_re_download(self, temp_project, apm_command):
        """Without a ref change, apm install uses the lockfile SHA (idempotent)."""
        # ── Install ──
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        commit_before = dep1.get("resolved_commit") if dep1 else None

        # ── Re-install without changing the ref ──
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Re-install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # ── Commit should remain the same (lockfile pinned) ──
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        commit_after = dep2.get("resolved_commit") if dep2 else None

        if commit_before and commit_after:
            assert commit_before == commit_after, (
                f"Lockfile SHA changed without a ref change: {commit_before} → {commit_after}"
            )


# ---------------------------------------------------------------------------
# Scenario 3: Full install is idempotent when manifest unchanged
# ---------------------------------------------------------------------------


class TestFullInstallIdempotent:
    """Running apm install multiple times without manifest changes is safe."""

    def test_repeated_install_does_not_remove_files(self, temp_project, apm_command):
        """Repeated apm install with same manifest preserves deployed files."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"First install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        files_before = dep1.get("deployed_files", []) if dep1 else []

        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # All files from the first install must still exist
        for rel_path in files_before:
            assert (temp_project / rel_path).exists(), (
                f"File {rel_path} disappeared after idempotent re-install"
            )

        # Package must still be in lockfile
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        assert dep2 is not None, "Package missing from lockfile after idempotent re-install"
