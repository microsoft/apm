"""MCP ownership history: current, VS Code and legacy-receipt owners across ops and locks (#4100).

PROMISE: P-OWN-legacy-mcp-ownership-key-migration -- APM <= 0.36 wrote copilot
servers to ``.github/mcp.json`` (legacy-receipt) and the removed vscode
target's servers to ``.vscode/mcp.json``; the next project command moves the
entries APM provably wrote into ``.mcp.json`` and keeps anything else (source:
install/mcp/retired_destinations.py).
PROMISE: P-OWN-lockfile-generator-compatibility -- a lock a newer APM wrote is
refused before any change, whatever MCP owner it records.

MCPOWN (E2, T-6a): ``copilot`` is the current ``.mcp.json`` owner (the 0.36.0
``plural`` fixture after one install migrated it); ``legacy-receipt`` is the
0.36.0 ``plural`` fixture as checked out; ``vscode`` is the 0.36.0
``plural-alias`` fixture (``targets: [vscode]``). Each row runs ONE command on
that state through the scenario kit with org policy on (I1-I9) and credits
the MCPOWN pairs its assignment implies. User scope holds no retired owner
(rule U-MCPOWN-USER), so only ``copilot`` has a user-scope row.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.scenarios._kit import Scenario, credits, mcp_server_problems
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-legacy-mcp-ownership-key-migration",
        "P-OWN-lockfile-generator-compatibility",
    ),
]

FIXTURES = Path(__file__).parents[2] / "fixtures" / "upgrade_0361" / "t2" / "v0.36.0"
# MCPOWN -> (fixture case, declared targets, retired file the fixture holds or None).
OWNERS = {
    "copilot": ("plural", ("claude", "copilot"), None),
    "legacy-receipt": ("plural", ("claude", "copilot"), ".github/mcp.json"),
    "vscode": ("plural-alias", ("vscode",), ".vscode/mcp.json"),
}
_INLINE = ("install", "--mcp", "p-inline", "--", "apm-lifecycle-inert-server", "--stdio")
# Row -> (command, OP, LOCK). Every row exits 0 on every owner.
ROWS = {
    "install": ("install", "install", "consistent"),
    "update": ("update --yes", "update", "consistent"),
    "uninstall": ("uninstall pkg", "uninstall", "consistent"),
    "prune": ("prune", "prune", "consistent"),
    "mcp-install": (_INLINE, "mcp-install", "consistent"),
    "lock-stale": ("install", "install", "stale"),
    "lock-absent": ("install", "install", "absent"),
}
# Rows that reconcile MCP ownership: they heal a retired file (the entry moves to
# .mcp.json and the emptied file APM created is deleted). The others leave it: an
# install with no lock has no proof the entry is APM's; uninstall, prune and the
# install --mcp side door (U7) do not run the heal.
HEALING_ROWS = frozenset({"install", "update", "lock-stale"})
_SECOND_SERVER = {"name": "srv2", "registry": False, "transport": "http", "url": "https://x.test/m"}


def _assignment(op: str, lock: str, owner: str, scope: str = "project") -> dict[str, str]:
    return {"OP": op, "LOCK": lock, "SCOPE": scope, "MCPOWN": owner}


def _start(world, owner: str) -> Scenario:
    """Check out the owner's fixture consumer (and its sibling package) as the project."""
    case, targets, _retired = OWNERS[owner]
    shutil.rmtree(world.project)
    shutil.copytree(FIXTURES / case, world.project)
    shutil.copytree(FIXTURES / "pkg", world.project.parent / "pkg")
    use_policy(world, "allow")
    scenario = Scenario(
        world, {"pkg": "../pkg"}, kinds={"pkg": ("other-file",)}, targets=targets, policy="allow"
    )
    scenario.adopt("pkg")
    if owner == "copilot":
        scenario.run("install")  # migrates the 0.36 claim: .mcp.json is now the owner
        assert not (world.project / ".github/mcp.json").exists()
    return scenario


def _prepare(scenario: Scenario, row: str) -> None:
    project = scenario.world.project
    if row == "lock-absent":
        (project / "apm.lock.yaml").unlink()
    elif row == "lock-stale":
        manifest = project / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["dependencies"]["mcp"].append(dict(_SECOND_SERVER))
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    if row in ("lock-absent", "lock-stale", "mcp-install"):
        scenario.source_moved()


