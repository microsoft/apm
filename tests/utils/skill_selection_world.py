"""Remote-shaped skill-bundle dependency graphs for real-CLI lifecycle tests.

Every package is published to an owned local bare repository and reached under
a ``github.com/<owner>/<repo>`` identity: Git transport is rewritten through
process-scoped ``insteadOf`` config and GitHub HTTPS traffic is answered by
:mod:`tests.utils.local_github_https_fixture`. Remote-shaped identities keep a
root declaration and a transitive declaration of the same package on ONE
dependency key (local paths split keys), which diamond-selection scenarios need.

Repository commits use the isolated environment's pinned author, committer and
dates, so the same world built twice yields the same commit SHAs. Upgrade
fixtures captured from older releases rely on that determinism.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_github_https_fixture import local_github_https_fixture

OWNER = "fixture-org"
INSTALL = ("install", "--no-policy", "--parallel-downloads", "0")
AUDIT_CI = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")


def skill_body(name: str, revision: str = "1") -> str:
    """Return a valid SKILL.md body for *name*."""
    return f"---\nname: {name}\ndescription: Selection fixture skill {name}\n---\n# {name} r{revision}\n"


def instruction_body(name: str) -> str:
    """Return a valid instruction body for *name*."""
    return f"---\napplyTo: '**'\ndescription: Selection fixture instruction {name}\n---\n# {name}\n"


@dataclass
class Published:
    """One published package: its served slug, repository and latest commit."""

    slug: str
    repository: LocalGitRepository
    commit: GitCommit
    host: str = "github.com"
    clone_url: str | None = None

    @property
    def remote_url(self) -> str:
        return self.clone_url or f"https://{self.host}/{self.slug}"


@dataclass
class SkillSelectionWorld:
    """Isolated HOME, published packages and a runner for one scenario."""

    isolated: IsolatedApmEnvironment
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner
    published: dict[str, Published] = field(default_factory=dict)
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        root: Path,
        apm_command: Sequence[str],
        *,
        timeout_seconds: float = 180.0,
    ) -> SkillSelectionWorld:
        isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        return cls(
            isolated=isolated,
            repositories=LocalGitRepositoryFactory(
                isolated.repository_root, env=isolated.subprocess_env()
            ),
            runner=ApmLifecycleRunner(
                tuple(apm_command),
                timeout_seconds=timeout_seconds,
                scenario_timeout_seconds=timeout_seconds * 4,
            ),
        )

    # -- authoring -------------------------------------------------------

    def _source_dir(self, name: str) -> Path:
        return self.isolated.package_root / name

    def publish(
        self,
        name: str,
        *,
        skills: Sequence[str] = (),
        instructions: Sequence[str] = (),
        dependencies: Sequence[object] = (),
        dev_dependencies: Sequence[object] = (),
        owner: str = OWNER,
        repo: str | None = None,
        host: str = "github.com",
        clone_url: str | None = None,
        manifest_extra: Mapping[str, object] | None = None,
    ) -> Published:
        """Author a package (skills, instructions, apm deps) and publish it."""
        repo = repo or name
        source = self._source_dir(f"{owner}--{repo}--{host}".replace("/", "_"))
        source.mkdir(parents=True, exist_ok=False)
        _write_package(
            source,
            name=name,
            skills=skills,
            instructions=instructions,
            dependencies=dependencies,
            dev_dependencies=dev_dependencies,
            manifest_extra=manifest_extra,
        )
        repository = self.repositories.create(
            f"{host}--{owner}--{repo}".replace("/", "_"), source_tree=source
        )
        commit = self.repositories.commit(repository, message=f"seed {name}")
        published = Published(f"{owner}/{repo}", repository, commit, host, clone_url)
        self.published[published.remote_url] = published
        return published

    def republish(
        self,
        published: Published,
        *,
        dependencies: Sequence[object] | None = None,
        skills: Sequence[str] | None = None,
        revision: str = "2",
    ) -> GitCommit:
        """Rewrite a published package's manifest deps and/or skills and push."""
        worktree = published.repository.worktree
        manifest_path = worktree / "apm.yml"
        manifest = load_yaml(manifest_path)
        if dependencies is not None:
            manifest.setdefault("dependencies", {})["apm"] = list(dependencies)
            dump_yaml(manifest, manifest_path)
        if skills is not None:
            shutil.rmtree(worktree / "skills", ignore_errors=True)
            for name in skills:
                _write(worktree / "skills" / name / "SKILL.md", skill_body(name, revision))
        published.commit = self.repositories.commit(
            published.repository, message=f"publish revision {revision}"
        )
        return published.commit

    def project(
        self,
        name: str,
        *,
        dependencies: Sequence[object] = (),
        dev_dependencies: Sequence[object] = (),
        targets: Sequence[str] = ("copilot",),
    ) -> Path:
        """Create a consumer project directory with an apm.yml."""
        root = self.isolated.work_root / name
        root.mkdir(parents=True)
        extra: dict[str, object] = {"targets": list(targets)} if targets else {}
        _write_package(
            root,
            name=name,
            dependencies=dependencies,
            dev_dependencies=dev_dependencies,
            manifest_extra=extra,
        )
        return root

    # -- execution -------------------------------------------------------

    @contextlib.contextmanager
    def serve(self) -> Iterator[dict[str, str]]:
        """Serve every published package; yield the child environment."""
        github = {p.slug: p.repository for p in self.published.values() if p.host == "github.com"}
        rewrites = [(p.repository, p.remote_url) for p in self.published.values()]
        with local_github_https_fixture(self.isolated.root / "certs", github) as fixture:
            env = fixture.child_environment(
                self.repositories.url_rewrite_subprocess_env_many(rewrites)
            )
            env["APM_E2E_TESTS"] = "1"
            env.update(self.extra_env)
            yield env

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        expect: int | None = 0,
        scenario_id: str = "skill-selection",
    ) -> CommandResult:
        result = self.runner.run(tuple(args), scenario_id=scenario_id, cwd=cwd, env=env)
        if expect is not None:
            assert result.returncode == expect, (
                f"scenario={scenario_id} args={list(args)!r} rc={result.returncode}\n"
                f"stdout:\n{result.stdout[-6000:]}\nstderr:\n{result.stderr[-4000:]}"
            )
        return result


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_package(
    root: Path,
    *,
    name: str,
    skills: Sequence[str] = (),
    instructions: Sequence[str] = (),
    dependencies: Sequence[object] = (),
    dev_dependencies: Sequence[object] = (),
    manifest_extra: Mapping[str, object] | None = None,
) -> None:
    """Write apm.yml without parsing deps, so names old parsers reject still land."""
    manifest: dict[str, object] = {
        "name": name,
        "version": "0.1.0",
        "description": f"Selection fixture package {name}",
        "author": "APM Test",
    }
    if dependencies:
        manifest["dependencies"] = {"apm": list(dependencies)}
    if dev_dependencies:
        manifest["devDependencies"] = {"apm": list(dev_dependencies)}
    manifest.update(manifest_extra or {})
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, root / "apm.yml")
    for skill in skills:
        _write(root / "skills" / skill / "SKILL.md", skill_body(skill))
    for instruction in instructions:
        _write(
            root / ".apm" / "instructions" / f"{instruction}.instructions.md",
            instruction_body(instruction),
        )


