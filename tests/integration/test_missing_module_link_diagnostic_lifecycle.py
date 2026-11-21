"""APMLifecycle: compile and audit name a deployed file's missing apm_modules link (#3143).

PROMISE: P-INV-I7 -- nothing is skipped silently: a committed deployed agent
file whose context link points into apm_modules is incomplete until
apm install materializes it (source: issue #3143)
CELL: OP=compile|audit SRC=local PRIM=agent SCOPE=project DEPLOYED=link-target-{present,missing}
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_TARGET = "copilot"
_AGENT = ".github/agents/helper.agent.md"
_CONTEXT = ".apm/context/shared.context.md"
_REMEDY = "Run 'apm install' to install the dependencies apm.yml declares."


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _missing_link_lines(result: CommandResult) -> list[str]:
    return [
        line.strip()
        for line in (result.stdout + result.stderr).splitlines()
        if "links to apm_modules/" in line
    ]


@pytest.mark.parametrize("command", ["compile", "audit"])
def test_a_missing_apm_modules_link_target_is_named_once_with_its_remedy(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    packages = LocalPackageFactory(isolated.work_root)
    shared = packages.create("shared")
    packages.add_agent(
        shared,
        "helper",
        "---\nname: helper\ndescription: d\n---\nRead [facts](../context/shared.context.md).\n",
    )
    (shared.root / _CONTEXT).parent.mkdir(parents=True, exist_ok=True)
    (shared.root / _CONTEXT).write_text("# Shared facts\n", encoding="ascii")
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, shared)

    def apm(*args: str) -> CommandResult:
        result = runner.run(args, cwd=project.root, env=env)
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
        return result

    installed = apm("install")
    assert installed.returncode == 0, _evidence(installed)
    agent = (project.root / _AGENT).read_text(encoding="utf-8")
    assert "apm_modules/" in agent, agent

    # Present target: no warning, and the exit code is the command's own.
    present = apm(command)
    assert _missing_link_lines(present) == [], _evidence(present)

    # A fresh clone: committed deployed files, no apm_modules.
    shutil.rmtree(project.root / "apm_modules")
    before = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))
    missing = apm(command)
    lines = _missing_link_lines(missing)
    assert len(lines) == 1, _evidence(missing)
    assert lines[0].startswith(f"[!] {_AGENT} links to apm_modules/"), lines
    assert lines[0].endswith(f"shared.context.md, which is missing. {_REMEDY}"), lines
    # The warning is advisory: exit code and durable state are unchanged.
    assert missing.returncode == (1 if command == "compile" else 0), _evidence(missing)
    assert not (project.root / "apm_modules").exists()
    assert (project.root / _AGENT).read_text(encoding="utf-8") == agent
    if command == "audit":
        assert LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,)) == before
