"""Org-policy field promises the generated corpus has no factor for yet (QL gap sweep).

PROMISE: P-INV-I4 -- an org policy restriction never widens what install deploys
(source: docs/src/content/docs/reference/policy-schema.md).
PROMISE: P-INV-I7 -- a refused or restricted install names its cause.

Each row authors its own packages and serves its own ``apm-policy.yml`` from a
unique org (``_serve_policy``), so the shared world variants stay untouched.
A promise row is paired with a control row that proves the fixture really
deploys what the promise restricts, so the promise row can never pass
vacuously. Rows whose promise is under private tracking are strict xfail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world.services import REGISTRY_SERVER
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

_PRIVATE = "see private tracking"
_SERVER = "t-transitive-srv"
_MCP_CONFIGS = (".mcp.json", ".vscode/mcp.json", ".cursor/mcp.json")


def _serve_policy(world, org: str, document: dict) -> None:
    """Serve *document* as ``<org>/.github/apm-policy.yml`` and point the project at *org*."""
    tree = world.root / f"policy-src-{org}"
    tree.mkdir()
    (tree / "apm-policy.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    world.services.publish(f"{org}/.github", tree, tag=None)
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remotes = subprocess.run(
        (*git, "remote"), env=world.env, capture_output=True, text=True, check=True
    ).stdout.split()
    verb = "set-url" if "origin" in remotes else "add"
    url = f"https://github.com/{org}/consumer.git"
    subprocess.run((*git, "remote", verb, "origin", url), env=world.env, check=True)


def _skill(factory: LocalPackageFactory, package, name: str) -> None:
    factory.add_skill(
        package, name, f"---\nname: {name}\ndescription: {name}.\n---\n\nUse {name}.\n"
    )


# A transitive MCP server reaches a project three ways: the one-run flag admits a
# self-defined one, an approval pins it, or a plain registry server needs neither.
PATHS = ("flag", "approved", "registry")
_CONFIGURED_NAME = {"flag": _SERVER, "approved": _SERVER, "registry": "p-registry"}


def _author(world, path: str) -> tuple[str, Path, str]:
    """t-parent -> t-child; t-child declares the path's MCP server.

    Returns the parent spec, the child root and the server name the policy names.
    """
    factory = LocalPackageFactory(world.root / "g1")
    if path == "registry":
        server, name = REGISTRY_SERVER, REGISTRY_SERVER
    else:
        server = {
            "name": _SERVER,
            "registry": False,
            "transport": "stdio",
            "command": "apm-lifecycle-inert-server",
            "args": ["--stdio"],
        }
        name = _SERVER
    child = factory.create("t-child", version="1.0.0", mcp_dependencies=[server])
    _skill(factory, child, "t-child")
    parent = factory.create("t-parent", version="1.0.0", dependencies=["../t-child"])
    _skill(factory, parent, "t-parent")
    if path != "approved":
        _gate_off(world)
    return str(parent.root), Path(child.root).resolve(), name


def _gate_off(world) -> None:
    """A project with no ``executables`` block: the default executables gate is off."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.pop("executables", None)
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _install(world, path: str, parent: str, child: Path):
    """Reach the transitive server the path's way; return the deciding install."""
    if path == "flag":
        return driver.run(world, ["install", parent, "--trust-transitive-mcp"])
    if path == "registry":
        return driver.run(world, ["install", parent])
    first = driver.run(world, ["install", parent])
    assert first.returncode == 0, _text(first)
    approve = driver.run(world, ["approve", f"local:{child}"])
    assert approve.returncode == 0, _text(approve)
    return driver.run(world, ["install"])


def _install_under_policy(world, path: str, parent: str, child: Path, **mcp):
    """Serve the trust_transitive policy, then reach the server the path's way.

    An approval needs an install the policy has not yet refused, so the approved
    path pins the server first and meets the policy on the next install.
    """
    if path != "approved":
        _transitive_policy(world, path, **mcp)
        return _install(world, path, parent, child)
    first = driver.run(world, ["install", parent])
    assert first.returncode == 0, _text(first)
    approve = driver.run(world, ["approve", f"local:{child}"])
    assert approve.returncode == 0, _text(approve)
    _transitive_policy(world, path, **mcp)
    return driver.run(world, ["install"])


# An unrelated deny entry keeps the policy non-empty: a policy that only restates
# defaults is reported "present but empty" and is not enforced at all.
_UNRELATED_DENY = "io.github.unrelated/never-requested"


def _transitive_policy(world, path: str, **mcp) -> None:
    _serve_policy(
        world,
        f"policy-g1-{path}",
        {
            "name": "g1",
            "version": "1.0",
            "enforcement": "block",
            "mcp": {"deny": [_UNRELATED_DENY], **mcp},
        },
    )


def _configured(world, name: str) -> bool:
    return any(
        name in (world.project / rel).read_text(encoding="utf-8")
        for rel in _MCP_CONFIGS
        if (world.project / rel).is_file()
    )


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


# -- G1: mcp.trust_transitive (policy-schema.md, mcp section) ----------------------


