"""Deterministic generator of the perf fixtures (recipes in ``recipes.py``).

``build_tree`` authors a layered dependency DAG of git packages plus the
consumer project that installs it; ``build_monorepo`` authors a repository
with several ``apm.yml`` roots that share local-path and remote packages.
Package sources are authored through ``LocalPackageFactory`` (the same
factory the lifecycle tests use), so they are shaped like real APM packages:
instructions, agents, skills (with a reference file), prompts, hooks on a
subset and stdio MCP servers on a smaller subset.

Everything derives from the recipe seed: no clock, no host data. Git commits
use a fixed identity and date, so ``publish_repositories`` returns the same
SHAs on every machine.
"""

from __future__ import annotations

import contextlib
import os
import random
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tests.fixtures.perf.recipes import MonorepoRecipe, PrimitiveMix, TreeRecipe
from tests.utils.local_package import LocalPackage, LocalPackageFactory

# Inert: a deployed hook or MCP entry is configuration only and never runs here.
INERT_COMMAND = "apm-perf-inert-server"
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "APM Perf Fixtures",
    "GIT_AUTHOR_EMAIL": "perf-fixtures@example.invalid",
    "GIT_COMMITTER_NAME": "APM Perf Fixtures",
    "GIT_COMMITTER_EMAIL": "perf-fixtures@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
_WORDS = [
    "agent",
    "api",
    "apply",
    "audit",
    "branch",
    "build",
    "cache",
    "change",
    "check",
    "cli",
    "code",
    "commit",
    "config",
    "context",
    "contract",
    "deploy",
    "dependency",
    "diff",
    "docs",
    "error",
    "file",
    "format",
    "git",
    "guard",
    "hook",
    "install",
    "instruction",
    "lint",
    "lockfile",
    "manifest",
    "merge",
    "module",
    "network",
    "output",
    "package",
    "path",
    "policy",
    "primitive",
    "prompt",
    "pull",
    "release",
    "repo",
    "resolve",
    "review",
    "schema",
    "scope",
    "security",
    "skill",
    "source",
    "spec",
    "state",
    "target",
    "test",
    "token",
    "tree",
    "update",
    "verify",
    "version",
    "workflow",
    "workspace",
    "write",
]
_LANGUAGES = ("py", "ts", "go", "rs", "java", "md", "yml", "sh")


@dataclass(frozen=True)
class TreeFixture:
    """A generated dependency tree: package sources, its consumer and the DAG."""

    recipe: TreeRecipe
    root: Path
    project: Path
    packages: Mapping[str, Path]
    edges: Mapping[str, tuple[str, ...]]
    roots: tuple[str, ...]

    def slug(self, name: str) -> str:
        return f"{self.recipe.owner}/{name}"

    def spec(self, name: str) -> str:
        return f"{self.slug(name)}#{self.recipe.tag}"

    @property
    def depth(self) -> int:
        """Longest path from a direct dependency to a leaf, in levels."""
        memo: dict[str, int] = {}

        def height(name: str) -> int:
            if name not in memo:
                memo[name] = 1 + max((height(c) for c in self.edges[name]), default=0)
            return memo[name]

        return max(height(name) for name in self.roots)

    @property
    def diamonds(self) -> int:
        """Packages reached through two or more parents."""
        parents: dict[str, int] = {}
        for children in self.edges.values():
            for child in children:
                parents[child] = parents.get(child, 0) + 1
        return sum(1 for count in parents.values() if count >= 2)


@dataclass(frozen=True)
class MonorepoFixture:
    """A generated monorepo: its ``apm.yml`` roots and the remote tree they share."""

    recipe: MonorepoRecipe
    root: Path
    project_roots: tuple[Path, ...]
    local_packages: Mapping[str, Path]
    remote: TreeFixture
    remote_by_root: Mapping[Path, tuple[str, ...]] = field(default_factory=dict)


def _seeded(seed: int | str) -> random.Random:
    """Fixture content, not secrets: a reproducible generator is the point."""
    return random.Random(seed)  # noqa: S311


