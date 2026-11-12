"""Installed-CLI lifecycle coverage for retained revision pins."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
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


def _new_scenario(
    root: Path,
    *,
    mcp_dependencies: tuple[dict[str, object], ...] = (),
) -> _Scenario:
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
        mcp_dependencies=mcp_dependencies,
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


_LEGACY_GENERATED_AT = "2026-01-01T00:00:00+00:00"
_MCP_SERVER = "revision-pin-local-server"
_REPLACEMENT_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def _set_manifest(scenario: _Scenario, *, released_ref: str, replace: bool) -> None:
    """Re-pin the released dependency and toggle one dormant replacement rule."""
    manifest_path = scenario.consumer.manifest_path
    manifest = load_yaml(manifest_path)
    for entry in manifest["dependencies"]["apm"]:
        if entry["alias"] == "released":
            entry["ref"] = released_ref
    if replace:
        manifest["$schema"] = _REPLACEMENT_SCHEMA
        manifest["replace"] = [
            {
                "for": {"git": "https://github.com/unused-replacement-fixture/base.git"},
                "with": {"path": "../intentionally-absent"},
            }
        ]
    else:
        manifest.pop("replace", None)
        manifest.pop("$schema", None)
    dump_yaml(manifest, manifest_path)


def _assert_unchanged_installs(
    apm_binary_path: Path, scenario: _Scenario, lock_path: Path
) -> bytes:
    """Two consecutive unchanged installs must leave the lock bytes untouched."""
    expected = lock_path.read_bytes()
    for _ in range(2):
        installed = _install(apm_binary_path, scenario)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert lock_path.read_bytes() == expected
    return expected


def _assert_lock_state(
    lock_path: Path,
    scenario: _Scenario,
    *,
    version: str,
    released_commit: str,
    released_tag: str | None,
) -> dict:
    """Check schema version, both pins and MCP ownership in the written lock."""
    lock = load_yaml(lock_path)
    assert lock["lockfile_version"] == version
    by_commit = {entry["resolved_commit"]: entry for entry in lock["dependencies"]}
    assert set(by_commit) == {released_commit, scenario.retained.sha}
    assert by_commit[released_commit].get("resolved_tag") == released_tag
    assert "resolved_tag" not in by_commit[scenario.retained.sha]
    assert lock["mcp_servers"] == [_MCP_SERVER]
    assert lock["mcp_target_servers"] == {"claude": [_MCP_SERVER]}
    return lock


def test_unchanged_install_keeps_revision_pin_tag_lock_bytes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A carried-forward resolved_tag must not churn a committed lock (#3433)."""
    scenario = _new_scenario(
        tmp_path / "no-churn",
        mcp_dependencies=(
            {
                "name": _MCP_SERVER,
                "registry": False,
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "revision_pin_server"],
            },
        ),
    )
    lock_path = scenario.consumer.root / "apm.lock.yaml"
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    applied = _run(apm_binary_path, scenario, "--yes")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    _assert_lock_state(
        lock_path,
        scenario,
        version="2",
        released_commit=scenario.released_new.sha,
        released_tag="v2.0.0",
    )
    # Model a lock committed by an older CLI that still recorded generated_at.
    lock_text = lock_path.read_text(encoding="utf-8")
    assert "generated_at" not in lock_text
    lock_path.write_text(
        lock_text.replace(
            "lockfile_version: '2'\n",
            f"lockfile_version: '2'\ngenerated_at: '{_LEGACY_GENERATED_AT}'\n",
            1,
        ),
        encoding="utf-8",
    )
    assert load_yaml(lock_path)["generated_at"] == _LEGACY_GENERATED_AT

    _assert_unchanged_installs(apm_binary_path, scenario, lock_path)

    _set_manifest(scenario, released_ref=scenario.released_new.sha, replace=True)
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    replaced = _assert_lock_state(
        lock_path,
        scenario,
        version="3",
        released_commit=scenario.released_new.sha,
        released_tag="v2.0.0",
    )
    assert replaced["replacements"]
    _assert_unchanged_installs(apm_binary_path, scenario, lock_path)

    _set_manifest(scenario, released_ref=scenario.released_old.sha, replace=False)
    before_ref_change = lock_path.read_bytes()
    installed = _install(apm_binary_path, scenario)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert lock_path.read_bytes() != before_ref_change
    repinned = _assert_lock_state(
        lock_path,
        scenario,
        version="1",
        released_commit=scenario.released_old.sha,
        released_tag=None,
    )
    assert "replacements" not in repinned
    assert repinned["generated_at"] != _LEGACY_GENERATED_AT
    _assert_unchanged_installs(apm_binary_path, scenario, lock_path)