# -- observation helpers -----------------------------------------------------


def lock_path(root: Path) -> Path:
    return root / "apm.lock.yaml"


def locked(root: Path, repo_url: str) -> dict[str, object] | None:
    """Return the lock entry for *repo_url*, or None."""
    path = lock_path(root)
    if not path.is_file():
        return None
    data = load_yaml(path) or {}
    entries = [e for e in data.get("dependencies") or [] if e.get("repo_url") == repo_url]
    assert len(entries) <= 1, entries
    return entries[0] if entries else None


def skill_dirs(skills_root: Path) -> list[str]:
    """Names of skill directories under one harness skills root."""
    if not skills_root.is_dir():
        return []
    return sorted(p.name for p in skills_root.iterdir() if p.is_dir())


def deployment_values(root: Path) -> set[str]:
    """Every deployment-ledger value recorded in the lockfile."""
    path = lock_path(root)
    if not path.is_file():
        return set()
    data = load_yaml(path) or {}
    return {str(row.get("value")) for row in data.get("deployments") or []}


def audit_failures(result: CommandResult) -> dict[str, list[str]]:
    """Map failed audit check names to their details from ``--format json`` output."""
    payload = json.loads(result.stdout)
    return {
        check["name"]: list(check.get("details") or [check.get("message", "")])
        for check in payload.get("checks", [])
        if not check.get("passed", True)
    }
