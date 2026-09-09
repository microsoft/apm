"""Installed-CLI lifecycle coverage for retained revision pins."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_OWNER = "apm-fixture-org"
_INSTALL_ARGS = (
    "install",
    "--target",
    "claude",
    "--parallel-downloads",
    "0",
    "--no-policy",
)
_UPDATE_ARGS = (
    "update",
    "--target",
    "claude",
    "--parallel-downloads",
    "0",
)


@dataclass(frozen=True)
class _Scenario:
    """Owned repositories and consumer project for one revision-pin update."""

    environment: dict[str, str]
    consumer: LocalPackage
    repositories: LocalGitRepositoryFactory
    released_repository: LocalGitRepository
    released_old: GitCommit
    released_new: GitCommit
    retained: GitCommit


def _instruction(marker: str) -> str:
    """Return a minimal APM instruction document."""
    return f"---\napplyTo: '**'\n---\n# {marker}\n"


def _new_scenario(root: Path) -> _Scenario:
    """Create two GitHub-shaped local repositories and one pinned consumer."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)

    released_source = packages.create("released", targets=("claude",))
    released_instruction = packages.add_instruction(
        released_source,
        "released",
        _instruction("old"),
    )
    released_repo = repositories.create("released", source_tree=released_source.root)
    released_old = repositories.commit(released_repo, message="publish released old")
    released_repo_instruction = released_repo.worktree / released_instruction.relative_to(
        released_source.root
    )
    released_repo_instruction.write_text(_instruction("new"), encoding="utf-8")
    released_new = repositories.commit(released_repo, message="publish released new")
    repositories.tag(released_repo, "v2.0.0", released_new, annotated=True)

    retained_source = packages.create("retained", targets=("claude",))
    packages.add_instruction(retained_source, "retained", _instruction("unchanged"))
    retained_repo = repositories.create("retained", source_tree=retained_source.root)
    retained = repositories.commit(retained_repo, message="publish retained")

    released_url = f"https://github.com/{_OWNER}/released"
    retained_url = f"https://github.com/{_OWNER}/retained"
    environment = repositories.url_rewrite_subprocess_env_many(
        ((released_repo, released_url), (retained_repo, retained_url))
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "revision-pin-consumer",
        dependencies=(
            {"git": released_url, "ref": released_old.sha, "alias": "released"},
            {"git": retained_url, "ref": retained.sha, "alias": "retained"},
        ),
        targets=("claude",),
    )
    return _Scenario(
        environment=environment,
        consumer=consumer,
        repositories=repositories,
        released_repository=released_repo,
        released_old=released_old,
        released_new=released_new,
        retained=retained,
    )


def _run(
    apm_binary_path: Path,
    scenario: _Scenario,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI for one scenario."""
    return subprocess.run(
        (str(apm_binary_path), *_UPDATE_ARGS, *extra_args),
        cwd=scenario.consumer.root,
        env=scenario.environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _install(
    apm_binary_path: Path,
    scenario: _Scenario,
) -> subprocess.CompletedProcess[str]:
    """Install the pinned baseline before exercising update behavior."""
    return subprocess.run(
        (str(apm_binary_path), *_INSTALL_ARGS),
        cwd=scenario.consumer.root,
        env=scenario.environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_update_retains_unreleased_pin_in_dry_run_and_apply(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Dry-run and apply move only the pin backed by an eligible release tag."""
    scenario = _new_scenario(tmp_path / "lifecycle")
    manifest = scenario.consumer.manifest_path
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    manifest_before = manifest.read_bytes()
    lock_path = scenario.consumer.root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()

    dry_run = _run(apm_binary_path, scenario, "--dry-run")

    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "Retained 1 revision pin" in dry_run.stdout
    assert (
        f"{scenario.released_old.sha[:8]} -> {scenario.released_new.sha[:8]} (v2.0.0)"
        in dry_run.stdout
    )
    assert manifest.read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before

    applied = _run(apm_binary_path, scenario, "--yes")

    assert applied.returncode == 0, applied.stdout + applied.stderr
    manifest_text = manifest.read_text(encoding="utf-8")
    assert scenario.released_new.sha in manifest_text
    assert scenario.released_old.sha not in manifest_text
    assert scenario.retained.sha in manifest_text
    lock = load_yaml(lock_path)
    assert {entry["resolved_commit"] for entry in lock["dependencies"]} == {
        scenario.released_new.sha,
        scenario.retained.sha,
    }
    released_lock = next(
        entry
        for entry in lock["dependencies"]
        if entry["resolved_commit"] == scenario.released_new.sha
    )
    assert released_lock["resolved_ref"] == scenario.released_new.sha
    assert released_lock["resolved_tag"] == "v2.0.0"
    assert "constraint" not in released_lock


def _write_malformed_git_shim(shim_dir: Path, real_git: Path) -> None:
    """Write a git shim whose tag-only listing returns a null object ID."""
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
if "ls-remote" in arguments and "--tags" in arguments and "--heads" not in arguments:
    sys.stdout.write(("0" * 40) + "\\trefs/tags/v2.0.0\\n")
    raise SystemExit(0)
raise SystemExit(
    subprocess.run([os.environ["APM_TEST_REAL_GIT"], *arguments], env=os.environ).returncode
)
""",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    assert real_git.is_file()


def _publish_tree_tag(scenario: _Scenario) -> None:
    """Publish a higher annotated tag whose peeled object is a tree."""
    worktree = scenario.released_repository.worktree
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=worktree,
        env=scenario.environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "tag", "-a", "v3.0.0", tree, "-m", "Release v3.0.0"),
        cwd=worktree,
        env=scenario.environment,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ("git", "push", "origin", "refs/tags/v3.0.0"),
        cwd=worktree,
        env=scenario.environment,
        capture_output=True,
        text=True,
        check=True,
    )


def test_update_rejects_annotated_noncommit_before_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An annotated tag peeled to a tree cannot update project state."""
    scenario = _new_scenario(tmp_path / "noncommit")
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    _publish_tree_tag(scenario)
    manifest_before = scenario.consumer.manifest_path.read_bytes()
    lock_path = scenario.consumer.root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()

    result = _run(apm_binary_path, scenario, "--yes", "--verbose")

    assert result.returncode == 1, result.stdout + result.stderr
    assert scenario.consumer.manifest_path.read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before


def test_update_malformed_tag_output_exits_before_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Malformed tag output must fail without changing project artifacts."""
    scenario = _new_scenario(tmp_path / "malformed")
    manifest = scenario.consumer.manifest_path
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    manifest_before = manifest.read_bytes()
    lock_path = scenario.consumer.root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    resolved_git = shutil.which("git", path=scenario.environment["PATH"])
    assert resolved_git is not None
    real_git = Path(resolved_git)
    shim_dir = tmp_path / "malformed" / "bin"
    _write_malformed_git_shim(shim_dir, real_git)
    scenario.environment.update(
        {
            "PATH": f"{shim_dir}{os.pathsep}{scenario.environment['PATH']}",
            "APM_TEST_REAL_GIT": str(real_git),
        }
    )

    result = _run(apm_binary_path, scenario, "--yes", "--verbose")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Malformed remote tag data for" in output
    assert "No files changed" in output
    assert manifest.read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before
