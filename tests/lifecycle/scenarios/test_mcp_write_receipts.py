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
CELL: OP=install SCOPE=user LOCK=absent PRIM=mcp MCPOWN=copilot (two COPILOT_HOME
profiles, #4801).
CELL: OP=install SCOPE=project LOCK=absent PRIM=mcp TGT=multi-narrowed (#4784),
and the same with no targets: in apm.yml (auto-detected: nothing is reported).
OWNER: install/mcp/write_receipts.py -- per-entry write evidence bound to this
installation, keyed by (runtime, config file, native key), read by install when
the lockfile is absent; kept entries are announced on every later install.

Each row deploys a package's self-defined stdio server (or a local bundle's
server) to ``.mcp.json``, then removes the package from apm.yml and deletes
apm.lock.yaml before the next install. Every step runs through ``driver.run`` with org policy on and applies
I1-I9.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

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
_NO_LOCK_USER = {**_NO_LOCK, "SCOPE": "user"}
_NO_LOCK_RETARGET = {
    "OP": "install",
    "SCOPE": "project",
    "LOCK": "absent",
    "PRIM": "mcp",
    "TGT": "multi-narrowed",
}
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
        "test_a_kept_entry_stays_reported_after_the_lockfile_is_rewritten[edited]": [_NO_LOCK],
        "test_a_kept_entry_stays_reported_after_the_lockfile_is_rewritten[unusable-evidence]": [
            _NO_LOCK
        ],
        "test_each_copilot_home_profile_keeps_its_own_evidence": [_NO_LOCK_USER],
        "test_a_local_bundle_mcp_entry_is_retired_without_the_lockfile": [_NO_LOCK],
        "test_an_apm_written_entry_for_a_runtime_no_longer_targeted_is_retired_without_the_lockfile": [
            _NO_LOCK_RETARGET
        ],
        "test_auto_detected_targets_report_no_kept_entry_without_the_lockfile": [_NO_LOCK],
    },
)


def _servers(scenario: Scenario, config: str = ".mcp.json") -> dict:
    path = scenario.world.project / config
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}


def _start(world, targets: tuple[str, ...] = ("copilot",)) -> Scenario:
    use_policy(world, "allow")
    factory = LocalPackageFactory(world.root / "owned")
    package = factory.create("o-mcp", version="1.0.0", mcp_dependencies=[_DECLARATION])
    return Scenario(
        world,
        {"o-mcp": str(package.root)},
        kinds={"o-mcp": ("mcp",)},
        policy="allow",
        targets=targets,
    )


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


# -- follow-ups (#4801) --------------------------------------------------------

OTHER = "o-keep-mcp"


def _start_two(world) -> Scenario:
    """Two MCP packages: removing ``o-mcp`` leaves ``o-keep``, so the lockfile is rewritten."""
    use_policy(world, "allow")
    factory = LocalPackageFactory(world.root / "owned")
    first = factory.create("o-mcp", version="1.0.0", mcp_dependencies=[_DECLARATION])
    other = factory.create(
        "o-keep", version="1.0.0", mcp_dependencies=[{**_DECLARATION, "name": OTHER}]
    )
    return Scenario(
        world,
        {"o-mcp": str(first.root), "o-keep": str(other.root)},
        kinds={"o-mcp": ("mcp",), "o-keep": ("mcp",)},
        policy="allow",
    )


_UNUSABLE = "Invalid MCP write evidence"


