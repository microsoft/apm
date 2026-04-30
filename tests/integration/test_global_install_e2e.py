"""End-to-end integration tests for `apm install -g` / `apm uninstall -g`.

Covers gaps that existing scope tests do not exercise:
- G1: real package install under user scope deploys primitive files to ~/.apm/
- U1: uninstall under user scope removes deployed files from ~/.apm/
- Cross-scope coexistence: a global install and a project install of the same
  package live side by side without colliding.

Uses the public `microsoft/apm-sample-package` repo (ref `main`) as the real
fixture, the same canonical sample referenced by other e2e suites.

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


SAMPLE_PKG = "microsoft/apm-sample-package"


@pytest.fixture
def apm_command():
    """Resolve the apm CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def fake_home(tmp_path):
    """Isolated HOME directory so user-scope installs never touch the real home."""
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    return home_dir


def _env_with_home(fake_home):
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    return env


def _run_apm(apm_command, args, cwd, fake_home, timeout=180):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env_with_home(fake_home),
    )


def _write_user_manifest(fake_home, packages):
    """Seed ~/.apm/apm.yml with the given APM dependency list."""
    apm_dir = fake_home / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    (apm_dir / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "global-project",
                "version": "1.0.0",
                "dependencies": {"apm": packages, "mcp": []},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def _read_lockfile(directory):
    lock_path = directory / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    return yaml.safe_load(lock_path.read_text(encoding="utf-8"))


def _get_locked_dep(lockfile, repo_url):
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            if entry.get("repo_url") == repo_url:
                return entry
    return None


def _existing_deployed_files(deploy_root, dep_entry):
    """Return deployed_files entries that exist on disk under *deploy_root*.

    User-scope deploy_root is ``~/`` (Path.home()), not ``~/.apm/``: integrators
    write to paths like ``~/.copilot/agents/...`` while metadata lives in
    ``~/.apm/``. See ``apm_cli.core.scope.get_deploy_root``.
    """
    if not dep_entry or not dep_entry.get("deployed_files"):
        return []
    return [f for f in dep_entry["deployed_files"] if (deploy_root / f).exists()]


class TestGlobalInstallDeploysRealPackage:
    """Verify `apm install -g` actually deploys primitive files under ~/.apm/."""

    def test_install_global_deploys_real_package_to_user_scope(
        self, apm_command, fake_home, tmp_path
    ):
        _write_user_manifest(fake_home, [SAMPLE_PKG])
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()

        result = _run_apm(apm_command, ["install", "-g"], work_dir, fake_home)
        assert result.returncode == 0, (
            f"global install failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        apm_dir = fake_home / ".apm"
        lockfile = _read_lockfile(apm_dir)
        assert lockfile is not None, "~/.apm/apm.lock.yaml was not created"
        dep = _get_locked_dep(lockfile, SAMPLE_PKG)
        assert dep is not None, f"{SAMPLE_PKG} not present in user-scope lockfile: {lockfile}"

        deployed = _existing_deployed_files(fake_home, dep)
        assert len(deployed) > 0, (
            f"No primitive files deployed under user-scope deploy root. "
            f"deployed_files={dep.get('deployed_files')}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        # Cross-scope leakage check: the working directory must be untouched.
        assert not (work_dir / "apm.yml").exists(), "apm.yml leaked into cwd"
        assert not (work_dir / "apm.lock.yaml").exists(), "lockfile leaked into cwd"
        assert not (work_dir / "apm_modules").exists(), "apm_modules leaked into cwd"

    def test_uninstall_global_removes_deployed_files(self, apm_command, fake_home, tmp_path):
        _write_user_manifest(fake_home, [SAMPLE_PKG])
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()

        install_result = _run_apm(apm_command, ["install", "-g"], work_dir, fake_home)
        assert install_result.returncode == 0, (
            f"setup install failed:\nSTDOUT: {install_result.stdout}\n"
            f"STDERR: {install_result.stderr}"
        )

        apm_dir = fake_home / ".apm"
        dep_before = _get_locked_dep(_read_lockfile(apm_dir), SAMPLE_PKG)
        assert dep_before is not None, "Package missing from lockfile after install"
        deployed_before = _existing_deployed_files(fake_home, dep_before)
        if not deployed_before:
            pytest.skip("Sample package deployed no files; nothing to verify removal of")

        uninstall_result = _run_apm(
            apm_command,
            ["uninstall", SAMPLE_PKG, "-g"],
            work_dir,
            fake_home,
        )
        assert uninstall_result.returncode == 0, (
            f"global uninstall failed:\nSTDOUT: {uninstall_result.stdout}\n"
            f"STDERR: {uninstall_result.stderr}"
        )

        # Lockfile should no longer contain the package entry.
        lockfile_after = _read_lockfile(apm_dir)
        if lockfile_after is not None:
            assert _get_locked_dep(lockfile_after, SAMPLE_PKG) is None, (
                "Package still in user-scope lockfile after uninstall"
            )

        # Manifest should no longer list the package.
        manifest_after = yaml.safe_load((apm_dir / "apm.yml").read_text(encoding="utf-8"))
        apm_deps = manifest_after.get("dependencies", {}).get("apm", []) or []
        assert SAMPLE_PKG not in apm_deps, (
            f"{SAMPLE_PKG} still in ~/.apm/apm.yml after uninstall: {apm_deps}"
        )

        # Previously deployed primitive files must be gone.
        for rel_path in deployed_before:
            assert not (fake_home / rel_path).exists(), (
                f"Deployed file {rel_path} not removed by uninstall -g"
            )

    def test_install_global_then_project_install_does_not_collide(
        self, apm_command, fake_home, tmp_path
    ):
        # Install globally first.
        _write_user_manifest(fake_home, [SAMPLE_PKG])
        global_workdir = tmp_path / "global-workdir"
        global_workdir.mkdir()
        global_result = _run_apm(apm_command, ["install", "-g"], global_workdir, fake_home)
        assert global_result.returncode == 0, (
            f"global install failed:\nSTDOUT: {global_result.stdout}\n"
            f"STDERR: {global_result.stderr}"
        )

        apm_dir = fake_home / ".apm"
        global_dep = _get_locked_dep(_read_lockfile(apm_dir), SAMPLE_PKG)
        assert global_dep is not None, "Global lockfile missing the package"

        # Now create a separate project and install the same package locally.
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".github").mkdir()
        (project_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "local-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": [SAMPLE_PKG], "mcp": []},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        local_result = _run_apm(apm_command, ["install"], project_dir, fake_home)
        assert local_result.returncode == 0, (
            f"project install failed:\nSTDOUT: {local_result.stdout}\nSTDERR: {local_result.stderr}"
        )

        # Both deployments must coexist.
        project_dep = _get_locked_dep(_read_lockfile(project_dir), SAMPLE_PKG)
        assert project_dep is not None, "Project lockfile missing the package"

        # Re-read the global lockfile and confirm it is still intact.
        global_dep_after = _get_locked_dep(_read_lockfile(apm_dir), SAMPLE_PKG)
        assert global_dep_after is not None, (
            "Global lockfile entry disappeared after project install"
        )
        assert (apm_dir / "apm_modules").exists(), (
            "Global apm_modules disappeared after project install"
        )
        assert (project_dir / "apm_modules").exists(), "Project apm_modules was not created"