class _Prose:
    """Seeded filler text shaped like real primitive bodies."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def sentence(self, words: int) -> str:
        chosen = [self._rng.choice(_WORDS) for _ in range(words)]
        return " ".join(chosen).capitalize() + "."

    def paragraph(self, sentences: int) -> str:
        return " ".join(self.sentence(self._rng.randint(6, 16)) for _ in range(sentences))

    def bullets(self, count: int) -> str:
        return "\n".join(f"- {self.sentence(self._rng.randint(5, 12))}" for _ in range(count))

    def body(self, title: str) -> str:
        sections = [f"# {title}", "", self.paragraph(self._rng.randint(3, 6)), ""]
        for _ in range(self._rng.randint(2, 4)):
            sections += [
                f"## {self.sentence(3)[:-1]}",
                "",
                self.paragraph(3),
                "",
                self.bullets(4),
                "",
            ]
        return "\n".join(sections)


def _instruction(prose: _Prose, rng: random.Random, name: str) -> str:
    glob = f"**/*.{rng.choice(_LANGUAGES)}"
    return f"---\ndescription: {prose.sentence(8)}\napplyTo: '{glob}'\n---\n\n{prose.body(name)}"


def _agent(prose: _Prose, name: str) -> str:
    return f"---\nname: {name}\ndescription: {prose.sentence(10)}\n---\n\n{prose.body(name)}"


def _skill(prose: _Prose, name: str) -> str:
    return f"---\nname: {name}\ndescription: {prose.sentence(12)}\n---\n\n{prose.body(name)}"


def _prompt(prose: _Prose, name: str) -> str:
    return f"---\ndescription: {prose.sentence(8)}\n---\n\n{prose.body(name)}"


def _hook(name: str) -> dict[str, object]:
    command = {"type": "command", "command": f"echo {name}"}
    return {"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [command]}]}}


def _mcp(name: str) -> dict[str, object]:
    return {
        "name": f"{name}-mcp",
        "registry": False,
        "transport": "stdio",
        "command": INERT_COMMAND,
        "args": ["--stdio", name],
    }


def author_package(
    factory: LocalPackageFactory,
    name: str,
    index: int,
    mix: PrimitiveMix,
    rng: random.Random,
    dependencies: tuple[str, ...] = (),
) -> LocalPackage:
    """Author one package with the primitives of *mix* (names are package-prefixed)."""
    with_mcp = mix.mcp_every > 0 and index % mix.mcp_every == 0
    package = factory.create(
        name,
        version="1.0.0",
        dependencies=dependencies,
        mcp_dependencies=[_mcp(name)] if with_mcp else [],
    )
    prose = _Prose(rng)
    for i in range(mix.instructions):
        factory.add_instruction(package, f"{name}-rule-{i}", _instruction(prose, rng, name))
    for i in range(mix.agents):
        agent = f"{name}-agent-{i}"
        factory.add_agent(package, agent, _agent(prose, agent))
    for i in range(mix.skills):
        skill = f"{name}-skill-{i}"
        factory.add_skill(package, skill, _skill(prose, skill))
        reference = package.root / "skills" / skill / "references" / "guide.md"
        reference.parent.mkdir(parents=True)
        write_lf(reference, prose.body(f"{skill} guide"))
    for i in range(mix.prompts):
        factory.add_prompt(package, f"{name}-prompt-{i}", _prompt(prose, f"{name}-prompt-{i}"))
    if mix.hook_every > 0 and index % mix.hook_every == 0:
        factory.add_hook(package, f"{name}-hook", _hook(name))
    return package


def _level_sizes(packages: int, depth: int) -> list[int]:
    if depth < 1 or packages < depth:
        raise ValueError(f"need packages >= depth >= 1, got packages={packages} depth={depth}")
    sizes = [packages // depth] * depth
    for i in range(packages - sum(sizes)):
        sizes[1 + i % max(depth - 1, 1) if depth > 1 else 0] += 1
    return sizes


def plan_tree(recipe: TreeRecipe) -> tuple[list[list[str]], dict[str, list[str]]]:
    """The seeded DAG of *recipe*: package names per level and each package's children."""
    rng = _seeded(recipe.seed)
    levels: list[list[str]] = []
    index = 0
    for size in _level_sizes(recipe.packages, recipe.depth):
        levels.append([f"pkg-{index + i:03d}" for i in range(size)])
        index += size
    edges: dict[str, list[str]] = {name: [] for level in levels for name in level}
    low, high = recipe.fanout
    for i, level in enumerate(levels[:-1]):
        below = levels[i + 1]
        for name in level:
            count = min(rng.randint(low, high), len(below))
            edges[name].extend(rng.sample(below, count))
            if i + 2 < len(levels) and rng.random() < recipe.skip_ratio:
                skip = rng.choice(levels[i + 2])
                if skip not in edges[name]:
                    edges[name].append(skip)
    parents: dict[str, list[str]] = {name: [] for name in edges}
    for parent, children in edges.items():
        for child in children:
            parents[child].append(parent)
    for i, level in enumerate(levels[1:], start=1):
        for name in level:
            if not parents[name]:
                parent = rng.choice(levels[i - 1])
                edges[parent].append(name)
                parents[name].append(parent)
    diamonds = sum(1 for p in parents.values() if len(p) >= 2)
    candidates = [
        (i, name)
        for i, level in enumerate(levels[1:], start=1)
        for name in level
        if len(parents[name]) == 1 and len(levels[i - 1]) >= 2
    ]
    rng.shuffle(candidates)
    for i, name in candidates:
        if diamonds >= recipe.min_diamonds:
            break
        extra = rng.choice([p for p in levels[i - 1] if p not in parents[name]])
        edges[extra].append(name)
        parents[name].append(extra)
        diamonds += 1
    return levels, {name: sorted(children) for name, children in edges.items()}


