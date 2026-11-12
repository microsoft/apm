"""Hermetic worlds whose project, HOME and package paths contain spaces and non-ASCII.

Each world isolates HOME/APM_HOME under a root such as ``iso w\\u00f6rld \\u00e9``,
publishes local Git origins behind HTTPS rewrites, and drives the real ``apm``
binary through :class:`ApmLifecycleRunner`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
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

HOST = "git.example.invalid"
# Spaces plus precomposed (NFC) non-ASCII letters in every scenario root.
CORNER_SEGMENT = "iso w\u00f6rld \u00e9"


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


@dataclass
class CornerWorld:
    """Isolated APM home under a spaced, non-ASCII root with local git origins."""

    isolated: IsolatedApmEnvironment
    repos: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner
    rewrites: list[tuple[LocalGitRepository, str]]
    env: dict[str, str]

    @classmethod
    def create(cls, root: Path, binary: Path) -> CornerWorld:
        isolated = IsolatedApmEnvironment.create(root / CORNER_SEGMENT, base_env=dict(os.environ))
        env = isolated.subprocess_env()
        repos = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        runner = ApmLifecycleRunner(
            (str(binary),), timeout_seconds=180, scenario_timeout_seconds=900
        )
        return cls(isolated=isolated, repos=repos, runner=runner, rewrites=[], env=env)

    @property
    def home(self) -> Path:
        return Path(self.env["HOME"])

    def publish(self, name: str, tree: Path) -> GitCommit:
        repository = self.repos.create(name, source_tree=tree)
        commit = self.repos.commit(repository, message=f"seed {name}")
        self.rewrites.append((repository, remote_url(name)))
        self.env = self.repos.url_rewrite_subprocess_env_many(self.rewrites)
        return commit

    def apm(self, cwd: Path, *args: str, expect: int | None = 0) -> CommandResult:
        result = self.runner.run(args, scenario_id="path-corner", cwd=cwd, env=self.env)
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

    def project(
        self, name: str, *, targets: Sequence[str], deps: Sequence[str | Mapping] = ()
    ) -> Path:
        root = self.isolated.work_root / name
        root.mkdir(parents=True)
        manifest: dict[str, object] = {
            "name": "corner-consumer",
            "version": "0.1.0",
            "targets": list(targets),
        }
        if deps:
            manifest["dependencies"] = {
                "apm": [dep if isinstance(dep, str) else dict(dep) for dep in deps]
            }
        dump_yaml(manifest, root / "apm.yml")
        return root

    def lock(self, project: Path) -> dict:
        return load_yaml(project / "apm.lock.yaml")


def publish_corner_packages(world: CornerWorld) -> dict[str, str]:
    """Publish packages whose primitive file names contain spaces and non-ASCII."""
    sources = world.isolated.package_root

    uni = sources / "uni"
    write(uni / "apm.yml", "name: uni\nversion: 1.0.0\ndescription: u\n")
    write(uni / ".apm/skills/caf-skill/SKILL.md", skill_md("caf-skill"))
    write(uni / ".apm/skills/caf-skill/notes \u00e9 \u00fc.md", "unicode file\n")
    write(
        uni / ".apm/instructions/my notes.instructions.md",
        "---\napplyTo: '**/*.py'\n---\nspace instr\n",
    )
    write(uni / ".apm/instructions/caf\u00e9.instructions.md", "---\napplyTo: '**'\n---\nu\n")
    write(uni / ".apm/agents/r\u00e9viewer.agent.md", "---\nname: reviewer\ndescription: r\n---\n")
    write(uni / ".apm/prompts/do thing.prompt.md", "---\ndescription: p\n---\nprompt\n")

    mono = sources / "mono"
    write(mono / "LICENSE", "Apache\n")
    tool = mono / "packages" / "t\u00f6\u00f6l kit"
    write(tool / "apm.yml", "name: tool-kit\nversion: 1.0.0\ndescription: t\n")
    write(tool / ".apm/skills/tool-skill/SKILL.md", skill_md("tool-skill"))

    return {"uni": world.publish("uni", uni).sha, "mono": world.publish("mono", mono).sha}
