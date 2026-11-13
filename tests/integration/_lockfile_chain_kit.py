"""Shared two-tag package fixture for lockfile and cold-restore lifecycle chains (#3649).

``test/kit`` is a local APM package published at ``v1.0.0`` with three
skills, an instruction, a prompt and an agent. ``publish_v110`` releases
``v1.1.0``: alpha changes and drops ``notes.md``, gamma and the prompt are
removed, the instruction changes and a new ``delta`` skill appears. The
consumer project depends on ``test/kit#^1.0.0`` and commits its deployed
output to Git, so fresh clones emulate CI runners without receipts.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    LocalGitRepository,
    LocalGitRepositoryFactory,
)

KIT_URL = "https://github.com/test/kit.git"
REFUSAL = "no trustworthy local deployment baseline"
GUIDANCE = "Move the conflicting file aside and retry"


@dataclass
class Kit:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    factory: LocalGitRepositoryFactory
    repository: LocalGitRepository
    project: Path

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(args, cwd=cwd or self.project, env=self.env)


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=60,
    ).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def skill(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n"


def kit_v100(root: Path) -> None:
    root.mkdir(parents=True)
    dump_yaml({"name": "kit", "version": "1.0.0", "description": "Kit"}, root / "apm.yml")
    write(root / ".apm/skills/alpha/SKILL.md", skill("alpha", "Alpha v1."))
    write(root / ".apm/skills/alpha/notes.md", "Alpha notes v1.\n")
    write(root / ".apm/skills/beta/SKILL.md", skill("beta", "Beta v1."))
    write(root / ".apm/skills/beta/extra.md", "Beta extra.\n")
    write(root / ".apm/skills/gamma/SKILL.md", skill("gamma", "Gamma v1."))
    write(
        root / ".apm/instructions/style.instructions.md",
        "---\napplyTo: '**/*.py'\ndescription: style\n---\n# Style v1\n",
    )
    write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nHello v1.\n")
    write(
        root / ".apm/agents/helper.agent.md",
        "---\nname: helper\ndescription: Helper\n---\nHelper v1.\n",
    )


def output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def build_kit(
    tmp_path: Path,
    apm_binary_path: Path,
    targets: tuple[str, ...] = ("copilot", "claude", "codex"),
) -> Kit:
    """Install ``test/kit@v1.0.0`` and commit the consumer's deployed output."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    source = isolated.package_root / "kit"
    kit_v100(source)
    repository = factory.create("kit", source_tree=source)
    factory.tag(repository, "v1.0.0", factory.commit(repository, message="v1.0.0"))
    env = factory.url_rewrite_subprocess_env(repository, KIT_URL)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": list(targets),
            "dependencies": {"apm": ["test/kit#^1.0.0"], "mcp": []},
        },
        project / "apm.yml",
    )
    write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    kit = Kit(isolated, env, runner, factory, repository, project)
    first = kit.run("install", "--no-policy")
    assert first.returncode == 0, first.stdout + first.stderr
    git(project, env, "init", "--initial-branch=main")
    git(project, env, "config", "user.email", "test@example.com")
    git(project, env, "config", "user.name", "APM Test")
    git(project, env, "config", "core.autocrlf", "false")
    git(project, env, "add", "--all")
    git(project, env, "commit", "-m", "commit kit v1.0.0 output")
    return kit


def publish_v110(kit: Kit) -> None:
    worktree = kit.repository.worktree
    manifest = load_yaml(worktree / "apm.yml")
    manifest["version"] = "1.1.0"
    dump_yaml(manifest, worktree / "apm.yml")
    write(worktree / ".apm/skills/alpha/SKILL.md", skill("alpha", "Alpha v2."))
    (worktree / ".apm/skills/alpha/notes.md").unlink()
    robust_rmtree(worktree / ".apm/skills/gamma")
    (worktree / ".apm/prompts/hello.prompt.md").unlink()
    write(
        worktree / ".apm/instructions/style.instructions.md",
        "---\napplyTo: '**/*.py'\ndescription: style\n---\n# Style v2\n",
    )
    write(worktree / ".apm/skills/delta/SKILL.md", skill("delta", "Delta v2."))
    kit.factory.tag(kit.repository, "v1.1.0", kit.factory.commit(kit.repository, message="v1.1.0"))


def evict_machine_state(kit: Kit, *, apm_home: bool = False) -> None:
    """Emulate a fresh CI runner: no receipts and no download cache."""
    if apm_home:
        robust_rmtree(kit.isolated.config_root)
        kit.isolated.config_root.mkdir(parents=True)
    for receipts in kit.isolated.config_root.glob("deployment-receipts*"):
        robust_rmtree(receipts)
    if kit.isolated.cache_root.exists():
        robust_rmtree(kit.isolated.cache_root)
    kit.isolated.cache_root.mkdir(parents=True)


def fresh_clone(kit: Kit, name: str) -> Path:
    clone = kit.isolated.work_root / name
    git(
        kit.isolated.work_root,
        kit.env,
        "clone",
        "--quiet",
        "-c",
        "core.autocrlf=false",
        str(kit.project),
        str(clone),
    )
    evict_machine_state(kit)
    return clone


def tree(project: Path) -> dict[str, bytes]:
    """Every project file except Git metadata, module cache and machine-local state."""
    files: dict[str, bytes] = {}
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project).as_posix()
        if relative.startswith((".git/", "apm_modules/", ".apm-local/")) or not path.is_file():
            continue
        files[relative] = path.read_bytes()
    return files


def status(project: Path, env: dict[str, str]) -> str:
    return git(project, env, "status", "--porcelain", "--untracked-files=all")


def receipt_destinations(kit: Kit) -> set[str]:
    destinations: set[str] = set()
    for receipt in kit.isolated.config_root.glob("deployment-receipts*/*.json"):
        destinations.update(
            json.loads(receipt.read_text(encoding="utf-8"))["destinations"].values()
        )
    return destinations


def locked_tag(project: Path) -> str:
    lock = load_yaml(project / "apm.lock.yaml")
    entry = next(dep for dep in lock["dependencies"] if dep.get("repo_url") == "test/kit")
    return str(entry.get("resolved_tag") or entry.get("resolved_ref"))
