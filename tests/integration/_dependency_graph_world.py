"""Shared hermetic world for dependency-graph lifecycle chains (#3649).

Every remote package is a local bare repository reached through a
process-scoped ``insteadOf`` rewrite of ``https://github.com/test/<name>``.
Each scenario step asserts the exit code and then inspects disk,
``apm.lock.yaml`` and ``apm_modules`` directly.

A package named on the ``apm install`` command line is validated before the
install; on github.com that probe is a REST API call that no rewrite can
route.  Scenarios that name packages on the command line serve them under
:data:`GENERIC_HOST` too (:meth:`World.serve_on`), whose probe is
``git ls-remote`` through the same fixture rewrite.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

NO_POLICY = "--no-policy"
GENERIC_HOST = "git.example.test"


def remote(name: str, host: str = "github.com") -> str:
    return f"https://{host}/test/{name}.git"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    return path


def instruction_text(name: str) -> str:
    return f"---\napplyTo: '**/*.py'\n---\n{name} rules\n"


def write_package(root: Path, name: str, deps: list[object] | None = None) -> Path:
    """Write an APM package with one instruction and the given dependencies."""
    manifest: dict[str, object] = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": list(deps)}
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, root / "apm.yml")
    write(root / ".apm" / "instructions" / f"{name}.instructions.md", instruction_text(name))
    return root


def set_deps(root: Path, deps: list[object]) -> None:
    manifest = load_yaml(root / "apm.yml")
    if deps:
        manifest["dependencies"] = {"apm": list(deps)}
    else:
        manifest.pop("dependencies", None)
    dump_yaml(manifest, root / "apm.yml")


@dataclass
class World:
    isolated: IsolatedApmEnvironment
    factory: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner
    project: Path
    repos: dict[str, LocalGitRepository] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    hosts: list[str] = field(default_factory=lambda: ["github.com"])

    @classmethod
    def create(cls, tmp_path: Path, apm_binary_path: Path) -> World:
        isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
        runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
        project = isolated.work_root / "proj"
        project.mkdir()
        world = cls(isolated, factory, runner, project)
        world.env = isolated.subprocess_env()
        return world

    # -- remote packages -------------------------------------------------
    def publish(self, name: str, deps: list[object] | None = None) -> Path:
        """Create and publish a remote package; return its working tree."""
        source = self.isolated.package_root / name
        write_package(source, name, deps)
        repository = self.factory.create(name, source_tree=source)
        self.factory.commit(repository, message=f"publish {name}")
        self.repos[name] = repository
        self._route()
        return repository.worktree

    def serve_on(self, host: str) -> None:
        """Also serve every fixture (published now or later) under *host*."""
        self.hosts.append(host)
        self._route()

    def _route(self) -> None:
        if not self.repos:
            return
        self.env = self.factory.url_rewrite_subprocess_env_many(
            [
                (repo, remote(repo_name, host))
                for host in self.hosts
                for repo_name, repo in self.repos.items()
            ]
        )

    def republish(self, name: str, message: str = "update") -> None:
        self.factory.commit(self.repos[name], message=message)

    # -- project -----------------------------------------------------------
    def manifest(self, deps: list[object], *, targets: tuple[str, ...] = ("copilot",)) -> None:
        manifest: dict[str, object] = {
            "name": "proj",
            "version": "1.0.0",
            "targets": list(targets),
        }
        if deps:
            manifest["dependencies"] = {"apm": list(deps)}
        dump_yaml(manifest, self.project / "apm.yml")

    def apm(self, *args: str, expect: int | None = 0) -> CommandResult:
        result = self.runner.run(args, cwd=self.project, env=self.env)
        if expect is not None:
            assert result.returncode == expect, (
                f"apm {' '.join(args)} -> {result.returncode} (expected {expect})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def lock(self) -> dict[str, object]:
        return load_yaml(self.project / "apm.lock.yaml")

    def lock_bytes(self) -> bytes:
        return (self.project / "apm.lock.yaml").read_bytes()

    def locked(self) -> dict[str, dict[str, object]]:
        """Lock entries keyed by ``repo_url`` (plus ``/virtual_path``)."""
        entries: dict[str, dict[str, object]] = {}
        for dep in self.lock().get("dependencies") or []:
            key = str(dep["repo_url"])
            if dep.get("virtual_path"):
                key = f"{key}/{dep['virtual_path']}"
            entries[key] = dep
        return entries

    def deployed(self, root: str = ".github/instructions") -> set[str]:
        base = self.project / root
        if not base.exists():
            return set()
        return {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}

    def modules(self) -> set[str]:
        """Package roots under apm_modules (dirs holding apm.yml)."""
        base = self.project / "apm_modules"
        if not base.exists():
            return set()
        return {
            p.parent.relative_to(base).as_posix()
            for p in base.rglob("apm.yml")
            if ".apm-resolution-staging" not in p.parts
        }

    def cross_check(self, *, present: set[str], absent: frozenset[str] = frozenset()) -> None:
        """deps list / tree / why / prune --dry-run / audit --ci agree with *present*."""
        listing = self.apm("deps", "list").stdout
        tree = self.apm("deps", "tree").stdout
        for key in present:
            assert key in listing, f"{key} missing from deps list:\n{listing}"
            assert key in tree, f"{key} missing from deps tree:\n{tree}"
            why = self.apm("deps", "why", key)
            assert "not installed" not in why.stdout
        for key in absent:
            assert key not in tree, f"{key} unexpectedly in deps tree:\n{tree}"
        assert "orphaned" not in listing.lower(), listing
        prune = self.apm("prune", "--dry-run").stdout
        assert "No orphaned packages found" in prune, prune
        self.apm("audit", "--ci", NO_POLICY)

    def clean_cache(self) -> None:
        shutil.rmtree(self.isolated.cache_root)
        self.isolated.cache_root.mkdir()
