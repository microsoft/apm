"""Shared hermetic fixtures for marketplace, pack, export-patch and view lifecycle chains."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)

HOST = "git.example.invalid"


def remote_url(repo: str) -> str:
    return f"https://{HOST}/acme/{repo}"


def skill_md(name: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: Skill {name}\n---\n# {name}\n{body}"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def evidence(result: CommandResult) -> str:
    return (
        f"cwd={str(result.cwd)!r}\ncommand={result.command!r}\n"
        f"returncode={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def output(result: CommandResult) -> str:
    """Whitespace-collapsed combined output for wording assertions."""
    return " ".join((result.stdout + result.stderr).split())


@dataclass
class MarketplacePackWorld:
    """Isolated APM home, local git origins with HTTPS rewrites, and a runner."""

    isolated: IsolatedApmEnvironment
    repos: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner
    rewrites: list[tuple[LocalGitRepository, str]]
    env: dict[str, str]

    @classmethod
    def create(cls, root: Path, binary: Path) -> MarketplacePackWorld:
        isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
        env = isolated.subprocess_env()
        repos = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        runner = ApmLifecycleRunner(
            (str(binary),), timeout_seconds=180, scenario_timeout_seconds=900
        )
        return cls(isolated=isolated, repos=repos, runner=runner, rewrites=[], env=env)

    def publish(self, name: str, tree: Path) -> tuple[LocalGitRepository, GitCommit]:
        repository = self.repos.create(name, source_tree=tree)
        commit = self.repos.commit(repository, message=f"seed {name}")
        self.rewrites.append((repository, remote_url(name)))
        self.env = self.repos.url_rewrite_subprocess_env_many(self.rewrites)
        return repository, commit

    def apm(self, cwd: Path, *args: str, expect: int | None = 0, sid: str = "mp") -> CommandResult:
        result = self.runner.run(args, scenario_id=sid, cwd=cwd, env=self.env)
        if expect is not None:
            assert result.returncode == expect, evidence(result)
        return result

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ("git", *args),
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def consumer(self, name: str, *, targets: Sequence[str], deps: Sequence[Mapping] = ()) -> Path:
        root = self.isolated.work_root / name
        root.mkdir(parents=True)
        manifest: dict[str, object] = {"name": name, "version": "0.1.0", "targets": list(targets)}
        if deps:
            manifest["dependencies"] = {"apm": [dict(dep) for dep in deps]}
        dump_yaml(manifest, root / "apm.yml")
        for target in targets:
            marker = {"claude": ".claude", "copilot": ".github", "codex": ".codex"}.get(target)
            if marker:
                (root / marker).mkdir(exist_ok=True)
        return root

    def home_snapshot(self) -> ArtifactSnapshot:
        return ArtifactSnapshot.capture(self.isolated.home)

    def lock(self, project: Path) -> dict:
        return load_yaml(project / "apm.lock.yaml")


def lock_dep(lock: dict, repo_url: str, virtual_path: str | None = None) -> dict:
    matches = [
        dep
        for dep in lock["dependencies"]
        if dep["repo_url"] == repo_url and dep.get("virtual_path") == virtual_path
    ]
    assert len(matches) == 1, lock["dependencies"]
    return matches[0]


def tree_bytes(root: Path, *, exclude: Sequence[str] = (".git",)) -> dict[str, bytes]:
    """Map every regular file under *root* (symlinks as their target text) to bytes."""
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(rel == part or rel.startswith(f"{part}/") for part in exclude):
            continue
        if path.is_symlink():
            result[rel] = b"symlink:" + os.readlink(path).encode()
        elif path.is_file():
            result[rel] = path.read_bytes()
    return result
