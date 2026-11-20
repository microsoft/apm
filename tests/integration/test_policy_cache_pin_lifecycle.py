"""Cached org policy content is verified against its pin before use (#4218).

PROMISE: transaction.failed_command_preserves_state and
outcome.status_matches_state (tests/fixtures/lifecycle_bug_ledger.json);
owner: policy/discovery.py _read_cache_entry (the only policy cache reader).

The installed CLI discovers a pinned org policy (``policy.hash``) served over
the loopback GitHub HTTPS fixture and caches it. When the cached policy
document is then altered on disk while its metadata (and recorded hash) stay
as written, the altered content is never applied: the next command fetches
the policy again, verifies the pin on the fresh bytes and keeps the project
as the verified policy allows. When the policy cannot be fetched again, the
pin cannot be verified: the command fails with the policy hash mismatch
outcome and writes nothing, instead of proceeding without policy.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml

from tests.integration._policy_scope_kit import (
    ORG,
    POLICY_CACHE_DIR,
    PolicyScopeWorld,
    deployed_skills,
    output,
    run_step,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
    pytest.mark.promise(
        "P-OWN-cached-policy-shape",
        "P-LAW-outcome.status_matches_state",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
]

_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _edit_cached_policy(world: PolicyScopeWorld, package: str) -> None:
    """Deny *package* in every cached policy document; metadata stays as written."""
    documents = sorted((world.isolated.cache_root / POLICY_CACHE_DIR).rglob("*.yml"))
    assert documents, "no cached policy document"
    for path in documents:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        dependencies = data.get("dependencies") or {}
        dependencies["deny"] = [*(dependencies.get("deny") or []), package]
        data["dependencies"] = dependencies
        path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_altered_cached_policy_is_not_applied(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    assert world.policy_repository is not None
    policy_bytes = (world.policy_repository.worktree / "apm-policy.yml").read_bytes()
    pin = "sha256:" + hashlib.sha256(policy_bytes).hexdigest()
    project = world.consumer(dependencies=[package.slug], extra={"policy": {"hash": pin}})
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="install", expected=0)
        assert deployed_skills(project) == {"pkg-a-skill"}
        before = tree_snapshot(project)

        _edit_cached_policy(world, f"{ORG}/pkg-a")
        result = run_step(runner, ("install",), cwd=project, env=env, step="reinstall", expected=0)

        out = " ".join(output(result).split())
        assert "blocked by org policy" not in out, out
        assert deployed_skills(project) == {"pkg-a-skill"}, out
        assert tree_snapshot(project) == before, out


def test_altered_cached_policy_without_a_refresh_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    assert world.policy_repository is not None
    policy_bytes = (world.policy_repository.worktree / "apm-policy.yml").read_bytes()
    pin = "sha256:" + hashlib.sha256(policy_bytes).hexdigest()
    project = world.consumer(dependencies=[package.slug], extra={"policy": {"hash": pin}})
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="install", expected=0)
    before = tree_snapshot(project)

    _edit_cached_policy(world, f"{ORG}/pkg-a")
    result = run_step(runner, ("install",), cwd=project, env=env, step="offline", expected=1)

    out = " ".join(output(result).split())
    assert "Policy hash mismatch" in out, out
    assert "proceeding without policy enforcement" not in out, out
    assert tree_snapshot(project) == before, out
