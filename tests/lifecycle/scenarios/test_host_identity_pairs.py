"""The lock's host identity (HOSTID) across audit and lock states (#4096).

PROMISE: P-INV-I1 -- the lock row records the dependency's host exactly
(host, port, proxy prefix, unique key), and ``apm audit`` replays it from that
row (source: deps/lockfile.py LockedDependency host/port/registry_prefix).
PROMISE: P-OWN-lockfile-generator-compatibility -- a lock a newer APM wrote is
refused before anything is written, whatever host its rows name.

E1 HOSTID shapes: ``bare-default`` (github.com shorthand), ``qualified-host-port``
(a ``git:`` object on a GHES host with a port) and ``proxy-logical`` (shorthand
through the registry proxy). Each row asserts the lock row before it credits.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from tests.lifecycle.scenarios._kit import Scenario, credits, serve_repository
from tests.lifecycle.scenarios._sources import source_fields
from tests.lifecycle.world import use_policy, use_registry_proxy
from tests.lifecycle.world.routes import PROXY_HOST, PROXY_PREFIX
from tests.lifecycle.world.services import GITHUB_HOST, HOST_ALIASES
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-OWN-lockfile-generator-compatibility"),
]

_REPO = "lifecycle/p-skill"
_GHES = HOST_ALIASES["ghes"][0]
_PORT = 8443
_SHORTHAND = f"{_REPO}#v1.0.0"
# HOSTID -> (manifest entry, expected lock row (host, port, registry_prefix, key)).
_SHAPES = {
    "bare-default": (_SHORTHAND, (GITHUB_HOST, None, None, _REPO)),
    "qualified-host-port": (
        {"git": f"https://{_GHES}:{_PORT}/{HOST_ALIASES['ghes'][1]}/p-skill.git", "ref": "v1.0.0"},
        (_GHES, _PORT, None, f"{_GHES}:{_PORT}/{_REPO}"),
    ),
    "proxy-logical": (_SHORTHAND, (PROXY_HOST, None, PROXY_PREFIX, _REPO)),
}


def _declared(world, hostid: str) -> Scenario:
    """p-skill declared in apm.yml from *hostid*'s host, nothing installed, no lock."""
    use_policy(world, "allow")
    if hostid == "proxy-logical":
        use_registry_proxy(world, only=False)
    scenario = Scenario(world, {"p-skill": _SHORTHAND}, policy="allow")
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {})["apm"] = [_SHAPES[hostid][0]]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.adopt("p-skill", lock="absent")
    return scenario


def _assert_row(scenario: Scenario, hostid: str) -> None:
    rows = [r for r in scenario.lock().get_package_dependencies() if r.repo_url.endswith("p-skill")]
    assert len(rows) == 1, [r.repo_url for r in rows]
    row = rows[0]
    found = (row.host, row.port, row.registry_prefix, row.get_unique_key())
    assert found == _SHAPES[hostid][1], f"{hostid} lock row: {found}"


_ABSENT_SHAPES = ("proxy-logical", "qualified-host-port")


@pytest.mark.parametrize("hostid", sorted(_SHAPES))
def test_audit_replays_each_host_identity(lifecycle_world, hostid) -> None:
    scenario = _declared(lifecycle_world, hostid)
    first = [{"LOCK": "absent", "HOSTID": hostid}] if hostid in _ABSENT_SHAPES else []
    scenario.run("install", credit=first)
    _assert_row(scenario, hostid)
    scenario.assert_deployed()
    # --ci fails on drift or a failed replay; bare audit only reports them.
    step = scenario.run(
        "audit --ci", read_only=True, exit=0, credit=[{"OP": "audit", "HOSTID": hostid}]
    )
    assert step.result.returncode == 0, step.output
    _assert_row(scenario, hostid)
    scenario.finish()


@pytest.mark.parametrize("hostid", sorted(_SHAPES))
def test_a_newer_lock_is_refused_for_each_host_identity(lifecycle_world, hostid) -> None:
    scenario = _declared(lifecycle_world, hostid)
    scenario.run("install")
    _assert_row(scenario, hostid)
    text = scenario.lock_path.read_text(encoding="utf-8")
    text, count = re.subn(r"^apm_version: .*$", "apm_version: 99.0.0", text, flags=re.M)
    assert count == 1, text
    scenario.lock_path.write_text(text, encoding="utf-8")
    scenario.model = scenario.model.with_lock("newer-generator")
    step = scenario.run(
        "install",
        exit=1,
        credit=[{"LOCK": "newer-generator", "HOSTID": hostid}],
        refusal=True,
    )
    assert "Install APM 99.0.0 or newer" in step.output, step.output
    scenario.finish()


# -- a git marketplace plugin through the proxy and from a ported host ------------

_PORTED_MARKET = "ported-market"


def _ported_catalog(world) -> str:
    """A git catalog whose p-skill plugin lives on the GHES host with a port."""
    plugin = {
        "name": "p-skill",
        "description": "Lifecycle world package p-skill",
        "source": {"source": "url", "url": _SHAPES["qualified-host-port"][0]["git"]},
        "ref": "v1.0.0",
    }
    document = {"name": _PORTED_MARKET, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
    tree = world.root / _PORTED_MARKET
    (tree / ".claude-plugin").mkdir(parents=True)
    (tree / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )
    repos = LocalGitRepositoryFactory(world.root / "repos", env=world.env)
    repository = repos.create(_PORTED_MARKET, source_tree=tree)
    repos.commit(repository, message="ported catalog")
    serve_repository(world, f"lifecycle/{_PORTED_MARKET}", repository.origin)
    return f"https://github.com/lifecycle/{_PORTED_MARKET}"


_MARKET_SHAPES = ("proxy-logical", "qualified-host-port")


@pytest.mark.parametrize("hostid", _MARKET_SHAPES)
def test_a_git_marketplace_plugin_records_its_host_identity(lifecycle_world, hostid) -> None:
    use_policy(lifecycle_world, "allow")
    if hostid == "proxy-logical":
        catalog, market = lifecycle_world.services.marketplace_url, "lifecycle-market"
        use_registry_proxy(lifecycle_world, only=False)
    else:
        catalog, market = _ported_catalog(lifecycle_world), _PORTED_MARKET
    scenario = Scenario(lifecycle_world, {"p-skill": f"p-skill@{market}"}, policy="allow")
    scenario.run(f"marketplace add {catalog}", forbidden={"project": ("*",)}, allowed={})
    credit = {"SRC": "marketplace-git", "HOSTID": hostid}
    scenario.run("install p-skill", credit=[credit], **source_fields("marketplace-git"))
    _assert_row(scenario, hostid)
    scenario.assert_deployed()
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_audit_replays_each_host_identity[{hostid}]": [
                {"OP": "audit", "HOSTID": hostid},
                *([{"LOCK": "absent", "HOSTID": hostid}] if hostid in _ABSENT_SHAPES else []),
            ]
            for hostid in _SHAPES
        },
        **{
            f"test_a_git_marketplace_plugin_records_its_host_identity[{hostid}]": [
                {"SRC": "marketplace-git", "HOSTID": hostid}
            ]
            for hostid in _MARKET_SHAPES
        },
        **{
            f"test_a_newer_lock_is_refused_for_each_host_identity[{hostid}]": [
                {"LOCK": "newer-generator", "HOSTID": hostid}
            ]
            for hostid in _SHAPES
        },
    },
)
