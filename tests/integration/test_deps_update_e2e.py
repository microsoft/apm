"""End-to-end integration tests for the `apm deps update` CLI command.

Covers gaps Up1, Up2, Up3, G3 -- canonical update workflows that previously
had zero CLI-level coverage:

  Up1: `apm deps update` (no args) bumps the lockfile SHA across all packages
  Up2: `apm deps update <pkg>` updates only the named package
  Up3: `apm deps update -g` updates user-scope dependencies under ~/.apm/
  G3:  unknown package argument exits non-zero with helpful message

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
Uses real packages from GitHub:
  - microsoft/apm-sample-package
  - github/awesome-copilot/skills/aspire (only for selective-update test)
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


SAMPLE_REPO_URL = "microsoft/apm-sample-package"
SAMPLE_GIT_URL = "https://github.com/microsoft/apm-sample-package.git"
# Initial commit of microsoft/apm-sample-package (older than current main).
OLD_SHA = "318a8439"
NEWER_REF = "main"


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
    """Create a temporary APM project with a .github/ marker."""
    project_dir = tmp_path / "deps-update-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


@pytest.fixture
def fake_home(tmp_path):
    """Isolated HOME for user-scope tests."""
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    return home_dir


def _env_with_home(fake_home):
    """Return an env dict with HOME/USERPROFILE pointing to *fake_home*."""
    import sys

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    return env


def _run_apm(apm_command, args, cwd, env=None, timeout=180):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env if env is not None else os.environ.copy(),
    )


def _write_apm_yml(target_dir, packages):
    """Write apm.yml at *target_dir* with the given list of APM package specs."""
    config = {
        "name": "deps-update-test",
        "version": "1.0.0",
        "dependencies": {"apm": packages, "mcp": []},
    }
    (target_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _read_lockfile(lock_dir):
    """Read and parse apm.lock.yaml from *lock_dir*."""
    lock_path = lock_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_locked_dep(lockfile, repo_url):
    """Return the lockfile entry for *repo_url* (or None)."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            if entry.get("repo_url") == repo_url:
                return entry
    return None


# ---------------------------------------------------------------------------
# Up1: `apm deps update` bumps SHA for all packages after a ref change
# ---------------------------------------------------------------------------


def test_deps_update_all_packages_bumps_lockfile_sha(temp_project, apm_command):
    """`apm deps update` (no args) re-resolves refs and bumps the lockfile SHA."""
    # Step 1: install pinned to an older commit SHA.
    _write_apm_yml(temp_project, [{"git": SAMPLE_GIT_URL, "ref": OLD_SHA}])
    result1 = _run_apm(apm_command, ["install"], temp_project)
    assert result1.returncode == 0, (
        f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
    )
    lockfile1 = _read_lockfile(temp_project)
    dep1 = _get_locked_dep(lockfile1, SAMPLE_REPO_URL)
    assert dep1 is not None, "Sample package missing from lockfile after install"
    old_commit = dep1.get("resolved_commit")
    assert old_commit, "No resolved_commit recorded for initial install"
    deployed_before = list(dep1.get("deployed_files") or [])
    assert deployed_before, "No deployed files recorded -- cannot verify update"

    # Step 2: bump apm.yml to point at main.
    _write_apm_yml(temp_project, [{"git": SAMPLE_GIT_URL, "ref": NEWER_REF}])

    # Step 3: run `apm deps update` with no positional args.
    result2 = _run_apm(apm_command, ["deps", "update"], temp_project)
    assert result2.returncode == 0, (
        f"deps update failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
    )

    # Step 4: lockfile SHA must differ from old.
    lockfile2 = _read_lockfile(temp_project)
    dep2 = _get_locked_dep(lockfile2, SAMPLE_REPO_URL)
    assert dep2 is not None, "Sample package disappeared from lockfile after update"
    new_commit = dep2.get("resolved_commit")
    assert new_commit, "No resolved_commit recorded after update"
    assert new_commit != old_commit, (
        f"Lockfile SHA did not change after deps update: {old_commit} == {new_commit}"
    )

    # Step 5: deployed files must still exist (re-integrated).
    package_dir = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
    assert package_dir.exists(), "Package directory missing after update"
    redeployed = [f for f in (dep2.get("deployed_files") or []) if (temp_project / f).exists()]
    assert redeployed, "No deployed files exist after update -- re-integration failed"


# ---------------------------------------------------------------------------
# Up2: `apm deps update <pkg>` updates only the named package
# ---------------------------------------------------------------------------


