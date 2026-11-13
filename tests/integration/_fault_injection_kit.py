"""Shared world for installed-CLI fault-injection lifecycle scenarios.

Builds a project with local-path packages so each scenario can inject one
filesystem or process fault mid-pipeline and then assert atomicity and
recovery with the real ``apm`` binary.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

USER_INSTRUCTION = ".github/instructions/user.instructions.md"
USER_INSTRUCTION_TEXT = "Team-authored rule kept by every command.\n"
_INSTALL = ("install", "--no-policy")


def evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout}\nstderr={result.stderr}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _write_package(root: Path, name: str, skills: int, mcp: bool) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if mcp:
        manifest += (
            "dependencies:\n  mcp:\n"
            f"    - name: {name}-srv\n      registry: false\n"
            f"      transport: stdio\n      command: {name}-mcp\n"
        )
    _write(root / "apm.yml", manifest)
    apm = root / ".apm"
    _write(
        apm / f"instructions/{name}.instructions.md",
        f'---\napplyTo: "**/*.py"\ndescription: {name} rules\n---\n{name} rule body\n',
    )
    _write(
        apm / f"agents/{name}.agent.md",
        f"---\nname: {name}-agent\ndescription: {name} agent\n---\n{name} agent body\n",
    )
    _write(apm / f"prompts/{name}.prompt.md", f"---\ndescription: {name}\n---\n{name} prompt\n")
    for index in range(skills):
        skill = f"{name}-skill-{index}"
        _write(
            apm / f"skills/{skill}/SKILL.md",
            f"---\nname: {skill}\ndescription: {skill}\n---\n{skill} body\n",
        )
        _write(apm / f"skills/{skill}/reference.md", f"{skill} reference\n" * 50)


@dataclass(frozen=True)
class FaultWorld:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    project: Path
    packages: tuple[str, ...]

    def apm(self, *args: str) -> CommandResult:
        return self.runner.run(args, cwd=self.project, env=self.env)

    def install(self, *extra: str) -> CommandResult:
        return self.apm(*_INSTALL, *extra)

    def lockfile_bytes(self) -> bytes | None:
        lock = self.project / "apm.lock.yaml"
        return lock.read_bytes() if lock.exists() else None

    def snapshot(self, relative: str) -> ArtifactSnapshot:
        return ArtifactSnapshot.capture(self.project / relative)

    def assert_user_file_intact(self) -> None:
        user_file = self.project / USER_INSTRUCTION
        assert user_file.read_text(encoding="ascii") == USER_INSTRUCTION_TEXT

    def assert_converges(self) -> None:
        """A clean install after the fault converges and ``audit --ci`` agrees."""
        result = self.install()
        assert result.returncode == 0, evidence(result)
        assert "not managed by APM" not in result.stdout + result.stderr, evidence(result)
        lock = self.lockfile_bytes()
        rerun = self.install()
        assert rerun.returncode == 0, evidence(rerun)
        assert self.lockfile_bytes() == lock
        audit = self.apm("audit", "--ci", "--no-policy")
        assert audit.returncode == 0, evidence(audit)
        self.assert_user_file_intact()


def build_world(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    packages: tuple[str, ...] = ("alpha", "beta", "gamma"),
    targets: tuple[str, ...] = ("copilot", "claude", "cursor"),
    skills: int = 1,
    mcp_package: str | None = None,
    installed: bool = True,
) -> FaultWorld:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    for name in packages:
        _write_package(project / "packages" / name, name, skills, mcp=name == mcp_package)
    deps = "".join(f"    - ./packages/{name}\n" for name in packages)
    _write(
        project / "apm.yml",
        "name: consumer\nversion: 1.0.0\n"
        f"targets: [{', '.join(targets)}]\n"
        f"dependencies:\n  apm:\n{deps}",
    )
    _write(project / USER_INSTRUCTION, USER_INSTRUCTION_TEXT)
    (project / ".claude").mkdir(exist_ok=True)
    world = FaultWorld(
        isolated=isolated,
        env=isolated.subprocess_env(),
        runner=ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300),
        project=project,
        packages=packages,
    )
    if installed:
        result = world.install()
        assert result.returncode == 0, evidence(result)
    return world


@contextmanager
def read_only(path: Path) -> Iterator[None]:
    """Make *path* (recursively) read-only for the duration of the block."""
    modes = {p: stat.S_IMODE(p.lstat().st_mode) for p in (path, *path.rglob("*"))}
    try:
        for target, mode in modes.items():
            if not target.is_symlink():
                target.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        yield
    finally:
        for target, mode in modes.items():
            if not target.is_symlink():
                target.chmod(mode)


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
