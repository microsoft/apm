"""Shared fixtures for multi-target integrator and compile lifecycle chains (#3649)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

ALL_TARGETS = ("copilot", "claude", "cursor", "codex", "opencode", "gemini", "antigravity", "kiro")

INSTRUCTIONS = {
    "global": ("Global rules", '"**"', "Always be global."),
    "py": ("Python rules", '"src/**/*.py"', "Python style."),
    "multi": ("Multi scope", '"src/**/*.ts,docs/**"', "Multi scope."),
    "none": ("No applyTo", None, "No scope."),
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def write_package(root: Path, name: str = "pkg") -> Path:
    """Write a package with instructions, an agent with relative links, a prompt and a skill."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    for stem, (description, apply_to, body) in INSTRUCTIONS.items():
        scope = f"applyTo: {apply_to}\n" if apply_to else ""
        write(
            root / f".apm/instructions/{stem}.instructions.md",
            f"---\ndescription: {description}\n{scope}---\n{body}\n",
        )
    write(
        root / ".apm/agents/rev.agent.md",
        "---\nname: rev\ndescription: Reviewer\n---\n"
        "Read [skill](../skills/helper/SKILL.md) and [ext](https://example.com/x.md)"
        " and [anchor](#top).\n",
    )
    write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nSay hello.\n")
    write(
        root / ".apm/skills/helper/SKILL.md",
        "---\nname: helper\ndescription: Helper skill\n---\nHelp.\n",
    )
    return root


def write_project(
    root: Path, targets: tuple[str, ...], dependency: str = "../pkg", extra: str = ""
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write(
        root / "apm.yml",
        "name: proj\nversion: 1.0.0\n"
        f"targets: [{', '.join(targets)}]\n"
        f"dependencies:\n  apm:\n    - path: {dependency}\n{extra}",
    )
    return root


def tree_digest(root: Path, *, skip: tuple[str, ...] = ("apm_modules", ".git")) -> dict[str, str]:
    """Map every regular file under ``root`` (outside ``skip``) to its sha256."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in skip:
            continue
        if path.is_file() and not path.is_symlink():
            digests[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def deployed_files(project: Path) -> list[str]:
    from apm_cli.utils.yaml_io import load_yaml

    lock = load_yaml(project / "apm.lock.yaml") or {}
    files: list[str] = []
    for dependency in lock.get("dependencies") or []:
        files.extend(dependency.get("deployed_files") or [])
    return sorted(files)


@dataclass(frozen=True)
class Scenario:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner

    @classmethod
    def create(cls, tmp_path: Path, apm_binary_path: Path) -> Scenario:
        isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
        runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
        return cls(isolated, isolated.subprocess_env(), runner)

    def run(
        self, *args: str, cwd: Path, expect: int | None = 0, env: dict[str, str] | None = None
    ) -> CommandResult:
        result = self.runner.run(args, cwd=cwd, env=env or self.env)
        if expect is not None:
            assert result.returncode == expect, (
                f"apm {' '.join(args)} -> {result.returncode} (expected {expect})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result


def output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())