def remove_tree(path: Path) -> None:
    """``shutil.rmtree(path, ignore_errors=True)`` that also deletes read-only files.

    Git writes object files read-only, and on Windows a read-only file cannot
    be deleted, so a plain ``ignore_errors`` rmtree silently leaves them behind.
    """

    def retry(func, target, _exc) -> None:
        with contextlib.suppress(OSError):
            # Never follow a link: only the entry itself is made writable.
            if os.chmod in os.supports_follow_symlinks:
                os.chmod(target, stat.S_IWRITE, follow_symlinks=False)
            elif not os.path.islink(target):
                os.chmod(target, stat.S_IWRITE)
            func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def write_lf(path: Path, text: str) -> None:
    """Write *text* as UTF-8 with LF newlines on every platform (byte-stable fixtures)."""
    path.write_bytes(text.encode("utf-8"))


def consumer_manifest(name: str, dependencies: list[str], targets: tuple[str, ...]) -> str:
    document = {
        "name": name,
        "version": "1.0.0",
        "license": "MIT",
        "targets": list(targets),
        "dependencies": {"apm": dependencies},
    }
    return yaml.safe_dump(document, sort_keys=False)


def build_tree(recipe: TreeRecipe, root: Path) -> TreeFixture:
    """Author *recipe* under *root* (``packages/<name>/`` and ``project/apm.yml``)."""
    if root.exists():
        raise FileExistsError(f"{root} exists; generate into a fresh directory")
    levels, edges = plan_tree(recipe)
    factory = LocalPackageFactory(root / "packages")
    packages: dict[str, Path] = {}
    ordinal = 0
    for level in levels:
        for name in level:
            rng = _seeded(f"{recipe.seed}:{name}")
            deps = tuple(f"{recipe.owner}/{child}#{recipe.tag}" for child in edges[name])
            package = author_package(factory, name, ordinal, recipe.primitives, rng, deps)
            packages[name] = package.root
            ordinal += 1
    project = root / "project"
    project.mkdir(parents=True)
    direct = [f"{recipe.owner}/{name}#{recipe.tag}" for name in levels[0]]
    write_lf(
        project / "apm.yml", consumer_manifest(f"{recipe.name}-consumer", direct, recipe.targets)
    )
    return TreeFixture(
        recipe=recipe,
        root=root,
        project=project,
        packages=packages,
        edges={name: tuple(children) for name, children in edges.items()},
        roots=tuple(levels[0]),
    )