@pytest.mark.parametrize("path", PATHS)
def test_transitive_mcp_without_org_policy_is_admitted(lifecycle_world, path) -> None:
    """Control: a project with no org policy reaches the server as before."""
    parent, child, _name = _author(lifecycle_world, path)
    result = _install(lifecycle_world, path, parent, child)
    assert result.returncode == 0, _text(result)
    assert _configured(lifecycle_world, _CONFIGURED_NAME[path]), _text(result)


@pytest.mark.parametrize("path", PATHS)
def test_transitive_mcp_on_the_allow_list_is_admitted(lifecycle_world, path) -> None:
    """Control: trust_transitive false still admits a server mcp.allow names."""
    parent, child, name = _author(lifecycle_world, path)
    _transitive_policy(lifecycle_world, path, trust_transitive=False, allow=[name])
    result = _install(lifecycle_world, path, parent, child)
    assert result.returncode == 0, _text(result)
    assert _configured(lifecycle_world, _CONFIGURED_NAME[path]), _text(result)


@pytest.mark.parametrize("path", PATHS)
def test_trust_transitive_false_refuses_an_unlisted_transitive_server(
    lifecycle_world, path
) -> None:
    """mcp.trust_transitive false: install refuses a transitive server mcp.allow does not name."""
    parent, child, _name = _author(lifecycle_world, path)
    result = _install_under_policy(lifecycle_world, path, parent, child, trust_transitive=False)
    assert result.returncode == 1, _text(result)
    assert "trust_transitive" in _text(result), _text(result)
    assert not _configured(lifecycle_world, _CONFIGURED_NAME[path]), _text(result)


@pytest.mark.parametrize("path", PATHS)
def test_trust_transitive_false_fails_audit_for_an_unlisted_transitive_server(
    lifecycle_world, path
) -> None:
    """A server installed before the policy applied fails ``audit --ci`` once it does."""
    parent, child, name = _author(lifecycle_world, path)
    installed = _install(lifecycle_world, path, parent, child)
    assert installed.returncode == 0, _text(installed)
    _transitive_policy(lifecycle_world, path, trust_transitive=False)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    assert audit.returncode == 1, _text(audit)
    # The policy names the cause: a one-run flag install already fails audit on
    # its own (the consumer trust check), so the server name alone proves nothing.
    assert "trust_transitive" in _text(audit), _text(audit)
    assert name in _text(audit), _text(audit)


def test_a_direct_dependency_server_is_not_transitive(lifecycle_world) -> None:
    """mcp.trust_transitive judges depth > 1 only: a direct dependency's server installs."""
    factory = LocalPackageFactory(lifecycle_world.root / "g1-direct")
    package = factory.create("t-direct", version="1.0.0", mcp_dependencies=[REGISTRY_SERVER])
    _skill(factory, package, "t-direct")
    _gate_off(lifecycle_world)
    _transitive_policy(lifecycle_world, "direct", trust_transitive=False)
    result = driver.run(lifecycle_world, ["install", str(package.root)])
    assert result.returncode == 0, _text(result)
    assert _configured(lifecycle_world, "p-registry"), _text(result)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    assert audit.returncode == 0, _text(audit)


# Policy activation (regent ruling): an explicit ``trust_transitive: false`` counts
# as set; a policy that states nothing keeps no enforcement; any other non-default
# field makes the default ``false`` apply. Shown on the registry path.
_ACTIVATION = {
    "explicit-false-alone": {"trust_transitive": False},
    "default-under-another-field": {"deny": [_UNRELATED_DENY]},
}


def _activation_policy(world, case: str, mcp: dict | None) -> None:
    document = {"name": "g1", "version": "1.0", "enforcement": "block"}
    if mcp is not None:
        document["mcp"] = mcp
    _serve_policy(world, f"policy-g1-activation-{case}", document)


def test_a_policy_that_states_nothing_keeps_transitive_servers_admitted(lifecycle_world) -> None:
    """Control: a minimal policy is reported present but empty and enforces nothing."""
    parent, child, _name = _author(lifecycle_world, "registry")
    _activation_policy(lifecycle_world, "empty", None)
    result = _install(lifecycle_world, "registry", parent, child)
    assert result.returncode == 0, _text(result)
    assert "present but empty" in _text(result), _text(result)
    assert _configured(lifecycle_world, _CONFIGURED_NAME["registry"]), _text(result)


@pytest.mark.parametrize("case", sorted(_ACTIVATION))
def test_an_active_policy_refuses_an_unlisted_transitive_server(lifecycle_world, case) -> None:
    """An explicit false, or the default false under any other non-default field, refuses."""
    parent, child, _name = _author(lifecycle_world, "registry")
    _activation_policy(lifecycle_world, case, _ACTIVATION[case])
    result = _install(lifecycle_world, "registry", parent, child)
    assert result.returncode == 1, _text(result)
    assert "trust_transitive" in _text(result), _text(result)
    assert not _configured(lifecycle_world, _CONFIGURED_NAME["registry"]), _text(result)
