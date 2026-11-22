"""Hermetic E2E coverage for lockfile replay of transitive APM deps.

Issue #2007 was resolved as a documentation clarification: an existing
``apm.lock.yaml`` replays the locked transitive graph, while a fresh install
without a lockfile resolves the current upstream manifest. This test drives the
real ``apm install`` command through local git fixtures and stubs only the
network boundary so that the replay contract is empirically guarded.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml
from click.testing import CliRunner
from git import Repo

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import (
    GitReferenceType,
    ResolvedReference,
    clear_apm_yml_cache,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_e2e_mode]

TIMEOUT = 180
PRIMARY_REPO = "example/primary-package"
TRANSITIVE_REPO = "example/transitive-package"


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {repo}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    """Create a local git repository with a deterministic main branch."""
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "checkout", "-B", "main")
    _git(repo, "config", "user.name", "APM Test")
    _git(repo, "config", "user.email", "apm-test@example.invalid")


def _write_package_manifest(
    repo: Path,
    *,
    name: str,
    dependencies: list[str] | None = None,
    dev_dependencies: list[str] | None = None,
) -> None:
    """Write an APM package manifest and a primitive file into *repo*."""
    data: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": f"{name} fixture",
    }
    if dependencies is not None:
        data["dependencies"] = {"apm": dependencies}
    if dev_dependencies is not None:
        data["devDependencies"] = {"apm": dev_dependencies}

    (repo / "apm.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    skill_dir = repo / ".apm" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def _commit_all(repo: Path, message: str) -> str:
    """Commit all changes in *repo* and return the resulting SHA."""
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_consumer(project: Path) -> None:
    """Create the consumer manifest with an unpinned direct git dependency."""
    project.mkdir(parents=True)
    manifest = {
        "name": "lockfile-replay-consumer",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": [PRIMARY_REPO]},
    }
    (project / "apm.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )


def _lock_deps(project: Path) -> dict[str, dict[str, Any]]:
    """Read ``apm.lock.yaml`` as a mapping keyed by ``repo_url``."""
    parsed = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    return {
        dep["repo_url"]: dep
        for dep in parsed.get("dependencies", [])
        if isinstance(dep, dict) and "repo_url" in dep
    }


def _argv(command: object) -> list[str]:
    """Return a subprocess command as argv tokens for the network sentinel."""
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    if isinstance(command, str):
        return shlex.split(command)
    return [str(command)]


def _looks_remote_token(token: str) -> bool:
    """Return True when a subprocess token points at a remote URL."""
    if token.startswith("git@"):
        return True
    parsed = urlparse(token)
    return parsed.scheme in {"https", "http", "ssh", "git"}


def _invoke_install(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    repos: dict[str, Path],
    extra_args: list[str] | None = None,
) -> object:
    """Run the real ``apm install`` command with git network calls remapped local."""
    original_popen = subprocess.Popen

    def guarded_popen(command: Any, *args: Any, **kwargs: Any) -> subprocess.Popen:
        argv = _argv(command)
        binary = os.path.basename(argv[0]) if argv else ""
        if binary in {"gh", "curl", "wget"}:
            raise AssertionError(f"Unexpected network subprocess: {binary}")
        if binary == "git" and any(_looks_remote_token(token) for token in argv):
            raise AssertionError(f"Unexpected remote git subprocess: {' '.join(argv)}")
        return original_popen(command, *args, **kwargs)

    def resolve_local_ref(
        _self: GitHubPackageDownloader,
        dep_ref: Any,
    ) -> ResolvedReference:
        repo = repos[dep_ref.repo_url]
        ref = dep_ref.reference or "main"
        sha = _git(repo, "rev-parse", ref)
        ref_type = (
            GitReferenceType.COMMIT
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref)
            else GitReferenceType.BRANCH
        )
        return ResolvedReference(
            original_ref=ref,
            ref_type=ref_type,
            resolved_commit=sha,
            ref_name=ref,
        )

    def clone_local_repo(
        _self: GitHubPackageDownloader,
        repo_url_base: str,
        target_path: Path,
        *args: Any,
        dep_ref: Any = None,
        **clone_kwargs: Any,
    ) -> Repo:
        repo_key = dep_ref.repo_url if dep_ref is not None else repo_url_base
        clone_args: dict[str, Any] = {}
        branch = clone_kwargs.get("branch")
        if branch:
            clone_args["branch"] = branch
        return Repo.clone_from(str(repos[repo_key]), str(target_path), **clone_args)

    with monkeypatch.context() as patch:
        patch.chdir(project)
        patch.setenv("APM_NO_CACHE", "1")
        patch.setenv("HOME", str(project / ".home"))
        patch.setenv("USERPROFILE", str(project / ".home"))
        for token_name in (
            "GITHUB_APM_PAT",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ADO_APM_PAT",
            "GITLAB_APM_PAT",
            "GITLAB_TOKEN",
        ):
            patch.delenv(token_name, raising=False)
        patch.setattr(subprocess, "Popen", guarded_popen)
        patch.setattr(GitHubPackageDownloader, "resolve_git_reference", resolve_local_ref)
        patch.setattr(GitHubPackageDownloader, "_clone_with_fallback", clone_local_repo)

        clear_apm_yml_cache()
        return CliRunner().invoke(cli, ["install", *(extra_args or [])], catch_exceptions=False)


def test_lockfile_replay_keeps_locked_transitive_dependencies_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked install replays old transitive dependencies; a fresh one does not."""
    remotes = tmp_path / "remotes"
    primary = remotes / "primary-package"
    transitive = remotes / "transitive-package"

    _init_repo(transitive)
    _write_package_manifest(transitive, name="transitive-package")
    transitive_sha = _commit_all(transitive, "initial transitive package")

    _init_repo(primary)
    _write_package_manifest(
        primary,
        name="primary-package",
        dependencies=[TRANSITIVE_REPO],
    )
    old_primary_sha = _commit_all(primary, "primary depends on transitive")

    project = tmp_path / "consumer"
    _write_consumer(project)
    repos = {PRIMARY_REPO: primary, TRANSITIVE_REPO: transitive}

    first = _invoke_install(project, monkeypatch, repos)
    assert first.exit_code == 0, f"stdout:\n{first.output}\nstderr:\n{first.stderr}"
    first_deps = _lock_deps(project)
    assert first_deps[PRIMARY_REPO]["resolved_commit"] == old_primary_sha
    assert first_deps[TRANSITIVE_REPO]["resolved_commit"] == transitive_sha
    assert first_deps[TRANSITIVE_REPO]["depth"] == 2
    assert (project / "apm_modules" / "example" / "transitive-package" / "apm.yml").is_file()

    _write_package_manifest(
        primary,
        name="primary-package",
        dev_dependencies=[TRANSITIVE_REPO],
    )
    new_primary_sha = _commit_all(primary, "move transitive dep to dev dependencies")

    # Lockfile replay should keep using the old primary commit and therefore
    # keep the old transitive production dependency in the resolved graph.
    shutil.rmtree(project / "apm_modules", ignore_errors=True)
    replay = _invoke_install(project, monkeypatch, repos)
    assert replay.exit_code == 0, f"stdout:\n{replay.output}\nstderr:\n{replay.stderr}"
    replay_deps = _lock_deps(project)
    assert replay_deps[PRIMARY_REPO]["resolved_commit"] == old_primary_sha
    assert replay_deps[TRANSITIVE_REPO]["resolved_commit"] == transitive_sha
    assert replay_deps[TRANSITIVE_REPO]["depth"] == 2
    assert (project / "apm_modules" / "example" / "transitive-package" / "apm.yml").is_file()

    shutil.rmtree(project / "apm_modules", ignore_errors=True)
    frozen_replay = _invoke_install(project, monkeypatch, repos, extra_args=["--frozen"])
    assert frozen_replay.exit_code == 0, (
        f"stdout:\n{frozen_replay.output}\nstderr:\n{frozen_replay.stderr}"
    )
    frozen_deps = _lock_deps(project)
    assert frozen_deps[PRIMARY_REPO]["resolved_commit"] == old_primary_sha
    assert frozen_deps[TRANSITIVE_REPO]["resolved_commit"] == transitive_sha
    assert frozen_deps[TRANSITIVE_REPO]["depth"] == 2
    assert (project / "apm_modules" / "example" / "transitive-package" / "apm.yml").is_file()

    # Removing the lockfile regenerates from current upstream state. The same
    # direct dependency now has the transitive entry under devDependencies.apm,
    # and APM does not traverse transitive dev dependencies.
    (project / "apm.lock.yaml").unlink()
    shutil.rmtree(project / "apm_modules", ignore_errors=True)
    fresh = _invoke_install(project, monkeypatch, repos)
    assert fresh.exit_code == 0, f"stdout:\n{fresh.output}\nstderr:\n{fresh.stderr}"
    fresh_deps = _lock_deps(project)
    assert fresh_deps[PRIMARY_REPO]["resolved_commit"] == new_primary_sha
    assert TRANSITIVE_REPO not in fresh_deps
    assert not (project / "apm_modules" / "example" / "transitive-package").exists()
