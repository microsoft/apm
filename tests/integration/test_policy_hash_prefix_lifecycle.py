"""APMLifecycle: a policy.hash pin names its algorithm by prefix (#4253).

PROMISE: req-mf-018 -- when ``policy.hash_algorithm`` is omitted, the
``<algo>:`` prefix of ``policy.hash`` selects the algorithm (sha256, sha384,
sha512); the pin is still verified against the policy bytes (source:
docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=policy-status SRC=file SCOPE=project GATE=org-policy-pinned NET=offline
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_POLICY = "name: org\nversion: 1.0.0\nenforcement: block\ndependencies:\n  deny: [evil/*]\n"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.req("req-mf-018")
@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_prefixed_policy_hash_pin_is_verified_with_its_algorithm(
    tmp_path: Path, apm_binary_path: Path, algorithm: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir()
    policy = project / "apm-policy.yml"
    policy.write_text(_POLICY, encoding="ascii")
    digest = hashlib.new(algorithm, _POLICY.encode("ascii")).hexdigest()
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\npolicy:\n  hash: {algorithm}:{digest}\n",
        encoding="ascii",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)
    argv = ("policy", "status", "--policy-source", str(policy), "--json")

    pinned = runner.run(argv, cwd=project, env=env)
    assert pinned.returncode == 0, _evidence(pinned)
    status = json.loads(pinned.stdout)
    assert (status["outcome"], status["error"]) == ("found", None), _evidence(pinned)

    policy.write_text(_POLICY.replace("evil/*", "other/*"), encoding="ascii")
    changed = runner.run(argv, cwd=project, env=env)
    status = json.loads(changed.stdout)
    assert status["outcome"] == "hash_mismatch", _evidence(changed)
    assert f"expected {algorithm}:{digest}" in status["error"], _evidence(changed)