def _servers(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("mcpServers", data.get("servers", {})))


def _assert_ownership(project: Path, owner: str, row: str) -> None:
    retired = OWNERS[owner][2]
    if retired is None:
        assert "srv" in _servers(project / ".mcp.json"), "the current owner keeps srv"
    elif row in HEALING_ROWS:
        assert not (project / retired).exists(), f"{row} heals and deletes {retired}"
        assert "srv" in _servers(project / ".mcp.json"), f"{row} moves srv to .mcp.json"
    else:
        assert "srv" in _servers(project / retired), f"{row} leaves {retired} unhealed"


@pytest.mark.parametrize("owner", sorted(OWNERS))
@pytest.mark.parametrize("row", sorted(ROWS))
def test_one_command_on_an_mcp_owner(lifecycle_world, row, owner) -> None:
    scenario = _start(lifecycle_world, owner)
    _prepare(scenario, row)
    line, op, lock = ROWS[row]
    step = scenario.run(line, credit=[_assignment(op, lock, owner)])
    assert step.result.returncode == 0, step.output
    _assert_ownership(lifecycle_world.project, owner, row)
    if row == "uninstall":
        lock = scenario.lock()
        rows = {dep.repo_url for dep in lock.get_package_dependencies()} if lock else set()
        assert not any(url.rstrip("/").endswith("pkg") for url in rows), rows
        manifest = (lifecycle_world.project / "apm.yml").read_text(encoding="utf-8")
        assert "../pkg" not in manifest, manifest
    if row == "mcp-install":
        problems = mcp_server_problems(scenario, {"p-inline"})
        assert not problems, f"{problems}\n{step.output}"
    if row in ("lock-absent", "lock-stale"):
        # The install repairs the drift: pkg's lock row is back, and the server the
        # stale lock never recorded is installed.
        lock = scenario.lock()
        rows = {dep.repo_url for dep in lock.get_package_dependencies()} if lock else set()
        assert any(url.rstrip("/").endswith("pkg") for url in rows), f"{rows}\n{step.output}"
        if row == "lock-stale" and owner == "copilot":
            problems = mcp_server_problems(scenario, {"srv2"})
            assert not problems, f"{problems}\n{step.output}"
    scenario.finish()


@pytest.mark.parametrize("owner", sorted(OWNERS))
def test_newer_generator_lock_is_refused_for_every_mcp_owner(lifecycle_world, owner) -> None:
    scenario = _start(lifecycle_world, owner)
    lock = lifecycle_world.project / "apm.lock.yaml"
    text, count = re.subn(
        r"^apm_version: .*\n", "apm_version: 99.0.0\n", lock.read_text("utf-8"), flags=re.M
    )
    assert count == 1, "the lock records its generator once"
    lock.write_text(text, encoding="utf-8")
    scenario.source_moved()
    step = scenario.run("install", exit=1)
    evidence = Evidence(
        exit=1,
        output_matches=("newer than this APM",),
        unchanged={root: ("*",) for root in scenario.snapshot_roots()},
    )
    check_obligation(
        f"pair|LOCK=newer-generator|MCPOWN={owner}",
        strength="e2e-evidence",
        before=step.before,
        after=step.after,
        result=step.result,
        evidence=evidence,
    )
    scenario.finish()


def test_user_scope_copilot_owner(lifecycle_world) -> None:
    """User scope has only the current owner (U-MCPOWN-USER): an inline server install."""
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, (), scope="user", targets=("copilot",), policy="allow")
    scenario.source_moved()
    step = scenario.run(_INLINE, credit=[_assignment("mcp-install", "absent", "copilot", "user")])
    assert step.result.returncode == 0, step.output
    problems = mcp_server_problems(scenario, {"p-inline"})
    assert not problems, f"{problems}\n{step.output}"
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_one_command_on_an_mcp_owner[{row}-{owner}]": [
                _assignment(ROWS[row][1], ROWS[row][2], owner)
            ]
            for row in ROWS
            for owner in OWNERS
        },
        **{
            f"test_newer_generator_lock_is_refused_for_every_mcp_owner[{owner}]": [
                {"LOCK": "newer-generator", "MCPOWN": owner}
            ]
            for owner in OWNERS
        },
        "test_user_scope_copilot_owner": [_assignment("mcp-install", "absent", "copilot", "user")],
    },
)