def test_deps_update_single_package_selective(temp_project, apm_command):
    """`apm deps update <pkg>` should accept the selective filter and succeed.

    With two packages installed, requesting an update for one must succeed and
    must not error on the unrelated package.
    """
    _write_apm_yml(
        temp_project,
        [
            {"git": SAMPLE_GIT_URL, "ref": OLD_SHA},
            "github/awesome-copilot/skills/aspire",
        ],
    )
    result1 = _run_apm(apm_command, ["install"], temp_project)
    assert result1.returncode == 0, (
        f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
    )
    lockfile1 = _read_lockfile(temp_project)
    dep_sample_before = _get_locked_dep(lockfile1, SAMPLE_REPO_URL)
    assert dep_sample_before is not None, "sample package not in initial lockfile"
    sample_old_sha = dep_sample_before.get("resolved_commit")

    # Bump the sample package ref so a real update is possible.
    _write_apm_yml(
        temp_project,
        [
            {"git": SAMPLE_GIT_URL, "ref": NEWER_REF},
            "github/awesome-copilot/skills/aspire",
        ],
    )

    result2 = _run_apm(
        apm_command,
        ["deps", "update", SAMPLE_REPO_URL],
        temp_project,
    )
    assert result2.returncode == 0, (
        f"Selective deps update failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
    )

    # The sample package SHA should change (since we bumped its ref).
    lockfile2 = _read_lockfile(temp_project)
    dep_sample_after = _get_locked_dep(lockfile2, SAMPLE_REPO_URL)
    assert dep_sample_after is not None, "sample package missing after selective update"
    sample_new_sha = dep_sample_after.get("resolved_commit")
    assert sample_new_sha and sample_old_sha and sample_new_sha != sample_old_sha, (
        f"Selected package SHA did not change: {sample_old_sha} -> {sample_new_sha}"
    )


# ---------------------------------------------------------------------------
# Up3: `apm deps update -g` updates user-scope deps under ~/.apm/
# ---------------------------------------------------------------------------


def test_deps_update_global_user_scope(tmp_path, fake_home, apm_command):
    """`apm deps update -g` must update ~/.apm/apm.lock.yaml, not cwd lockfile.

    Regression guard: a historical bug deployed silently to the project even
    when --global was set. cli.py:601-611 now passes scope=USER through.
    """
    # Create the user manifest with an older pinned commit.
    apm_dir = fake_home / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    user_manifest = apm_dir / "apm.yml"

    def _write_user_manifest(ref):
        user_manifest.write_text(
            yaml.dump(
                {
                    "name": "global-deps-update-test",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"git": SAMPLE_GIT_URL, "ref": ref}],
                        "mcp": [],
                    },
                }
            ),
            encoding="utf-8",
        )

    _write_user_manifest(OLD_SHA)

    env = _env_with_home(fake_home)

    # Use a separate cwd that has NO project manifest, to confirm scope=USER
    # is honored.
    work_dir = tmp_path / "outside-project"
    work_dir.mkdir()

    # Step 1: install -g to populate ~/.apm/apm.lock.yaml.
    result1 = _run_apm(apm_command, ["install", "-g"], work_dir, env=env)
    assert result1.returncode == 0, (
        f"Global install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
    )
    user_lockfile1 = _read_lockfile(apm_dir)
    assert user_lockfile1 is not None, "~/.apm/apm.lock.yaml not created by install -g"
    dep1 = _get_locked_dep(user_lockfile1, SAMPLE_REPO_URL)
    assert dep1 is not None, "package missing from user-scope lockfile"
    old_commit = dep1.get("resolved_commit")
    assert old_commit, "no resolved_commit in user-scope lockfile"

    # Step 2: bump the user manifest to main.
    _write_user_manifest(NEWER_REF)

    # Step 3: run `apm deps update -g` from a directory with no project.
    result2 = _run_apm(apm_command, ["deps", "update", "-g"], work_dir, env=env)
    assert result2.returncode == 0, (
        f"deps update -g failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
    )

    # Step 4: ~/.apm/apm.lock.yaml must reflect the new SHA.
    user_lockfile2 = _read_lockfile(apm_dir)
    assert user_lockfile2 is not None, "~/.apm/apm.lock.yaml missing after update -g"
    dep2 = _get_locked_dep(user_lockfile2, SAMPLE_REPO_URL)
    assert dep2 is not None, "package disappeared from user-scope lockfile after update"
    new_commit = dep2.get("resolved_commit")
    assert new_commit and new_commit != old_commit, (
        f"User-scope lockfile SHA did not change: {old_commit} -> {new_commit}"
    )

    # Step 5: scope was respected -- no project lockfile in cwd.
    assert not (work_dir / "apm.lock.yaml").exists(), (
        "apm.lock.yaml leaked into cwd -- scope=USER not honored"
    )
    assert not (work_dir / "apm.lock").exists(), (
        "Legacy apm.lock leaked into cwd -- scope=USER not honored"
    )
    assert not (work_dir / "apm.yml").exists(), "apm.yml leaked into cwd -- scope=USER not honored"


# ---------------------------------------------------------------------------
# G3: unknown package argument exits non-zero
# ---------------------------------------------------------------------------


def test_deps_update_unknown_package_errors(temp_project, apm_command):
    """`apm deps update <unknown>` should exit non-zero with a helpful error."""
    _write_apm_yml(temp_project, [SAMPLE_REPO_URL])
    result_install = _run_apm(apm_command, ["install"], temp_project)
    assert result_install.returncode == 0, (
        f"Initial install failed:\nSTDOUT: {result_install.stdout}\nSTDERR: {result_install.stderr}"
    )

    result = _run_apm(
        apm_command,
        ["deps", "update", "some/nonexistent-package"],
        temp_project,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit for unknown package, got 0\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "not found in" in combined, (
        f"Expected 'not found in' in error output, got:\n{result.stdout}\n{result.stderr}"
    )
