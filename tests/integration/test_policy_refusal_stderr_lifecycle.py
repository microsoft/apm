"""Policy refusals and approve usage errors land on stderr (#3992).

Under a blocking org policy, ``install``, ``install --frozen`` and ``update``
refuse with exit 1, and a bare ``apm approve`` refuses with exit 1. A caller
collecting errors with ``2>`` must receive the violation; stdout keeps only
progress. Exit codes are unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import PolicyScopeWorld, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_ALLOW_ALL = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _assert_refusal_on_stderr(result: CommandResult, *needles: str) -> None:
    stdout = " ".join(result.stdout.split())
    stderr = " ".join(result.stderr.split())
    for needle in needles:
        assert needle in stderr, f"{needle!r} missing from stderr\n--- stderr ---\n{stderr}"
        assert needle not in stdout, f"{needle!r} leaked to stdout\n--- stdout ---\n{stdout}"


def test_policy_refusals_and_approve_usage_error_write_stderr(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = world.publish_package("pkg-a")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[package.slug])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")

        world.publish_policy({**_ALLOW_ALL, "dependencies": {"deny": [package.slug]}})
        world.clear_policy_cache()
        violation = f"Policy violation: github.com/{package.slug}"
        for step, args in (
            ("2-install-blocked", ("install",)),
            ("3-frozen-blocked", ("install", "--frozen")),
            ("4-update-blocked", ("update", "--yes")),
        ):
            result = run_step(runner, args, cwd=project, env=env, step=step, expected=1)
            _assert_refusal_on_stderr(
                result, violation, "blocked by org policy", "--no-policy` for one-off bypass"
            )

        usage = run_step(runner, ("approve",), cwd=project, env=env, step="5-approve", expected=1)
        _assert_refusal_on_stderr(usage, "Specify at least one package")
