"""APMLifecycle: a dependency chain deeper than the depth cap fails the install (#4277).

PROMISE: req-rs-006 -- exceeding the transitive depth cap (50) fails the
install with a diagnostic naming the chain; it never drops the dependency and
continues with an incomplete graph (source:
docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=install SRC=local PRIM=instruction SCOPE=project LOCK=absent GRAPH=depth-51
"""

from __future__ import annotations

import os
from itertools import pairwise
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
    pytest.mark.promise("P-INV-I7", "P-LAW-transaction.failed_command_preserves_state"),
]

_TARGET = "copilot"
_DEPTH_CAP = 50


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout[-4000:]!r}\nstderr={result.stderr[-4000:]!r}"
    )


@pytest.mark.req("req-rs-006")
def test_install_fails_when_the_chain_exceeds_the_depth_cap(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)
    chain = [packages.create(f"p{index:02d}") for index in range(_DEPTH_CAP + 1)]
    for parent, child in pairwise(chain):
        packages.add_relative_dependency(parent, child)
    packages.add_instruction(chain[-1], "deepest", "---\napplyTo: '**'\ndescription: d\n---\nB\n")
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, chain[0])
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=600)
    before = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))

    result = runner.run(("install",), cwd=project.root, env=env)

    assert result.returncode == 1, _evidence(result)
    flat = " ".join((result.stdout + "\n" + result.stderr).split())
    assert flat.isascii(), _evidence(result)
    assert f"Dependency depth limit ({_DEPTH_CAP}) exceeded:" in flat, _evidence(result)
    assert "p49 -> " in flat and flat.count(" -> ") >= _DEPTH_CAP, _evidence(result)
    assert not (project.root / ".github/instructions/deepest.instructions.md").exists()
    assert not (project.root / "apm.lock.yaml").exists(), _evidence(result)
    after = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))
    assert after.deployment_records == before.deployment_records
