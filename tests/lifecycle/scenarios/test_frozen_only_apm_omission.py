"""Frozen replay preserves an intentionally omitted local MCP declaration (#5415).

PROMISE: P-INV-I2 -- a converged frozen skills-only install preserves bytes
(source: docs/src/content/docs/reference/baseline-checks.md).
CELL: OP=install SRC=local-path PRIM=skill,mcp SCOPE=project FROZEN SELECT=apm
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world.contract import World
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [pytest.mark.promise("P-INV-I2", "P-INV-I7", "P-INV-I8")]


@pytest.mark.parametrize("target", ["agent-skills", "opencode"])
@pytest.mark.parametrize("predeclared", [False, True])
def test_frozen_only_apm_replays_local_omission(
    lifecycle_world: World, target: str, predeclared: bool
) -> None:
    world = lifecycle_world
    source = world.project.parent / "source"
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Minimal test skill.\n---\nTest.\n",
        encoding="ascii",
    )
    (source / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "frozen-mcp-repro",
                "version": "1.0.0",
                "includes": ["skills/demo/"],
                "dependencies": {
                    "mcp": [
                        {
                            "name": "sample",
                            "registry": False,
                            "transport": "stdio",
                            "command": "example-command-not-executed",
                        }
                    ]
                },
            }
        ),
        encoding="ascii",
    )
    manifest_path = world.project / "apm.yml"
    if predeclared:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["dependencies"]["apm"] = ["../source"]
        manifest_path.write_text(yaml.safe_dump(manifest), encoding="ascii")

    before_install = LifecycleStateSnapshot.capture(world.project, targets=(target,))
    args = ["install"]
    if not predeclared:
        args.append("../source")
    args.extend(("--target", target, "--only", "apm"))
    installed = driver.run(world, args)
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    before = LifecycleStateSnapshot.capture(world.project, targets=(target,))
    assert before.lockfile_bytes != before_install.lockfile_bytes
    lock = yaml.safe_load(before.lockfile_bytes)
    assert lock["mcp_omissions"]["sample"]["reason"] == "only-apm"
    assert not lock.get("mcp_configs")
    assert not lock.get("mcp_servers")
    assert any("demo" in state.relative_path for state in before.files)

    frozen = driver.run(world, ("install", "--target", target, "--only", "apm", "--frozen"))
    after = LifecycleStateSnapshot.capture(world.project, targets=(target,))
    assert frozen.returncode == 0, (frozen.stdout, frozen.stderr)
    assert after == before