def build_monorepo(recipe: MonorepoRecipe, root: Path) -> MonorepoFixture:
    """Author *recipe* under *root*: ``repo/`` (the monorepo) and ``remote/`` (its tree)."""
    remote = build_tree(recipe.tree, root / "remote")
    repo = root / "repo"
    factory = LocalPackageFactory(repo / "packages")
    rng = _seeded(recipe.seed)
    names = [f"lib-{i:02d}" for i in range(recipe.local_packages)]
    remote_names = sorted(remote.packages)
    local: dict[str, Path] = {}
    for i, name in enumerate(names):
        deps: list[str] = []
        later = names[i + 1 :]
        if later:
            deps += [f"../{n}" for n in rng.sample(later, min(2, len(later)))]
        if rng.random() < 0.5:
            deps.append(remote.spec(rng.choice(remote_names)))
        package_rng = _seeded(f"{recipe.seed}:{name}")
        package = author_package(factory, name, i, recipe.primitives, package_rng, tuple(deps))
        local[name] = package.root
    roots: list[Path] = [repo]
    remote_by_root: dict[Path, tuple[str, ...]] = {}
    apps = [repo / "apps" / f"app-{i:02d}" for i in range(recipe.apps)]
    for project in (repo, *apps):
        prefix = "./packages" if project == repo else "../../packages"
        chosen_local = rng.sample(names, min(recipe.local_per_app, len(names)))
        chosen_remote = rng.sample(remote_names, min(recipe.remote_per_app, len(remote_names)))
        deps = [f"{prefix}/{n}" for n in sorted(chosen_local)]
        deps += [remote.spec(n) for n in sorted(chosen_remote)]
        project.mkdir(parents=True, exist_ok=True)
        write_lf(
            project / "apm.yml",
            consumer_manifest(f"{recipe.name}-{project.name}", deps, recipe.targets),
        )
        remote_by_root[project] = tuple(sorted(chosen_remote))
        if project != repo:
            roots.append(project)
    return MonorepoFixture(
        recipe=recipe,
        root=repo,
        project_roots=tuple(roots),
        local_packages=local,
        remote=remote,
        remote_by_root=remote_by_root,
    )


def _publish_env(extra: Mapping[str, str]) -> dict[str, str]:
    """Host-independent git: no system/global config, ignore rules, filters or GIT_* leaks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if os.environ.get("GIT_EXEC_PATH"):
        # A relocatable git finds its helpers only through GIT_EXEC_PATH.
        env["GIT_EXEC_PATH"] = os.environ["GIT_EXEC_PATH"]
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            **_GIT_IDENTITY,
            **extra,
        }
    )
    return env


_PINNED_CONFIG = (
    "-c",
    "commit.gpgsign=false",
    "-c",
    f"core.excludesFile={os.devnull}",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
    "-c",
    "core.filemode=true",
)


def _git(*args: str, env: Mapping[str, str]) -> str:
    git = shutil.which("git") or "git"
    result = subprocess.run(
        (git, *_PINNED_CONFIG, *args),
        capture_output=True,
        text=True,
        env=_publish_env(env),
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def commit_bare(bare: Path, tree: Path, *, tag: str | None) -> str:
    """Create bare repository *bare* holding *tree* as one commit on main (+ *tag*).

    Git runs with host configuration disabled, so the committed bytes and SHAs
    do not depend on the machine (see ``_publish_env``).
    """
    _git("init", "-q", "--bare", "--template=", "--initial-branch=main", str(bare), env={})
    index = {
        "GIT_DIR": str(bare),
        "GIT_INDEX_FILE": str(bare / "perf-index"),
        "GIT_WORK_TREE": str(tree),
    }
    _git("add", "-A", "--force", "--", ".", env=index)
    tree_sha = _git("write-tree", env=index)
    (bare / "perf-index").unlink()
    commit = _git("commit-tree", tree_sha, "-m", f"publish {tree.name}", env={"GIT_DIR": str(bare)})
    _git("update-ref", "refs/heads/main", commit, env={"GIT_DIR": str(bare)})
    if tag is not None:
        _git("update-ref", f"refs/tags/{tag}", commit, env={"GIT_DIR": str(bare)})
    return commit


def publish_repositories(fixture: TreeFixture, directory: Path) -> dict[str, Path]:
    """Commit every package of *fixture* as a bare repository; return ``slug -> bare``.

    Bare repositories live under ``directory/<recipe name>/`` and are rebuilt on
    every call, so a changed recipe never serves stale content.
    """
    root = directory / fixture.recipe.name
    remove_tree(root)
    root.mkdir(parents=True)
    served: dict[str, Path] = {}
    for name, tree in sorted(fixture.packages.items()):
        bare = root / f"{fixture.recipe.owner}__{name}.git"
        commit_bare(bare, tree, tag=fixture.recipe.tag)
        served[fixture.slug(name)] = bare
    return served
