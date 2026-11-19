"""Org ``executables.deny_all`` suppresses every lifecycle script tier (#3596, #3649).

Promise under test: when the org policy sets ``executables.deny_all: true``
every lifecycle tier (project, even when trusted, and user) is suppressed on
install, update, uninstall, and compile -- including the new ``pre-compile``
event -- and the same scripts fire once the ceiling is lifted (control).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    lock_repo_urls,
    output,
    run_step,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="bash lifecycle commands"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_EVENTS = (
    "pre-install",
    "post-install",
    "pre-update",
    "post-update",
    "pre-uninstall",
    "post-uninstall",
    "pre-compile",
)

_BASE_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _lifecycle_block(marker: Path, tier: str) -> dict[str, object]:
    return {
        event: [{"type": "command", "bash": f"echo {tier}:{event} >> '{marker}'"}]
        for event in _EVENTS
    }


def _fired(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return [line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line]


def test_deny_all_suppresses_every_lifecycle_tier_across_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    world.publish_policy({**_BASE_POLICY, "executables": {"deny_all": True}})
    marker = world.isolated.root / "fired.log"
    project = world.consumer(
        dependencies=[f"{pkg_a.slug}#v1.0.0"],
        extra={"lifecycle": _lifecycle_block(marker, "project")},
    )
    user_manifest = world.isolated.config_root / "apm.yml"
    dump_yaml(
        {"name": "user", "version": "0.0.0", "lifecycle": _lifecycle_block(marker, "user")},
        user_manifest,
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        run_step(runner, ("lifecycle", "trust"), cwd=project, env=env, step="0-trust")

        r = run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        assert lock_repo_urls(project) == {pkg_a.slug}
        assert _fired(marker) == [], output(r)
        assert "executables.deny_all" in output(r)

        r = run_step(runner, ("update", "--yes"), cwd=project, env=env, step="2-update")
        assert _fired(marker) == [], output(r)

        r = run_step(runner, ("compile",), cwd=project, env=env, step="3-compile")
        assert _fired(marker) == [], output(r)

        r = run_step(runner, ("uninstall", pkg_a.slug), cwd=project, env=env, step="4-uninstall")
        assert _fired(marker) == [], output(r)
        assert lock_repo_urls(project) == set()

        # Control: lift the ceiling; the same trusted scripts now fire.
        world.publish_policy(_BASE_POLICY)
        world.clear_policy_cache()
        r = run_step(
            runner,
            ("install", f"{pkg_a.slug}#v1.0.0"),
            cwd=project,
            env=env,
            step="5-install-control",
        )
        fired = _fired(marker)
        assert "project:pre-install" in fired, output(r)
        assert "project:post-install" in fired, output(r)
        assert "user:pre-install" in fired, output(r)

        r = run_step(runner, ("compile",), cwd=project, env=env, step="6-compile-control")
        assert "project:pre-compile" in _fired(marker), output(r)

        manifest = load_yaml(project / "apm.yml")
        assert manifest["lifecycle"] == _lifecycle_block(marker, "project")