@pytest.mark.parametrize("evidence", ["edited", "unusable-evidence"])
def test_a_kept_entry_stays_reported_after_the_lockfile_is_rewritten(
    lifecycle_world, evidence
) -> None:
    """Install announces and ``audit --ci`` reports a kept entry until the user resolves it."""
    scenario = _start_two(lifecycle_world)
    scenario.run("install o-mcp o-keep")
    scenario.run("approve o-mcp")
    scenario.run("approve o-keep")
    scenario.run("install")
    assert {SERVER, OTHER} <= set(_servers(scenario)), "both approved servers must deploy"
    config = lifecycle_world.project / ".mcp.json"
    if evidence == "edited":
        data = json.loads(config.read_text(encoding="utf-8"))
        data["mcpServers"][SERVER]["args"] = ["--stdio", "--edited"]
        held = scenario.user_write(".mcp.json", json.dumps(data, indent=2) + "\n")
        message, reported = _KEPT, f"MCP server '{SERVER}' in .mcp.json"
    else:
        _evidence_file(scenario).write_text("{not json\n", encoding="utf-8")
        held = scenario.user_write(".mcp.json", config.read_text(encoding="utf-8"))
        message = reported = _UNUSABLE
    before = config.read_bytes()
    _remove_package_and_lockfile(scenario)

    step = scenario.run("install", credit=[_NO_LOCK], keep={"project": (held,)})

    assert config.read_bytes() == before, "an unproven entry was changed or removed"
    assert message in step.output, step.output
    assert scenario.lock_path.is_file(), "the remaining MCP package rewrites apm.lock.yaml"
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert reported in " ".join(audit.output.split()), audit.output
    rerun = scenario.run("install")
    assert config.read_bytes() == before
    assert message in rerun.output, "install announces the kept entry while apm.lock.yaml exists"
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert reported in " ".join(audit.output.split()), audit.output

    # The printed hints: remove the kept entry, and the unusable evidence file.
    scenario.release_user_write(".mcp.json")
    data = json.loads(config.read_text(encoding="utf-8"))
    del data["mcpServers"][SERVER]
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if evidence == "unusable-evidence":
        _evidence_file(scenario).unlink()
    resolved = scenario.run("install")
    assert message not in resolved.output, resolved.output
    scenario.run("audit --ci", read_only=True, exit=0)
    scenario.finish()


def _profile_servers(home) -> dict:
    path = home / "mcp-config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}


def test_each_copilot_home_profile_keeps_its_own_evidence(lifecycle_world) -> None:
    """User scope: each COPILOT_HOME profile's entry is retired only with that profile selected."""
    world = lifecycle_world
    factory = LocalPackageFactory(world.root / "owned")
    package = factory.create("o-mcp", version="1.0.0", mcp_dependencies=[_DECLARATION])
    scenario = Scenario(
        world, {"o-mcp": str(package.root)}, kinds={"o-mcp": ("mcp",)}, scope="user"
    )
    profiles = {name: world.home / f"copilot-{name}" for name in ("a", "b")}
    for home in profiles.values():
        home.mkdir()

    def select(name: str) -> None:
        world.env["COPILOT_HOME"] = str(profiles[name])
        scenario.source_moved()

    select("a")
    scenario.run("install o-mcp")
    scenario.run("approve o-mcp")
    scenario.run("install")
    select("b")
    scenario.run("install")
    assert SERVER in _profile_servers(profiles["a"]), "profile a must hold the entry"
    assert SERVER in _profile_servers(profiles["b"]), "profile b must hold the entry"
    configs = {
        write["config"]
        for write in json.loads(_evidence_file(scenario).read_text(encoding="utf-8"))["writes"]
    }
    assert configs == {(home / "mcp-config.json").as_posix() for home in profiles.values()}, (
        "each profile's write keeps its own evidence"
    )
    kept_a = (profiles["a"] / "mcp-config.json").read_bytes()
    _remove_package_and_lockfile(scenario)

    step = scenario.run("install", credit=[_NO_LOCK_USER])

    assert SERVER not in _profile_servers(profiles["b"]), "the selected profile's entry stays"
    assert (profiles["a"] / "mcp-config.json").read_bytes() == kept_a, "another profile changed"
    assert "Kept MCP server" not in step.output, step.output
    select("a")
    step = scenario.run("install", credit=[_NO_LOCK_USER])
    assert SERVER not in _profile_servers(profiles["a"]), "profile a's evidence was lost"
    assert "Kept MCP server" not in step.output, step.output
    scenario.finish()


_BUNDLE_SERVER = "p-exec-mcp"
_BUNDLE_KINDS = {"p-exec-bundle": ("hook", "mcp", "bin")}


def test_a_local_bundle_mcp_entry_is_retired_without_the_lockfile(lifecycle_world) -> None:
    """A local bundle's MCP write is recorded, so install retires it once nothing deploys it."""
    use_policy(lifecycle_world, "allow")
    specs = {"p-exec-bundle": lifecycle_world.packages["p-exec:bundle"].spec}
    scenario = Scenario(lifecycle_world, specs, policy="allow", kinds=_BUNDLE_KINDS)
    first = scenario.run("install p-exec-bundle")
    found = re.search(r'"(p-exec#[^"]+@sha256:[0-9a-f]+)"', first.output)
    assert found, first.output
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("executables", {}).setdefault("allow", {})[found.group(1)] = {
        "hooks": True,
        "mcp": True,
        "bin": True,
    }
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.model = scenario.model.approve("p-exec-bundle", _BUNDLE_KINDS["p-exec-bundle"])
    scenario.source_moved()
    scenario.run("install p-exec-bundle")
    assert _BUNDLE_SERVER in _servers(scenario), "the approved bundle server must deploy"
    records = list(lifecycle_world.apm_home.glob("deployment-receipts/mcp/*.json"))
    assert records, "the bundle's MCP write must be recorded"
    names = {
        write["name"] for write in json.loads(records[0].read_text(encoding="utf-8"))["writes"]
    }
    assert _BUNDLE_SERVER in names, "the bundle's MCP write must be recorded"
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")

    step = scenario.run("install", credit=[_NO_LOCK])

    assert _BUNDLE_SERVER not in _servers(scenario), "the proven bundle entry stays configured"
    assert "Kept MCP server" not in step.output, step.output
    scenario.finish()


