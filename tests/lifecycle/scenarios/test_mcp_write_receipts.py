"""Ownership: MCP entries without apm.lock.yaml follow this machine's write evidence (#4737).

PROMISE: P-OWN-legacy-mcp-ownership-key-migration -- MCP removal deletes only
the exact entries APM wrote (source: install/mcp/ownership.py,
install/mcp/write_receipts.py).
PROMISE: P-LAW-ownership.preserve_unowned -- an entry APM cannot prove it wrote
stays, even one identical to a declaration (source:
tests/fixtures/lifecycle_bug_ledger/).
PROMISE: P-INV-I4 -- the next install removes what is no longer deployed;
a kept entry is announced and ``apm audit --ci`` names it (source:
tests/lifecycle/oracles.py).
CELL: OP=install SCOPE=project LOCK=absent PRIM=mcp MCPOWN=copilot.
OWNER: install/mcp/write_receipts.py -- per-entry write evidence bound to this
installation, read by install when the lockfile is absent.

Each row deploys a package's self-defined stdio server to ``.mcp.json``, then
removes the package from apm.yml and deletes apm.lock.yaml before the next
install. Every step runs through ``driver.run`` with org policy on and applies
I1-I9.
"""

from __future__ import annotations

import json

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, credits
from tests.lifecycle.world import use_policy
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I4",
        "P-INV-I5",
        "P-LAW-ownership.preserve_unowned",
        "P-OWN-legacy-mcp-ownership-key-migration",
    ),
]

SERVER = "o-mcp-mcp"
_DECLARATION = {
    "name": SERVER,
    "registry": False,
    "transport": "stdio",
    "command": "apm-lifecycle-inert-server",
    "args": ["--stdio"],
}
# The entry copilot renders for _DECLARATION, as a user could write it by hand.
_RENDERED = {"type": "stdio", "command": "apm-lifecycle-inert-server", "args": ["--stdio"]}
_NO_LOCK = {"OP": "install", "SCOPE": "project", "LOCK": "absent", "MCPOWN": "copilot"}
_KEPT = f"Kept MCP server '{SERVER}' in .mcp.json"

CREDITS = credits(
    __file__,
    {
        "test_an_apm_written_entry_no_longer_deployed_is_retired_without_the_lockfile": [_NO_LOCK],
        "test_an_identical_user_entry_is_kept_without_the_lockfile": [_NO_LOCK],
        "test_an_unproven_entry_is_kept_and_reported_without_the_lockfile[edited]": [_NO_LOCK],
        "test_an_unproven_entry_is_kept_and_reported_without_the_lockfile[unusable-evidence]": [
            _NO_LOCK
        ],
    },
)


def _servers(scenario: Scenario) -> dict:
    path = scenario.world.project / ".mcp.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}


def _start(world) -> Scenario:
    use_policy(world, "allow")
    factory = LocalPackageFactory(world.root / "owned")
    package = factory.create("o-mcp", version="1.0.0", mcp_dependencies=[_DECLARATION])
    return Scenario(world, {"o-mcp": str(package.root)}, kinds={"o-mcp": ("mcp",)}, policy="allow")


def _deploy(scenario: Scenario) -> None:
    scenario.run("install o-mcp")
    scenario.run("approve o-mcp")
    scenario.run("install")
    assert SERVER in _servers(scenario), "the approved MCP server must deploy"


def _remove_package_and_lockfile(scenario: Scenario) -> None:
    """The package leaves apm.yml, then apm.lock.yaml is deleted (not committed, regenerated)."""
    scenario.drop_from_manifest("o-mcp")
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")


def _evidence_file(scenario: Scenario):
    (path,) = scenario.world.apm_home.glob("deployment-receipts/mcp/*.json")
    return path


def test_an_apm_written_entry_no_longer_deployed_is_retired_without_the_lockfile(
    lifecycle_world,
) -> None:
    scenario = _start(lifecycle_world)
    _deploy(scenario)
    _remove_package_and_lockfile(scenario)

    step = scenario.run("install", credit=[_NO_LOCK])

    assert_changes(step.changed, ("project:.mcp.json",), label="retire without the lockfile")
    assert SERVER not in _servers(scenario), "the entry APM wrote stays configured"
    assert _KEPT not in step.output
    rerun = scenario.run("install")
    assert_changes(rerun.changed, (), label="converged install after retirement")
    assert SERVER not in _servers(scenario)
    scenario.run("audit --ci", read_only=True, exit=0)
    scenario.finish()


def test_an_identical_user_entry_is_kept_without_the_lockfile(lifecycle_world) -> None:
    """Equality with the declaration is not write evidence: the user's entry stays."""
    scenario = _start(lifecycle_world)
    held = scenario.user_write(
        ".mcp.json", json.dumps({"mcpServers": {SERVER: _RENDERED}}, indent=2) + "\n"
    )
    before = (lifecycle_world.project / held).read_bytes()
    _deploy(scenario)
    assert (lifecycle_world.project / held).read_bytes() == before, "APM rewrote the user entry"
    _remove_package_and_lockfile(scenario)

    scenario.run("install", credit=[_NO_LOCK], keep={"project": (held,)})

    assert (lifecycle_world.project / held).read_bytes() == before, "a user-held entry changed"
    scenario.finish()


@pytest.mark.parametrize("evidence", ["edited", "unusable-evidence"])
def test_an_unproven_entry_is_kept_and_reported_without_the_lockfile(
    lifecycle_world, evidence
) -> None:
    """An entry the evidence cannot prove unchanged is kept, announced, and audit names it."""
    scenario = _start(lifecycle_world)
    _deploy(scenario)
    config = lifecycle_world.project / ".mcp.json"
    if evidence == "edited":
        data = json.loads(config.read_text(encoding="utf-8"))
        data["mcpServers"][SERVER]["args"] = ["--stdio", "--edited"]
        held = scenario.user_write(".mcp.json", json.dumps(data, indent=2) + "\n")
    else:
        record = _evidence_file(scenario)
        data = json.loads(record.read_text(encoding="utf-8"))
        for write in data["writes"]:
            write["hash"] = "sha256:unusable"
        record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        # Unproven bytes are treated as the user's: they must survive untouched.
        held = scenario.user_write(".mcp.json", config.read_text(encoding="utf-8"))
    before = config.read_bytes()
    _remove_package_and_lockfile(scenario)

    step = scenario.run("install", credit=[_NO_LOCK], keep={"project": (held,)})

    assert config.read_bytes() == before, "an unproven entry was changed or removed"
    assert _KEPT in step.output, step.output
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert f"MCP server '{SERVER}' in .mcp.json" in audit.output, audit.output
    rerun = scenario.run("install")
    assert config.read_bytes() == before
    assert _KEPT in rerun.output, "the keep is reported on every install"
    scenario.finish()
