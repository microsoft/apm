"""End-to-end coverage for cold-cache ``apm audit --ci`` replay semantics.

These scenarios exercise the setup-only CI topology from issues #2328 and
#2392: the checkout has a lockfile but no live ``apm_modules/`` tree, so the
audit must self-hydrate from lock pins into an isolated scratch directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]


def _run_git(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in the consumer repository."""
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _git_status(cwd: Path, env: dict[str, str]) -> str:
    """Return ``git status --porcelain`` for stable no-write assertions."""
    return _run_git(cwd, env, "status", "--porcelain").stdout


def _audit_json(
    runner: ApmLifecycleRunner,
    project_root: Path,
    env: dict[str, str],
    *,
    scenario_id: str,
    expected_returncode: int,
) -> tuple[dict[str, object], CommandResult]:
    """Run ``apm audit --ci -f json`` and parse its payload."""
    result = runner.run_sequence(
        (("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),),
        expected_returncodes=(expected_returncode,),
        scenario_id=scenario_id,
        cwd=project_root,
        env=env,
    )[0]
    return json.loads(result.stdout), result


def _init_consumer_git_repo(
    project_root: Path,
    env: dict[str, str],
    *,
    track_deployments: bool,
) -> None:
    """Create a git repo that either tracks or ignores deployed instructions."""
    _run_git(project_root, env, "init", "--initial-branch=main")
    _run_git(project_root, env, "config", "user.email", "test@example.com")
    _run_git(project_root, env, "config", "user.name", "APM Test")
    gitignore_lines = ["apm_modules/"]
    if not track_deployments:
        gitignore_lines.append(".github/instructions/")
    (project_root / ".gitignore").write_text("\n".join(gitignore_lines) + "\n", encoding="utf-8")


def _seed_consumer(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    track_deployments: bool,
) -> tuple[Path, dict[str, str], ApmLifecycleRunner]:
    """Create one consumer project plus a real remote dependency."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / ("tracked" if track_deployments else "gitignored"),
        base_env=dict(os.environ),
    )
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    packages = LocalPackageFactory(isolated.package_root)

    dependency = packages.create("remote-dependency")
    packages.add_instruction(
        dependency,
        "remote",
        "---\napplyTo: '**'\n---\n# Remote\nDependency instruction.\n",
    )
    dependency_repo = repositories.create("remote-dependency", source_tree=dependency.root)
    dependency_commit = repositories.commit(dependency_repo, message="seed dependency")
    remote_url = "https://github.com/test/remote-dependency.git"
    git_sources = (
        "git@github.com:test/remote-dependency.git",
        remote_url,
    )
    user_git_env = dict(env)
    user_git_env.pop("GIT_CONFIG_GLOBAL", None)
    for git_env in (env, user_git_env):
        for source in git_sources:
            subprocess.run(
                (
                    "git",
                    "config",
                    "--global",
                    "--add",
                    f"url.{dependency_repo.file_url}.insteadOf",
                    source,
                ),
                env=git_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
    consumer_env = dict(env)

    consumer = packages.create(
        "consumer",
        dependencies=(
            {
                "git": remote_url,
                "ref": dependency_commit.sha,
            },
        ),
        targets=("copilot",),
    )
    packages.add_instruction(
        consumer,
        "foo",
        "---\napplyTo: '**'\n---\n# Foo\nInitial content.\n",
    )
    _init_consumer_git_repo(
        consumer.root,
        consumer_env,
        track_deployments=track_deployments,
    )

    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    install = runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="seed-consumer-install",
        cwd=consumer.root,
        env=consumer_env,
    )[0]
    assert install.stdout
    _run_git(consumer.root, consumer_env, "add", "--all")
    _run_git(consumer.root, consumer_env, "commit", "-m", "seed consumer")
    return consumer.root, consumer_env, runner


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    """Return one named CI check from the public audit payload."""
    return next(check for check in payload["checks"] if check["name"] == name)


def test_cold_cache_audit_fails_for_tracked_stale_deployments(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Tracked committed outputs must fail from a cold cache without writes."""
    project_root, env, runner = _seed_consumer(
        tmp_path,
        apm_binary_path,
        track_deployments=True,
    )
    deployed_instruction = project_root / ".github" / "instructions" / "foo.instructions.md"
    assert deployed_instruction.is_file()
    original_deployed = deployed_instruction.read_bytes()
    original_lock = (project_root / "apm.lock.yaml").read_bytes()

    (project_root / ".apm" / "instructions" / "foo.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Foo\nUpdated content.\n",
        encoding="utf-8",
    )
    shutil.rmtree(project_root / "apm_modules")
    status_before = _git_status(project_root, env)

    payload, _result = _audit_json(
        runner,
        project_root,
        env,
        scenario_id="tracked-cold-cache-audit",
        expected_returncode=1,
    )

    drift = _check(payload, "drift")
    assert payload["passed"] is False
    assert drift["passed"] is False
    assert "drift detected" in drift["message"]
    assert "foo.instructions.md" in " ".join(drift.get("details") or [])
    assert deployed_instruction.read_bytes() == original_deployed
    assert (project_root / "apm.lock.yaml").read_bytes() == original_lock
    assert _git_status(project_root, env) == status_before
    assert not (project_root / "apm_modules").exists()


def test_cold_cache_audit_stays_green_for_gitignored_clean_deployments(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Gitignored deployed outputs remain a green positive control when clean."""
    project_root, env, runner = _seed_consumer(
        tmp_path,
        apm_binary_path,
        track_deployments=False,
    )
    lock_before = (project_root / "apm.lock.yaml").read_bytes()
    shutil.rmtree(project_root / "apm_modules")
    status_before = _git_status(project_root, env)

    payload, _result = _audit_json(
        runner,
        project_root,
        env,
        scenario_id="gitignored-cold-cache-audit",
        expected_returncode=0,
    )

    drift = _check(payload, "drift")
    assert payload["passed"] is True
    assert drift["passed"] is True
    assert drift["message"] == "no drift detected against lockfile"
    assert (project_root / "apm.lock.yaml").read_bytes() == lock_before
    assert _git_status(project_root, env) == status_before
    assert not (project_root / "apm_modules").exists()
