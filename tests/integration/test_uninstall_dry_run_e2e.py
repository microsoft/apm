"""End-to-end integration tests for `apm uninstall --dry-run`.

Covers gap U2: dry-run preview must list what would be removed without
mutating apm.yml, apm.lock.yaml, or any deployed files on disk.

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
Uses the real microsoft/apm-sample-package.
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
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "uninstall-dry-run-test"
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
        "name": "uninstall-dry-run-test",
        "version": "1.0.0",
        "dependencies": {"apm": packages, "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _snapshot_files(project_dir):
    """Return a set of relative file paths under project_dir."""
    files = set()
    for path in project_dir.rglob("*"):
        if path.is_file():
            files.add(path.relative_to(project_dir).as_posix())
    return files


SAMPLE_PKG = "microsoft/apm-sample-package#main"


def test_uninstall_dry_run_lists_files_without_removing(apm_command, temp_project):
    _write_apm_yml(temp_project, [SAMPLE_PKG])

    install = _run_apm(apm_command, ["install"], temp_project)
    assert install.returncode == 0, f"install failed: {install.stderr}\n{install.stdout}"

    apm_yml_before = (temp_project / "apm.yml").read_text(encoding="utf-8")
    lock_path = temp_project / "apm.lock.yaml"
    assert lock_path.exists(), "lockfile should exist after install"
    lock_before = lock_path.read_text(encoding="utf-8")
    assert "apm-sample-package" in lock_before
    files_before = _snapshot_files(temp_project)

    result = _run_apm(
        apm_command,
        ["uninstall", "microsoft/apm-sample-package", "--dry-run"],
        temp_project,
    )
    assert result.returncode == 0, f"dry-run failed: {result.stderr}\n{result.stdout}"

    combined = result.stdout + result.stderr
    assert "Dry run" in combined or "dry run" in combined.lower()
    assert "microsoft/apm-sample-package" in combined
    assert "no changes made" in combined.lower()

    files_after = _snapshot_files(temp_project)
    missing = files_before - files_after
    assert not missing, f"dry-run removed files: {sorted(missing)}"

    assert (temp_project / "apm.yml").read_text(encoding="utf-8") == apm_yml_before
    assert lock_path.read_text(encoding="utf-8") == lock_before
    assert "apm-sample-package" in lock_path.read_text(encoding="utf-8")


def test_uninstall_dry_run_with_unknown_package(apm_command, temp_project):
    _write_apm_yml(temp_project, [SAMPLE_PKG])

    install = _run_apm(apm_command, ["install"], temp_project)
    assert install.returncode == 0, f"install failed: {install.stderr}\n{install.stdout}"

    files_before = _snapshot_files(temp_project)
    apm_yml_before = (temp_project / "apm.yml").read_text(encoding="utf-8")

    result = _run_apm(
        apm_command,
        ["uninstall", "some/nonexistent", "--dry-run"],
        temp_project,
    )
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower(), (
        f"expected 'not found' warning for unknown package\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    files_after = _snapshot_files(temp_project)
    assert files_after == files_before, "no files should change for unknown package dry-run"
    assert (temp_project / "apm.yml").read_text(encoding="utf-8") == apm_yml_before
    lock_path = temp_project / "apm.lock.yaml"
    assert lock_path.exists() and "apm-sample-package" in lock_path.read_text(encoding="utf-8")
