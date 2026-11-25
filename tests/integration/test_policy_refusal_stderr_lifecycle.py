"""Policy refusals and approve usage errors land on stderr (#3992).

Under a blocking org policy, ``install``, ``install --frozen`` and ``update``
refuse with exit 1, and a bare ``apm approve`` is a usage error with exit 2
(#4262), like ``apm deny`` with no package. A caller
collecting errors with ``2>`` must receive the violation; stdout keeps only
progress. Exit codes are unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
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
    pytest.mark.e2e,
]

_ALLOW_ALL = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _assert_refusal_on_stderr(result: CommandResult, *needles: str) -> None:
    stdout = " ".join(result.stdout.split())
    stderr = " ".join(result.stderr.split())
    for needle in needles:
        assert needle in stderr, f"{needle!r} missing from stderr\n--- stderr ---\n{stderr}"
        assert needle not in stdout, f"{needle!r} leaked to stdout\n--- stdout ---\n{stdout}"


def _assert_no_bypass(result: CommandResult) -> None:
    for token in ("--no-policy", "APM_POLICY_DISABLE", "bypass"):
        assert token not in result.stdout + result.stderr, (token, result.stdout, result.stderr)


def test_policy_refusals_and_approve_usage_error_write_stderr(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = world.publish_package("pkg-a")
    added = world.publish_package("pkg-b")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[package.slug])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")

        world.publish_policy({**_ALLOW_ALL, "dependencies": {"deny": [package.slug]}})
        world.clear_policy_cache()
        violation = f"Policy violation: github.com/{package.slug}"
        # Upstream moves on, so `apm update` has a change to apply (#4166).
        (package.repository.worktree / "NEXT.md").write_text("next\n", encoding="utf-8")
        world.repositories.commit(package.repository, message="pkg-a next")
        # G7 (RA-015): no refusal advertises a bypass, on either stream.
        for step, args in (
            ("2-install-blocked", ("install",)),
            ("3-frozen-blocked", ("install", "--frozen")),
            ("4-update-blocked", ("update", "--yes")),
        ):
            result = run_step(runner, args, cwd=project, env=env, step=step, expected=1)
            _assert_refusal_on_stderr(
                result,
                violation,
                "blocked by org policy",
                "contact your policy admin to allow it.",
                "Run `apm policy status` to see the active policy.",
            )
            _assert_no_bypass(result)

        # RA-015b: a refused add was never kept, so the hint names no apm.yml edit.
        world.publish_policy({**_ALLOW_ALL, "dependencies": {"deny": [added.slug]}})
        world.clear_policy_cache()
        manifest = (project / "apm.yml").read_bytes()
        refused_add = run_step(
            runner, ("install", added.slug), cwd=project, env=env, step="4b-add", expected=1
        )
        _assert_refusal_on_stderr(
            refused_add,
            f"Policy violation: github.com/{added.slug}",
            "Nothing was changed. Choose a package the policy allows, or contact your policy admin to allow it.",
        )
        assert "from apm.yml" not in refused_add.stdout + refused_add.stderr
        _assert_no_bypass(refused_add)
        assert (project / "apm.yml").read_bytes() == manifest

        usage = run_step(runner, ("approve",), cwd=project, env=env, step="5-approve", expected=2)
        _assert_refusal_on_stderr(
            usage, "Error: Specify at least one package", "Try 'apm approve --help'"
        )


def test_required_package_missing_from_lock_converges_with_plain_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A required package absent from the lock is fixed by installing it, never a bypass."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    base = world.publish_package("pkg-a")
    required = world.publish_package("pkg-req")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[base.slug])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        world.publish_policy({**_ALLOW_ALL, "dependencies": {"require": [required.slug]}})
        world.clear_policy_cache()
        manifest = load_yaml(project / "apm.yml")
        manifest["dependencies"]["apm"].append(required.slug)
        dump_yaml(manifest, project / "apm.yml")

        audited = run_step(
            runner,
            ("audit", "--ci", "--no-fail-fast", "--format", "json"),
            cwd=project,
            env=env,
            step="2-audit",
            expected=1,
        )
        failed = {c["name"]: c for c in json.loads(audited.stdout)["checks"] if not c["passed"]}
        assert failed["required-packages-deployed"]["message"].endswith(
            "Hint: Run 'apm install' to record the required packages in the lockfile."
        ), failed
        _assert_no_bypass(audited)

        run_step(runner, ("install",), cwd=project, env=env, step="3-install")
        locked = {row["repo_url"] for row in load_yaml(project / "apm.lock.yaml")["dependencies"]}
        assert required.slug in locked, locked
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="4-audit-clean")
