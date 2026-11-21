"""Package-shape fixtures for install vs ``apm audit --ci`` replay lifecycle chains (#3649).

Each shape authors one realistic source (Git remote through a process-scoped
``insteadOf`` rewrite, or a local directory/file) and returns the
``apm.yml`` dependency entries plus the spelling ``apm uninstall`` takes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

ALL_FILE_TARGETS = ("copilot", "claude", "cursor", "codex", "opencode", "gemini")
# Cursor-native and Claude-import hooks may not share one dependency (by design).
HOOK_TARGETS = ("copilot", "claude", "codex", "opencode", "gemini")
NO_POLICY = "--no-policy"
SKILL = "---\nname: {name}\ndescription: Skill {name}\n---\nSee [ref](references/ref.md).\n"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    return path


def apm_package(root: Path, name: str) -> None:
    """Instructions with every applyTo form, an agent, a prompt and a skill."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    for stem, scope in (
        ("glob", 'applyTo: "**"\n'),
        ("py", 'applyTo: "src/**/*.py"\n'),
        ("multi", 'applyTo: "src/**/*.ts,docs/**"\n'),
        ("none", ""),
    ):
        write(
            root / f".apm/instructions/{stem}.instructions.md",
            f"---\ndescription: {stem}\n{scope}---\n{stem} rules.\n",
        )
    write(
        root / ".apm/agents/rev.agent.md",
        "---\nname: rev\ndescription: Reviewer\n---\nRead [skill](../skills/helper/SKILL.md).\n",
    )
    write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nSay hello.\n")
    write(root / ".apm/skills/helper/SKILL.md", SKILL.format(name="helper"))
    write(root / ".apm/skills/helper/references/ref.md", "Ref.\n")


def hooks_package(root: Path, name: str) -> None:
    """A hooks-only package whose command resolves a script in the package."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    hook = {"type": "command", "command": "${PLUGIN_ROOT}/scripts/check.sh", "timeout": 5}
    write(
        root / ".apm/hooks/guard.json",
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [hook]}]}}, indent=2) + "\n",
    )
    write(root / "scripts/check.sh", "#!/bin/sh\nexit 0\n")


def mcp_package(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    server = {"name": "echo-srv", "registry": False, "transport": "stdio", "command": "echo"}
    dump_yaml(
        {"name": name, "version": "1.0.0", "dependencies": {"mcp": [server]}},
        root / "apm.yml",
    )


def claude_plugin(root: Path, name: str) -> None:
    plugin = {"name": name, "version": "1.0.0", "description": "p"}
    write(root / ".claude-plugin/plugin.json", json.dumps(plugin) + "\n")
    write(root / "agents/helper.md", "---\nname: helper\ndescription: Helper\n---\nHelp.\n")
    write(root / "commands/greet.md", "---\ndescription: Greet\n---\nGreet.\n")
    write(root / "skills/plug/SKILL.md", SKILL.format(name="plug"))
    write(root / "skills/plug/references/ref.md", "Ref.\n")
    hook = {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/scripts/fmt.sh"}
    write(
        root / "hooks/hooks.json",
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [hook]}]}}) + "\n",
    )
    write(root / "scripts/fmt.sh", "#!/bin/sh\nexit 0\n")


def skill_bundle(root: Path, name: str) -> None:
    write(
        root / "skills/alpha/SKILL.md",
        "---\nname: alpha\ndescription: A\n---\nUse [beta](../beta/notes.md).\n",
    )
    write(root / "skills/beta/SKILL.md", "---\nname: beta\ndescription: B\n---\nBeta.\n")
    write(root / "skills/beta/notes.md", "Notes.\n")


def claude_skill(root: Path, name: str) -> None:
    write(root / "SKILL.md", f"---\nname: {name}\ndescription: Solo\n---\nSee guide.\n")
    write(root / "references/guide.md", "Guide.\n")


def linked_package(root: Path, name: str) -> None:
    """An APM package whose instruction and skill reference are contained symlinks."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    write(root / "shared/body.md", "---\ndescription: linked\n---\nShared body.\n")
    write(root / "shared/ref.md", "Shared ref.\n")
    write(root / ".apm/skills/linked/SKILL.md", SKILL.format(name="linked"))
    (root / ".apm/instructions").mkdir(parents=True)
    (root / ".apm/instructions/linked.instructions.md").symlink_to("../../shared/body.md")
    (root / ".apm/skills/linked/references").mkdir(parents=True)
    (root / ".apm/skills/linked/references/ref.md").symlink_to("../../../../shared/ref.md")


