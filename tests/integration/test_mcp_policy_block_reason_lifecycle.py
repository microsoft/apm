"""Installed-CLI regression: a policy-blocked ``install --mcp`` prints its reason.

Promise (MCP install path, ``src/apm_cli/install/mcp/command.py``): when the
org policy denies the requested MCP server, ``apm install --mcp`` exits 1,
names the denied server and the deciding rule on stderr (as
``apm install <pkg>`` does), never a bare ``Error installing dependencies: 1``,
and leaves ``apm.yml`` unchanged. The org policy is served from a local HTTPS
fixture, so the verdict never depends on network reachability.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.core.remediation import catalog
from tests.integration._policy_scope_kit import PolicyScopeWorld, output, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import local_github_https_fixture

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-policy-outcome-routing"),
]

_MCP = ("install", "--mcp", "fetch", "--", "npx", "-y", "@modelcontextprotocol/server-fetch")


def test_blocked_mcp_install_prints_the_reason(apm_binary_path: Path, tmp_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(
        {
            "name": "contoso-policy",
            "version": "1.0.0",
            "enforcement": "block",
            "mcp": {"deny": ["fetch"]},
        }
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        project = world.consumer(targets=("claude",))
        manifest = (project / "apm.yml").read_bytes()

        blocked = run_step(runner, _MCP, cwd=project, env=env, step="mcp-denied", expected=1)

    text = output(blocked)
    assert "Error installing dependencies: 1" not in text, text
    assert "Policy violation: fetch -- denied by pattern: fetch" in blocked.stderr, text
    assert "(mcp-denylist)" in blocked.stderr, text
    assert "[x]" not in blocked.stdout, text
    assert (project / "apm.yml").read_bytes() == manifest


def test_unfetchable_policy_block_prints_the_reason(apm_binary_path: Path, tmp_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        project = world.consumer(
            targets=("claude",), extra={"policy": {"fetch_failure_default": "block"}}
        )
        manifest = (project / "apm.yml").read_bytes()

        blocked = run_step(runner, _MCP, cwd=project, env=env, step="policy-absent", expected=1)

    text = output(blocked)
    assert "Error installing dependencies: 1" not in text, text
    refusal = next(
        (line for line in blocked.stderr.splitlines() if "fetch_failure_default=block" in line),
        "",
    )
    assert refusal.startswith("[x] "), text
    # The way out of an org-enforced block comes from the remediation catalog
    # and never advertises a bypass.
    # An absent policy has no source to name.
    remedy = catalog()["policy-fetch-failure-blocked"].bind(source="")
    assert refusal.rstrip().endswith(remedy.sentence()), text
    assert "--no-policy" not in refusal, text
    assert (project / "apm.yml").read_bytes() == manifest