_CURSOR = ".cursor/mcp.json"
_KEPT_CURSOR = f"Kept MCP server '{SERVER}' in {_CURSOR}: APM no longer deploys it for cursor"


def test_an_apm_written_entry_for_a_runtime_no_longer_targeted_is_retired_without_the_lockfile(
    lifecycle_world,
) -> None:
    """The server stays declared; only cursor leaves the targets, and the lock is gone (#4784).

    Install keeps the cursor entry (no removal of a declared server's entry)
    and reports it on that install, every later install and ``audit --ci``,
    until the user removes it.
    """
    scenario = _start(lifecycle_world, targets=("copilot", "cursor"))
    _deploy(scenario)
    assert SERVER in _servers(scenario, _CURSOR), "the server must deploy for cursor"
    scenario.set_targets("copilot")
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")
    cursor = lifecycle_world.project / _CURSOR
    before = cursor.read_bytes()
    held = scenario.user_write(_CURSOR, before.decode("utf-8"))

    step = scenario.run("install", credit=[_NO_LOCK_RETARGET], keep={"project": (held,)})

    assert step.result.returncode == 0, step.output
    assert SERVER in _servers(scenario), "the entry for the targeted runtime must stay"
    # Retired, or kept and reported (the evidence cannot prove ownership): never silent.
    kept_reported = _KEPT_CURSOR in step.output
    assert SERVER not in _servers(scenario, _CURSOR) or kept_reported, (
        f"the entry APM wrote to {_CURSOR} stays configured for a runtime install "
        f"no longer targets, unreported:\n{step.output}"
    )
    assert cursor.read_bytes() == before, "a declared server's entry was changed or removed"
    assert "Kept MCP server" not in step.output.replace(_KEPT_CURSOR, ""), step.output

    # Kept and reported persists: audit --ci and every later install name it.
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert f"MCP server '{SERVER}' in {_CURSOR} (cursor)" in " ".join(audit.output.split())
    rerun = scenario.run("install")
    assert cursor.read_bytes() == before
    assert _KEPT_CURSOR in rerun.output, "install announces the kept entry while the lock exists"
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert f"MCP server '{SERVER}' in {_CURSOR} (cursor)" in " ".join(audit.output.split())

    # The printed hint: remove the entry; nothing is reported any more.
    scenario.release_user_write(_CURSOR)
    data = json.loads(cursor.read_text(encoding="utf-8"))
    del data["mcpServers"][SERVER]
    cursor.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    resolved = scenario.run("install")
    assert "Kept MCP server" not in resolved.output, resolved.output
    assert SERVER in _servers(scenario), "the targeted runtime's entry must stay"
    scenario.run("audit --ci", read_only=True, exit=0)
    scenario.finish()


def test_auto_detected_targets_report_no_kept_entry_without_the_lockfile(lifecycle_world) -> None:
    """No targets: in apm.yml: install keeps every entry quietly, as before (#4784).

    Only the manifest's own targets: say a runtime is no longer targeted;
    auto-detection never makes an entry look abandoned.
    """
    scenario = _start(lifecycle_world, targets=("copilot", "cursor"))
    _deploy(scenario)
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    del data["targets"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.source_moved()
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")
    before = {config: _servers(scenario, config) for config in (".mcp.json", _CURSOR)}

    step = scenario.run("install", credit=[_NO_LOCK])

    assert step.result.returncode == 0, step.output
    assert "Kept MCP server" not in step.output, step.output
    assert {config: _servers(scenario, config) for config in before} == before
    audit = scenario.run("audit --ci", read_only=True)
    assert "kept-mcp-entries" not in audit.output, audit.output
    scenario.finish()
