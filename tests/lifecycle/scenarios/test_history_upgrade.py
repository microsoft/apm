"""History: upgrade from every supported prior lock generator (#4100).

PROMISE: P-OWN-lockfile-generator-compatibility -- the running CLI replays and
rewrites a lockfile an older (or unstamped) APM generated, and refuses one a
newer APM generated before changing anything (source:
deps/lockfile_generator.py).
PROMISE: P-OWN-legacy-mcp-ownership-key-migration -- the first install after an
upgrade moves APM-written copilot MCP entries out of the retired
``.github/mcp.json`` into ``.mcp.json`` and keeps the file (source:
install/mcp/retired_destinations.py, install/mcp/ownership.py).

Prior generators come from ``tests/fixtures/upgrade_0361/t2``: the consumer
``plural`` (targets claude and copilot, one ``path: ../pkg`` dependency, one
self-defined HTTP MCP server) exactly as APM 0.35.1 and 0.36.0 wrote it,
lockfile_version 1. Two more rows derive from the 0.35.1 lock: no
``apm_version`` (generators before the stamp) and ``apm_version: unknown``
(the sentinel a CLI writes when it cannot read its own version). A newer
generator is the refusal neighbor.

Each row runs through ``driver.run`` with org policy on, applies I1-I9 after
every step, pins the exact changed set of the upgrade and replays it with
the I3 pair and a converged rerun (I2).
"""

from __future__ import annotations

import json
import re
import shutil
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, known_defect
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-lockfile-generator-compatibility",
        "P-OWN-legacy-mcp-ownership-key-migration",
    ),
]

FIXTURES = Path(__file__).parents[2] / "fixtures" / "upgrade_0361" / "t2"
TARGETS = ("claude", "copilot")
DEPLOYED = (
    ".agents/skills/pkg-skill/SKILL.md",
    ".claude/rules/style.md",
    ".claude/skills/pkg-skill/SKILL.md",
    ".github/instructions/style.instructions.md",
)
# Generator label -> (fixture release, apm_version line rewrite or None to keep).
PRIOR = {
    "0.35.1": ("v0.35.1", None),
    "0.36.0": ("v0.36.0", None),
    "unstamped": ("v0.35.1", ""),
    "unknown": ("v0.35.1", "apm_version: unknown\n"),
}
# What the first install after the upgrade writes.
UPGRADE_WRITES = (
    "project:apm.lock.yaml",
    "project:.github/mcp.json",
    "project:.apm-local*",
    "project:apm_modules*",
)


def _check_out(world, release: str, generator_line: str | None) -> None:
    """Replace the world project with the fixture consumer and its sibling package."""
    source = FIXTURES / release
    shutil.rmtree(world.project)
    shutil.copytree(source / "plural", world.project)
    shutil.copytree(source / "pkg", world.project.parent / "pkg")
    if generator_line is not None:
        lock = world.project / "apm.lock.yaml"
        text = lock.read_text(encoding="utf-8")
        text, count = re.subn(r"^apm_version: .*\n", generator_line, text, flags=re.M)
        assert count == 1, "the fixture lock records its generator once"
        lock.write_text(text, encoding="utf-8")


def _servers(path: Path) -> set[str]:
    return set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))


def _start(world, release: str, generator_line: str | None) -> Scenario:
    _check_out(world, release, generator_line)
    use_policy(world, "allow")
    scenario = Scenario(
        world, {"pkg": "../pkg"}, kinds={"pkg": ("other-file",)}, targets=TARGETS, policy="allow"
    )
    scenario.adopt("pkg")
    return scenario


@pytest.mark.parametrize("generator", sorted(PRIOR))
def test_upgrade_from_prior_generator(lifecycle_world, generator) -> None:
    scenario = _start(lifecycle_world, *PRIOR[generator])
    project = lifecycle_world.project
    deployed = {path: (project / path).read_bytes() for path in DEPLOYED}
    assert "srv" in _servers(project / ".github/mcp.json")

    step = scenario.run("install")
    # From an "unknown" generator the retired entry stays, unannounced.
    silent = generator == "unknown"
    with (
        known_defect("#4169", r"missing: \['project:\.github/mcp\.json'\]")
        if silent
        else nullcontext()
    ):
        assert_changes(step.changed, UPGRADE_WRITES, label=f"upgrade from {generator}")
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert data["apm_version"] == _running_version(scenario)
    assert {path: (project / path).read_bytes() for path in DEPLOYED} == deployed
    assert "srv" in _servers(project / ".mcp.json")
    with known_defect("#4169", r"retired entry") if silent else nullcontext():
        assert "srv" not in _servers(project / ".github/mcp.json"), "the retired entry is healed"

    scenario.pair()
    rerun = scenario.run("install")
    assert_changes(rerun.changed, (), label="converged install after upgrade")
    scenario.finish()


def test_newer_generator_is_refused_before_any_change(lifecycle_world) -> None:
    scenario = _start(lifecycle_world, "v0.36.0", "apm_version: 99.0.0\n")
    step = scenario.run("install", exit=1)
    assert_changes(step.changed, (), label="newer generator")
    assert "newer than this APM" in step.output, "I7: the refusal names its reason"
    scenario.finish()


def _running_version(scenario: Scenario) -> str:
    result = scenario.run("--version").result
    match = re.search(r"\d+\.\d+\.\d+\S*", result.stdout)
    assert match, result.stdout
    return match.group(0)
