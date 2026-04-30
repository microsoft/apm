"""Integration tests for intra-package stale file cleanup on apm install (#666).

Covers renames and file removals inside a still-present package -- i.e. the
case where apm.yml still points at the package but the package's produced
file set has changed between installs.

Uses a throwaway local-path package fixture so these tests are fully local
and do not require GITHUB_APM_PAT / GITHUB_TOKEN.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def apm_command():
    """Path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Temporary APM project with .github/ for VSCode target detection."""
    project_dir = tmp_path / "intra-package-cleanup-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


@pytest.fixture
def local_pkg_root(tmp_path):
    """A throwaway APM package on disk with one prompt primitive."""
    pkg = tmp_path / "local-pkg"
    (pkg / ".apm" / "prompts").mkdir(parents=True)
    (pkg / "apm.yml").write_text(
        yaml.dump(
            {"name": "local-pkg", "version": "0.0.1"},
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    (pkg / ".apm" / "prompts" / "my-command.prompt.md").write_text(
        "---\ndescription: smoke\n---\nhello\n",
        encoding="utf-8",
    )
    return pkg


def _run_apm(apm_command, args, cwd, timeout=180):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_lockfile(project_dir):
    """Read and parse apm.lock.yaml from the project directory, or return None."""
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_apm_yml_local(project_dir, local_pkg_path):
    """Write apm.yml with a single local-path package dependency."""
    config = {
        "name": "intra-package-cleanup-test",
        "version": "1.0.0",
        "dependencies": {
            "apm": [{"path": str(local_pkg_path)}],
            "mcp": [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _find_local_dep(lockfile):
    """Return the locked-dep entry that represents a local-path package, or None.

    The lockfile stores dependencies either as a dict keyed by unique_key or
    as a list of entries. We tolerate either shape and identify the local
    dep by its `source == "local"` marker.
    """
    if not lockfile:
        return None
    deps = lockfile.get("dependencies") or {}
    entries = deps if isinstance(deps, list) else deps.values()
    for entry in entries:
        if entry and entry.get("source") == "local":
            return entry
    return None


class TestFileRenamedWithinPackage:
    """Regression tests for issue #666: renaming a file inside a still-present
    package must delete the stale deployed artifacts on the next apm install."""

    def test_renamed_file_cleanup_on_install(self, temp_project, apm_command, local_pkg_root):
        """Rename a source primitive, re-install, assert old files gone and
        lockfile deployed_files no longer lists the stale paths."""
        # -- Step 1: initial install --
        _write_apm_yml_local(temp_project, local_pkg_root)
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile_before = _read_lockfile(temp_project)
        assert lockfile_before is not None, "apm.lock was not created"
        dep_before = _find_local_dep(lockfile_before)
        assert dep_before is not None, "Local package not in lockfile"
        deployed_before = [
            f for f in (dep_before.get("deployed_files") or []) if (temp_project / f).exists()
        ]
        assert deployed_before, "No deployed files found -- cannot verify cleanup"
        old_files = list(deployed_before)

        # -- Step 2: rename the source primitive in place --
        src = local_pkg_root / ".apm" / "prompts" / "my-command.prompt.md"
        new = local_pkg_root / ".apm" / "prompts" / "my-new-command.prompt.md"
        src.rename(new)

        # -- Step 3: re-install --
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Re-install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Step 4: old deployed files must be gone --
        for rel_path in old_files:
            assert not (temp_project / rel_path).exists(), (
                f"Stale file {rel_path} was NOT cleaned up after rename"
            )

        # -- Step 5: lockfile deployed_files must not include the stale paths --
        lockfile_after = _read_lockfile(temp_project)
        dep_after = _find_local_dep(lockfile_after)
        assert dep_after is not None, "Local package disappeared from lockfile"
        deployed_after = dep_after.get("deployed_files") or []
        for stale in old_files:
            assert stale not in deployed_after, (
                f"Stale path {stale} still in lockfile deployed_files after cleanup"
            )

    def test_partial_install_cleans_renamed_file(self, temp_project, apm_command, local_pkg_root):
        """`apm install --only=apm` on a package with a renamed file still cleans up.

        Verifies that partial installs clean files for the packages they touch
        -- a deliberate departure from detect_orphans (package-level), which
        no-ops on partial installs."""
        # -- Step 1: initial install --
        _write_apm_yml_local(temp_project, local_pkg_root)
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, f"Initial install failed: {result1.stderr}"

        lockfile_before = _read_lockfile(temp_project)
        dep_before = _find_local_dep(lockfile_before)
        assert dep_before is not None
        old_files = [
            f for f in (dep_before.get("deployed_files") or []) if (temp_project / f).exists()
        ]
        assert old_files

        # -- Step 2: rename the source primitive in place --
        src = local_pkg_root / ".apm" / "prompts" / "my-command.prompt.md"
        new = local_pkg_root / ".apm" / "prompts" / "my-new-command.prompt.md"
        src.rename(new)

        # -- Step 3: partial install --
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, f"Partial re-install failed: {result2.stderr}"

        # -- Step 4: old deployed files must be gone --
        for rel_path in old_files:
            assert not (temp_project / rel_path).exists(), (
                f"Stale file {rel_path} survived partial install"
            )

        # -- Step 5: lockfile deployed_files must not include the stale paths --
        lockfile_after = _read_lockfile(temp_project)
        dep_after = _find_local_dep(lockfile_after)
        assert dep_after is not None, "Local package disappeared from lockfile"
        deployed_after = dep_after.get("deployed_files") or []
        for stale in old_files:
            assert stale not in deployed_after, (
                f"Stale path {stale} still in lockfile deployed_files after partial install"
            )
