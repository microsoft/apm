"""Hermetic packaged-binary lifecycle for one virtual instruction file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_OWNER = "apm-fixture-org"
_REPO_NAME = "packaged-virtual-file"
_VIRTUAL_PATH = "packages/guardrail"
_REMOTE_URL = f"https://github.com/{_OWNER}/{_REPO_NAME}"
_DEPENDENCY = f"{_OWNER}/{_REPO_NAME}/{_VIRTUAL_PATH}#main"
_INSTRUCTION_BYTES = (
    b"---\napplyTo: '**'\ndescription: Hermetic packaged virtual instruction\n---\n# Guard\n"
)


def test_packaged_binary_installs_compiles_and_locks_virtual_file(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    inherited = {
        **os.environ,
        "GITHUB_APM_PAT": "ambient-github-token",
        "GITHUB_TOKEN": "ambient-actions-token",
        "ADO_APM_PAT": "ambient-ado-token",
    }
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=inherited)
    environment = isolated.subprocess_env()
    assert {
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "ADO_APM_PAT",
    }.isdisjoint(environment)

    source = isolated.package_root / _REPO_NAME
    source_packages = LocalPackageFactory(source / "packages")
    source_package = source_packages.create(
        "guardrail",
        targets=("copilot",),
    )
    source_packages.add_instruction(
        source_package,
        "guard",
        _INSTRUCTION_BYTES.decode(),
    )

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(_REPO_NAME, source_tree=source)
    commit = repositories.commit(repository, message="seed packaged virtual file")
    child_env = repositories.url_rewrite_subprocess_env(repository, _REMOTE_URL)
    assert child_env["GIT_ALLOW_PROTOCOL"] == "file"

    project = LocalPackageFactory(isolated.work_root).create(
        "packaged-virtual-file-consumer",
        dependencies=(_DEPENDENCY,),
        targets=("copilot",),
    )
    install, compile_result = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=240,
    ).run_sequence(
        (
            (
                "install",
                "--target",
                "copilot",
                "--no-policy",
                "--parallel-downloads",
                "0",
            ),
            ("compile", "--target", "copilot", "--force-instructions"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="packaged-virtual-file",
        cwd=project.root,
        env=child_env,
    )
    assert install.returncode == 0
    assert compile_result.returncode == 0

    deployed = project.root / ".github" / "instructions" / "guard.instructions.md"
    assert deployed.read_bytes() == _INSTRUCTION_BYTES

    dependencies = load_yaml(project.root / "apm.lock.yaml")["dependencies"]
    assert len(dependencies) == 1
    locked = dependencies[0]
    module_root = (
        LockedDependency.from_dict(locked)
        .to_dependency_ref()
        .get_install_path(project.root / "apm_modules")
    )
    assert locked["virtual_path"] == _VIRTUAL_PATH
    assert locked["resolved_commit"] == commit.sha
    assert locked["content_hash"] == compute_package_hash(module_root)
