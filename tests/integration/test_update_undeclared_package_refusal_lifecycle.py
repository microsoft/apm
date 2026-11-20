"""APMLifecycle: apm update refuses a named package that apm.yml does not declare (#4263).

PROMISE: outcome.status_matches_state -- an explicitly named package that is
not declared is refused with exit 1 and its name, whether or not apm.yml
declares other APM dependencies (source: issue #4263, update exit contract).
CELL: OP=update SELECT=undeclared SRC=local SCOPE=project LOCK=absent
"""

from __future__ import annotations

import os
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
]

_TARGET = "copilot"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.parametrize("declared", ["none", "other"])
def test_update_refuses_an_undeclared_package(
    tmp_path: Path, apm_binary_path: Path, declared: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)
    packages = LocalPackageFactory(isolated.work_root)
    project = packages.create("consumer", targets=(_TARGET,))
    if declared == "other":
        packages.add_relative_dependency(project, packages.create("alpha"))

    before = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))
    result = runner.run(("update", "--yes", "typo-pkg"), cwd=project.root, env=env)

    assert result.returncode == 1, _evidence(result)
    output = result.stdout + result.stderr
    assert output.isascii(), _evidence(result)
    assert "Package 'typo-pkg' not found in apm.yml" in " ".join(output.split()), _evidence(result)
    assert "nothing to update" not in output, _evidence(result)
    if declared == "none":
        assert "Run 'apm install typo-pkg' to add it." in " ".join(output.split()), _evidence(
            result
        )
    assert LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,)) == before
