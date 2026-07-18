"""End-to-end integration tests for ``apm install -g`` / ``apm uninstall -g``.

Covers gaps that existing scope tests do not exercise:
- G1: real package install under user scope deploys primitive files to ~/.apm/
- U1: uninstall under user scope removes deployed files from ~/.apm/
- Cross-scope coexistence: a global install and a project install of the same
  package live side by side without colliding.

Uses an owned local Git origin exposed as ``microsoft/apm-sample-package`` so
the user-scope lifecycle stays hermetic while preserving GitHub-shaped
lockfile provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.hermetic_packaged_sample import DEPENDENCY, HermeticPackagedSample

pytestmark = [
    pytest.mark.requires_apm_binary,
]


SAMPLE_PKG = DEPENDENCY
SAMPLE_REMOTE_URL = f"https://github.com/{SAMPLE_PKG}"


def _home(packaged_sample: HermeticPackagedSample) -> Path:
    return Path(packaged_sample.environment["HOME"])


def _apm_home(packaged_sample: HermeticPackagedSample) -> Path:
    return Path(packaged_sample.environment["APM_HOME"])


def _run_apm(
    packaged_sample: HermeticPackagedSample,
    args: tuple[str, ...],
    *,
    cwd: Path,
    scenario_id: str,
) -> CommandResult:
    return packaged_sample.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=cwd,
        env=packaged_sample.environment,
    )


def _ensure_home_target_signal(packaged_sample: HermeticPackagedSample) -> None:
    home_dir = _home(packaged_sample)
    github_dir = home_dir / ".github"
    github_dir.mkdir(exist_ok=True)
    (github_dir / "copilot-instructions.md").write_text("# test\n", encoding="utf-8")


def _write_user_manifest(packaged_sample: HermeticPackagedSample, packages: list[object]) -> None:
    """Seed ~/.apm/apm.yml with the given APM dependency list."""
    _ensure_home_target_signal(packaged_sample)
    apm_dir = _apm_home(packaged_sample)
    apm_dir.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {
            "name": "global-project",
            "version": "1.0.0",
            "dependencies": {"apm": packages, "mcp": []},
        },
        apm_dir / "apm.yml",
    )


def _read_lockfile(directory: Path) -> dict[str, object] | None:
    lock_path = directory / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    lockfile = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert lockfile is None or isinstance(lockfile, dict)
    return lockfile


def _get_locked_dep(
    lockfile: dict[str, object] | None,
    repo_url: str,
) -> dict[str, object] | None:
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            assert isinstance(entry, dict)
            if entry.get("repo_url") == repo_url:
                return entry
    return None


def _existing_deployed_files(
    deploy_root: Path,
    dep_entry: dict[str, object] | None,
) -> list[str]:
    """Return deployed_files entries that exist on disk under *deploy_root*.

    User-scope deploy_root is ``~/`` (Path.home()), not ``~/.apm/``: integrators
    write to paths like ``~/.copilot/agents/...`` while metadata lives in
    ``~/.apm/``. See ``apm_cli.core.scope.get_deploy_root``.
    """
    if not dep_entry or not dep_entry.get("deployed_files"):
        return []
    return [f for f in dep_entry["deployed_files"] if (deploy_root / f).exists()]


def _make_workdir(packaged_sample: HermeticPackagedSample, name: str) -> Path:
    work_dir = packaged_sample.project.root.parent / name
    work_dir.mkdir()
    return work_dir


class TestGlobalInstallDeploysRealPackage:
    """Verify `apm install -g` actually deploys primitive files under ~/.apm/."""

    def test_install_global_deploys_real_package_to_user_scope(
        self,
        hermetic_packaged_sample: HermeticPackagedSample,
    ) -> None:
        _write_user_manifest(hermetic_packaged_sample, [{"git": SAMPLE_REMOTE_URL}])
        work_dir = _make_workdir(hermetic_packaged_sample, "global-install-workdir")

        result = _run_apm(
            hermetic_packaged_sample,
            ("install", "-g"),
            cwd=work_dir,
            scenario_id="global-install-user-scope",
        )
        assert result.returncode == 0, (
            f"global install failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        apm_dir = _apm_home(hermetic_packaged_sample)
        lockfile = _read_lockfile(apm_dir)
        assert lockfile is not None, "~/.apm/apm.lock.yaml was not created"
        dep = _get_locked_dep(lockfile, SAMPLE_PKG)
        assert dep is not None, f"{SAMPLE_PKG} not present in user-scope lockfile: {lockfile}"

        deployed = _existing_deployed_files(_home(hermetic_packaged_sample), dep)
        assert len(deployed) > 0, (
            f"No primitive files deployed under user-scope deploy root. "
            f"deployed_files={dep.get('deployed_files')}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        # Cross-scope leakage check: the working directory must be untouched.
        assert not (work_dir / "apm.yml").exists(), "apm.yml leaked into cwd"
        assert not (work_dir / "apm.lock.yaml").exists(), "lockfile leaked into cwd"
        assert not (work_dir / "apm_modules").exists(), "apm_modules leaked into cwd"

    def test_uninstall_global_removes_deployed_files(
        self,
        hermetic_packaged_sample: HermeticPackagedSample,
    ) -> None:
        _write_user_manifest(hermetic_packaged_sample, [{"git": SAMPLE_REMOTE_URL}])
        work_dir = _make_workdir(hermetic_packaged_sample, "global-uninstall-workdir")

        install_result = _run_apm(
            hermetic_packaged_sample,
            ("install", "-g"),
            cwd=work_dir,
            scenario_id="global-install-before-uninstall",
        )
        assert install_result.returncode == 0, (
            f"setup install failed:\nSTDOUT: {install_result.stdout}\n"
            f"STDERR: {install_result.stderr}"
        )

        apm_dir = _apm_home(hermetic_packaged_sample)
        dep_before = _get_locked_dep(_read_lockfile(apm_dir), SAMPLE_PKG)
        assert dep_before is not None, "Package missing from lockfile after install"
        deployed_before = _existing_deployed_files(_home(hermetic_packaged_sample), dep_before)
        assert deployed_before, "Fixture package must deploy user-scope files before uninstall"

        uninstall_result = _run_apm(
            hermetic_packaged_sample,
            ("uninstall", SAMPLE_PKG, "-g"),
            cwd=work_dir,
            scenario_id="global-uninstall-user-scope",
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
            assert not (_home(hermetic_packaged_sample) / rel_path).exists(), (
                f"Deployed file {rel_path} not removed by uninstall -g"
            )

    def test_install_global_then_project_install_does_not_collide(
        self,
        hermetic_packaged_sample: HermeticPackagedSample,
    ) -> None:
        # Install globally first.
        _write_user_manifest(hermetic_packaged_sample, [{"git": SAMPLE_REMOTE_URL}])
        global_workdir = _make_workdir(hermetic_packaged_sample, "global-coexistence-workdir")
        global_result = _run_apm(
            hermetic_packaged_sample,
            ("install", "-g"),
            cwd=global_workdir,
            scenario_id="global-install-before-project",
        )
        assert global_result.returncode == 0, (
            f"global install failed:\nSTDOUT: {global_result.stdout}\n"
            f"STDERR: {global_result.stderr}"
        )

        apm_dir = _apm_home(hermetic_packaged_sample)
        global_dep = _get_locked_dep(_read_lockfile(apm_dir), SAMPLE_PKG)
        assert global_dep is not None, "Global lockfile missing the package"

        project_dir = hermetic_packaged_sample.project.root
        local_result = _run_apm(
            hermetic_packaged_sample,
            ("install",),
            cwd=project_dir,
            scenario_id="project-install-after-global",
        )
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
