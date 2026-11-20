"""APMLifecycle: a failed command names the failure and one runnable next step.

PROMISE: I8/I9 -- a command that fails prints what failed, what is available,
and exactly one ``Run 'apm ...'`` next step; running that step reaches the
stated outcome, and the failed command leaves durable state unchanged (I7).
(source: .agents/skills/devx-ux/SKILL.md "Every error names the failure, the
cause, and one next action"; #4205, #4206)
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-outcome.status_matches_state"),
]

_TARGET = "claude"
_NEXT_STEP = re.compile(r"Run '(apm [^']+)'")


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _single_next_step(result: CommandResult) -> tuple[str, ...]:
    """Return the argv of the one ``Run 'apm ...'`` step the failure printed."""
    output = f"{result.stdout}\n{result.stderr}"
    steps = _NEXT_STEP.findall(" ".join(output.split()))
    assert len(steps) == 1, _evidence(result)
    assert "<" not in steps[0], f"next step is a placeholder: {steps[0]!r}"
    argv = tuple(shlex.split(steps[0]))
    assert argv[0] == "apm", steps[0]
    return argv[1:]


class _World:
    def __init__(self, root: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        self.env = self.isolated.subprocess_env()
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        self.packages = LocalPackageFactory(self.isolated.work_root)

    def apm(self, project: Path, *args: str) -> CommandResult:
        result = self.runner.run(args, cwd=project, env=self.env)
        _assert_output_oracles(result)
        return result


def _snap(project: Path) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(project, targets=(_TARGET,))


def _assert_lock_matches_deployed(project: Path, snapshot: LifecycleStateSnapshot) -> None:
    """I1: every deployment record points at a path that exists."""
    assert snapshot.deployment_records
    for record in snapshot.deployment_records:
        assert (project / record.locator.value).exists(), record


def test_run_unknown_script_lists_scripts_and_prints_runnable_next_step(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4205: ``apm run missing`` names the scripts and one runnable step."""
    world = _World(tmp_path / "world", apm_binary_path)
    project = world.packages.create("consumer", targets=(_TARGET,))
    manifest = load_yaml(project.manifest_path)
    manifest["scripts"] = {"start": "echo started", "review": "echo reviewed"}
    dump_yaml(manifest, project.manifest_path)
    before = _snap(project.root)

    failed = world.apm(project.root, "run", "missing")

    assert failed.returncode == 1, _evidence(failed)  # I8
    output = " ".join(f"{failed.stdout}\n{failed.stderr}".split())
    assert "Script or prompt 'missing' not found." in output, _evidence(failed)
    assert "Available scripts in apm.yml: start, review" in output, _evidence(failed)
    assert _snap(project.root) == before, "I7: a failed run changed durable state"

    next_step = _single_next_step(failed)
    assert next_step == ("list",), _evidence(failed)
    listed = world.apm(project.root, *next_step)
    assert listed.returncode == 0, _evidence(listed)
    for name, command in (("start", "echo started"), ("review", "echo reviewed")):
        assert re.search(rf"\b{name}\b.*{command}", listed.stdout), _evidence(listed)


def test_install_skill_miss_lists_skills_and_prints_runnable_next_step(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4206: ``install --skill nope`` names the skills and one runnable step."""
    world = _World(tmp_path / "world", apm_binary_path)
    bundle = world.packages.create("bundle")
    for name in ("alpha", "beta"):
        world.packages.add_skill(bundle, name, f"---\nname: {name}\ndescription: d\n---\nBody\n")
    project = world.packages.create("consumer", targets=(_TARGET,))
    manifest_bytes = project.manifest_path.read_bytes()
    before = _snap(project.root)

    failed = world.apm(project.root, "install", "../bundle", "--skill", "nope")

    assert failed.returncode == 1, _evidence(failed)  # I8
    output = " ".join(f"{failed.stdout}\n{failed.stderr}".split())
    assert "Requested: nope. Available: alpha, beta." in output, _evidence(failed)
    assert project.manifest_path.read_bytes() == manifest_bytes
    assert not (project.root / "apm.lock.yaml").exists()
    assert _snap(project.root) == before, "I7: a failed install changed durable state"

    next_step = _single_next_step(failed)
    assert next_step == ("install", "../bundle", "--skill", "alpha"), _evidence(failed)
    installed = world.apm(project.root, *next_step)
    assert installed.returncode == 0, _evidence(installed)
    assert (project.root / ".claude/skills/alpha/SKILL.md").is_file(), _evidence(installed)
    assert not (project.root / ".claude/skills/beta").exists(), _evidence(installed)
    _assert_lock_matches_deployed(project.root, _snap(project.root))