@dataclass
class Shape:
    """One authored package shape: its apm.yml entries and uninstall spelling."""

    deps: list[object]
    uninstall: str
    targets: tuple[str, ...] = ALL_FILE_TARGETS
    install_args: tuple[str, ...] = ()
    expect_files: tuple[str, ...] = ()


@dataclass
class ReplayWorld:
    isolated: IsolatedApmEnvironment
    factory: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner
    project: Path
    env: dict[str, str]
    repos: dict[str, LocalGitRepository] = field(default_factory=dict)

    @classmethod
    def create(cls, tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
        isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        env = isolated.subprocess_env()
        factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        runner = ApmLifecycleRunner(
            (str(apm_binary_path),), timeout_seconds=240, scenario_timeout_seconds=1800
        )
        project = isolated.work_root / "proj"
        project.mkdir()
        return cls(isolated, factory, runner, project, env)

    @property
    def sources(self) -> Path:
        return self.isolated.work_root

    def publish(self, name: str, author: Callable[[Path, str], None]) -> str:
        """Publish *author*'s tree as ``https://github.com/test/<name>.git``."""
        repository = self.factory.create(name)
        author(repository.worktree, name)
        self.factory.commit(repository, message=f"publish {name}")
        self.repos[name] = repository
        self.env = self.factory.url_rewrite_subprocess_env_many(
            [
                (repo, f"https://github.com/test/{repo_name}.git")
                for repo_name, repo in self.repos.items()
            ]
        )
        return f"test/{name}"

    def manifest(self, shape: Shape) -> None:
        dump_yaml(
            {
                "name": "proj",
                "version": "1.0.0",
                "targets": list(shape.targets),
                "dependencies": {"apm": list(shape.deps)},
            },
            self.project / "apm.yml",
        )
        write(self.project / ".gitignore", "apm_modules/\n")

    def apm(self, *args: str, cwd: Path | None = None, expect: int | None = 0) -> CommandResult:
        result = self.runner.run(args, cwd=cwd or self.project, env=self.env)
        if expect is not None:
            assert result.returncode == expect, (
                f"apm {' '.join(args)} -> {result.returncode} (expected {expect})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def audit_clean(self, cwd: Path | None = None) -> None:
        self.apm("audit", "--ci", NO_POLICY, "--no-fail-fast", cwd=cwd)

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=cwd, env=self.env, capture_output=True, text=True, check=True
        ).stdout

    def cold_clone(self) -> Path:
        """Commit the project, clone it and drop receipts plus the download cache."""
        self.git(self.project, "init", "-q", "--initial-branch=main")
        self.git(self.project, "config", "user.email", "test@example.com")
        self.git(self.project, "config", "user.name", "APM Test")
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "-qm", "deployed")
        clone = self.isolated.work_root / "clone"
        self.git(self.isolated.work_root, "clone", "-q", str(self.project), str(clone))
        for receipts in (self.isolated.home / ".apm").glob("deployment-receipts*"):
            shutil.rmtree(receipts)
        if self.isolated.cache_root.exists():
            shutil.rmtree(self.isolated.cache_root)
        self.isolated.cache_root.mkdir(parents=True)
        return clone


def lock_bytes(project: Path) -> bytes:
    return (project / "apm.lock.yaml").read_bytes()


def deployed_files(project: Path) -> set[str]:
    """Every ``deployed_files`` claim in the lockfile (empty when it is absent)."""
    path = project / "apm.lock.yaml"
    lock = (load_yaml(path) or {}) if path.exists() else {}
    return {
        path
        for dependency in lock.get("dependencies") or []
        for path in dependency.get("deployed_files") or []
    }


def tree_digest(root: Path) -> dict[str, bytes]:
    """Regular files outside ``apm_modules``/``.git`` mapped to their bytes."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in ("apm_modules", ".git"):
            continue
        if path.is_file():
            files[relative.as_posix()] = path.read_bytes()
    return files
