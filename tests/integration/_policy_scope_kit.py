"""Shared hermetic plumbing for org-policy and user-scope lifecycle chains (#3649).

An org policy is served from ``contoso/.github`` through the loopback GitHub
HTTPS fixture, and ``contoso/pkg-*`` packages are served from local bare
repositories through process-scoped Git URL rewrites. The consumer project's
``origin`` remote points at ``https://github.com/contoso/consumer.git`` so
policy auto-discovery resolves the ``contoso`` org without any real network.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)

ORG = "contoso"
POLICY_REPO = ".github"
POLICY_CACHE_DIR = "policy_v1"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def skill_package_tree(root: Path, name: str, *, revision: str = "v1") -> None:
    """Write a minimal APM package with one skill named ``<name>-skill``."""
    write(
        root / "apm.yml",
        f"name: {name}\nversion: 1.0.0\ndescription: Policy fixture {name}\nauthor: Fixture\n",
    )
    write(
        root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md",
        f"---\nname: {name}-skill\ndescription: {name} fixture skill\n---\n# {name} {revision}\n",
    )


@dataclass
class PublishedPackage:
    name: str
    repository: LocalGitRepository
    commit: GitCommit

    @property
    def slug(self) -> str:
        return f"{ORG}/{self.name}"


class PolicyScopeWorld:
    """One isolated environment with packages, a policy repo, and a consumer."""

    def __init__(self, tmp_path: Path, base_env: Mapping[str, str]) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=base_env)
        self.repositories = LocalGitRepositoryFactory(
            self.isolated.repository_root, env=self.isolated.subprocess_env()
        )
        self.packages: dict[str, PublishedPackage] = {}
        self.policy_repository: LocalGitRepository | None = None

    # -- packages ---------------------------------------------------------
    def publish_package(self, name: str, *, tag: str = "v1.0.0") -> PublishedPackage:
        source = self.isolated.package_root / name
        skill_package_tree(source, name)
        repository = self.repositories.create(name, source_tree=source)
        commit = self.repositories.commit(repository, message=f"{name} {tag}")
        self.repositories.tag(repository, tag, commit)
        published = PublishedPackage(name=name, repository=repository, commit=commit)
        self.packages[name] = published
        return published

    # -- policy -----------------------------------------------------------
    def publish_policy(self, policy: Mapping[str, object]) -> None:
        if self.policy_repository is None:
            source = self.isolated.package_root / "org-policy"
            source.mkdir(parents=True, exist_ok=True)
            dump_yaml(dict(policy), source / "apm-policy.yml")
            self.policy_repository = self.repositories.create(POLICY_REPO, source_tree=source)
        else:
            dump_yaml(dict(policy), self.policy_repository.worktree / "apm-policy.yml")
        self.repositories.commit(self.policy_repository, message="policy")

    def clear_policy_cache(self) -> None:
        shutil.rmtree(self.isolated.cache_root / POLICY_CACHE_DIR, ignore_errors=True)

    def served_repositories(self) -> dict[str, LocalGitRepository]:
        served = {pkg.slug: pkg.repository for pkg in self.packages.values()}
        if self.policy_repository is not None:
            served[f"{ORG}/{POLICY_REPO}"] = self.policy_repository
        return served

    def git_env(self) -> dict[str, str]:
        rewrites = [
            (pkg.repository, f"https://github.com/{pkg.slug}") for pkg in self.packages.values()
        ]
        if not rewrites:
            return self.isolated.subprocess_env()
        return self.repositories.url_rewrite_subprocess_env_many(rewrites)

    # -- consumer ---------------------------------------------------------
    def consumer(
        self,
        name: str = "consumer",
        *,
        dependencies: Iterable[str] = (),
        extra: Mapping[str, object] | None = None,
        targets: Iterable[str] = ("copilot",),
    ) -> Path:
        project = self.isolated.work_root / name
        project.mkdir(parents=True)
        manifest: dict[str, object] = {
            "name": name,
            "version": "1.0.0",
            "description": "Policy consumer",
            "targets": list(targets),
            "dependencies": {"apm": list(dependencies)},
        }
        if extra:
            manifest.update(extra)
        dump_yaml(manifest, project / "apm.yml")
        env = self.isolated.subprocess_env()
        subprocess.run(
            ["git", "init", "-q", "--initial-branch=main"],
            cwd=project,
            env=env,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{ORG}/{name}.git"],
            cwd=project,
            env=env,
            check=True,
        )
        return project


def run_step(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    step: str,
    expected: int | tuple[int, ...] = 0,
) -> CommandResult:
    result = runner.run(args, scenario_id=step, cwd=cwd, env=env)
    allowed = expected if isinstance(expected, tuple) else (expected,)
    assert result.returncode in allowed, (
        f"[{step}] {args!r} -> {result.returncode} (expected {allowed})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result


def output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def lock_repo_urls(project: Path) -> set[str]:
    lock_path = project / "apm.lock.yaml"
    if not lock_path.exists():
        return set()
    lock = load_yaml(lock_path) or {}
    return {str(entry.get("repo_url")) for entry in lock.get("dependencies") or []}


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root: Path, *, exclude: tuple[str, ...] = (".git",)) -> dict[str, str]:
    """Return relative path -> sha256 for every regular file under root."""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in exclude:
            continue
        if path.is_file() and not path.is_symlink():
            snapshot[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def deployed_skills(project: Path) -> set[str]:
    names: set[str] = set()
    for path in project.rglob("SKILL.md"):
        rel = path.relative_to(project)
        if rel.parts[0] in ("apm_modules", ".apm", ".git"):
            continue
        names.add(path.parent.name)
    return names
