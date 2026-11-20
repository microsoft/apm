"""A refreshed org policy that fails validation keeps the cached policy (#4476).

PROMISE: P-INV-I3 (install and ``audit --ci`` agree) and
P-LAW-transaction.failed_command_preserves_state; owner: policy/discovery.py
``_malformed_refresh``. When the org policy refresh returns valid YAML that
fails policy validation, a cached policy within its stale window stays
applied, exactly as for an unreachable host or a garbage response.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import (
    POLICY_CACHE_DIR,
    PolicyScopeWorld,
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
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
    pytest.mark.promise(
        "P-INV-I3",
        "P-OWN-policy-outcome-routing",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
]

_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _expire_policy_cache(world: PolicyScopeWorld) -> None:
    """Age every cached policy past its TTL, inside the 7-day stale window."""
    metas = sorted((world.isolated.cache_root / POLICY_CACHE_DIR).rglob("*.meta.json"))
    assert metas, "no policy cache entry"
    for meta in metas:
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["cached_at"] = time.time() - 2 * 3600
        meta.write_text(json.dumps(data), encoding="utf-8")


def test_invalid_refresh_keeps_the_cached_deny_for_install_and_audit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    project = world.consumer(dependencies=[package.slug])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")

        # Cache a policy that denies the installed package.
        world.publish_policy({**_POLICY, "dependencies": {"deny": [package.slug]}})
        world.clear_policy_cache()
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="2-cache", expected=1)

        # The refresh is valid YAML that fails validation.
        world.publish_policy({**_POLICY, "enforcement": ["block"]})
        _expire_policy_cache(world)
        before = tree_snapshot(project)

        install = run_step(runner, ("install",), cwd=project, env=env, step="3-install", expected=1)
        out = " ".join(output(install).split())
        assert package.slug in out, out
        assert "proceeding without policy enforcement" not in out, out

        audit = run_step(
            runner, ("audit", "--ci"), cwd=project, env=env, step="4-audit", expected=1
        )
        out = " ".join(output(audit).split())
        assert "dependency-denylist" in out, out
        assert package.slug in out, out
        assert tree_snapshot(project) == before
