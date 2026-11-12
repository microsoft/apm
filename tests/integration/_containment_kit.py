"""Shared fixtures for project-containment lifecycle guards.

Builds a hermetic project with one local APM package and runs the real
``apm`` binary against it. Outside-the-project directories live next to the
project so tests can assert they stay byte-identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

PROMPT = "---\ndescription: Fixture prompt\n---\nPrompt body.\n"
AGENT = "---\nname: helper\ndescription: Fixture agent\n---\nAgent body.\n"
INSTRUCTION = '---\napplyTo: "**"\ndescription: Fixture rule\n---\nRule body.\n'
SKILL = "---\nname: review\ndescription: Fixture skill\n---\nSkill body.\n"
HOOK = '{"hooks": {"sessionStart": [{"type": "command", "bash": "echo ok"}]}}\n'


@dataclass(frozen=True)
class ContainmentKit:
    isolated: IsolatedApmEnvironment
    apm_binary_path: Path
    package: Path
    project: Path
    outside: Path

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        env = self.isolated.subprocess_env(overrides={"NO_COLOR": "1"})
        return ApmLifecycleRunner((str(self.apm_binary_path),)).run(
            args, cwd=cwd or self.project, env=env, scenario_id="project-containment"
        )

    def write_manifest(self, targets: tuple[str, ...], *, mcp: bool = False) -> None:
        lines = [
            "name: consumer",
            "version: 0.0.1",
            f"target: [{', '.join(targets)}]",
            "dependencies:",
            "  apm:",
            f"    - {self.package.as_posix()}",
        ]
        if mcp:
            lines += [
                "  mcp:",
                "    - name: localsrv",
                "      registry: false",
                "      transport: stdio",
                "      command: echo",
                "      args: [ok]",
            ]
        (self.project / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_containment_kit(tmp_path: Path, apm_binary_path: Path) -> ContainmentKit:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    package = isolated.work_root / "pkg"
    _write(package / "apm.yml", "name: pkg\nversion: 1.0.0\n")
    _write(package / ".apm/prompts/p.prompt.md", PROMPT)
    _write(package / ".apm/agents/helper.agent.md", AGENT)
    _write(package / ".apm/instructions/rule.instructions.md", INSTRUCTION)
    _write(package / ".apm/skills/review/SKILL.md", SKILL)
    _write(package / ".apm/hooks/h.json", HOOK)
    project = isolated.work_root / "project"
    project.mkdir()
    outside = isolated.work_root / "outside"
    outside.mkdir()
    return ContainmentKit(isolated, apm_binary_path, package, project, outside)


def snapshot(root: Path) -> dict[str, bytes]:
    """Return every regular file under *root* keyed by POSIX relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def output(result: CommandResult) -> str:
    """Return combined output with console line wrapping collapsed."""
    return " ".join((result.stdout + result.stderr).split())
