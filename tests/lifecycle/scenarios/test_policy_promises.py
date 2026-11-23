"""Org-policy field promises the generated corpus has no factor for yet (QL gap sweep).

PROMISE: P-INV-I4 -- an org policy restriction never widens what install deploys
(source: docs/src/content/docs/reference/policy-schema.md).
PROMISE: P-INV-I7 -- a refused or restricted install names its cause.

Each row authors its own packages and serves its own ``apm-policy.yml`` from a
unique org (``_serve_policy``), so the shared world variants stay untouched.
A promise row is paired with a control row that proves the fixture really
deploys what the promise restricts, so the promise row can never pass
vacuously. A promise row the CLI does not keep yet is a strict xfail citing its issue.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.world import use_policy, use_registry
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


@pytest.mark.parametrize("path", PATHS)
def test_a_transitive_server_in_both_allow_and_deny_is_refused_at_install(
    lifecycle_world, path
) -> None:
    """mcp.deny wins over mcp.allow for a transitive server: install refuses before any write."""
    parent, child, name = _author(lifecycle_world, path)
    project = lifecycle_world.project
    before = {
        rel: (project / rel).read_bytes() if (project / rel).exists() else None
        for rel in ("apm.yml", "apm.lock.yaml")
    }
    result = _install_under_policy(
        lifecycle_world, path, parent, child, trust_transitive=False, allow=[name], deny=[name]
    )
    text = _text(result)
    assert result.returncode == 1, text
    assert "mcp-denylist" in text or "denied by pattern" in text, text
    assert not _configured(lifecycle_world, _CONFIGURED_NAME[path]), text
    if path != "approved":
        after = {
            rel: (project / rel).read_bytes() if (project / rel).exists() else None
            for rel in before
        }
        assert after == before, "a refused install changed apm.yml or apm.lock.yaml"


@pytest.mark.parametrize("path", PATHS)
def test_audit_reports_a_transitive_server_in_both_allow_and_deny(lifecycle_world, path) -> None:
    """audit --ci fails a kept transitive server that mcp.deny names, even if mcp.allow lists it.

    A one-run flag install already fails audit on the consumer trust check
    (fail-fast), so that path asserts the failure and the server name.
    """
    parent, child, name = _author(lifecycle_world, path)
    installed = _install(lifecycle_world, path, parent, child)
    assert installed.returncode == 0, _text(installed)
    _transitive_policy(lifecycle_world, path, trust_transitive=False, allow=[name], deny=[name])
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    text = _text(audit)
    assert audit.returncode == 1, text
    assert name in text, text
    if path != "flag":
        assert "mcp-denylist" in text, text


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


def test_a_root_redeclared_server_is_judged_as_a_direct_one(lifecycle_world) -> None:
    """A root re-declaration is the project's own server: mcp.deny governs it, not 10b."""
    parent, _child, name = _author(lifecycle_world, "registry")
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {}).setdefault("mcp", []).append(name)
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _transitive_policy(lifecycle_world, "redeclared", deny=[name])
    result = driver.run(lifecycle_world, ["install", parent])
    assert result.returncode == 1, _text(result)
    assert "mcp-denylist" in _text(result), _text(result)
    assert not _configured(lifecycle_world, _CONFIGURED_NAME["registry"]), _text(result)


def test_a_dry_run_previews_the_trust_transitive_refusal(lifecycle_world) -> None:
    """--dry-run names the refused transitive server and writes nothing (#4455 convention)."""
    parent, _child, name = _author(lifecycle_world, "registry")
    _transitive_policy(lifecycle_world, "dry-run", trust_transitive=False)
    result = driver.run(lifecycle_world, ["install", parent, "--dry-run"])
    assert result.returncode == 0, _text(result)
    assert f"Would be blocked by policy: {name}" in _text(result), _text(result)
    assert not _configured(lifecycle_world, _CONFIGURED_NAME["registry"]), _text(result)


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


# -- G2: manifest.content_types (policy-schema.md, manifest section) ---------------


def _skill_and_instruction(world) -> str:
    factory = LocalPackageFactory(world.root / "g2")
    package = factory.create("t-typed", version="1.0.0")
    _skill(factory, package, "t-typed")
    factory.add_instruction(
        package, "t-typed", '---\napplyTo: "**"\n---\n\nKeep the typed instruction.\n'
    )
    return str(package.root)


def _instruction_deployed(world) -> bool:
    return any(
        path.is_file() and "apm_modules" not in path.parts
        for path in world.project.rglob("t-typed.instructions.md")
    )


def test_content_types_control_an_instruction_deploys(lifecycle_world) -> None:
    """Control: with no content-type restriction the package's instruction deploys."""
    use_policy(lifecycle_world, "allow")
    spec = _skill_and_instruction(lifecycle_world)
    result = driver.run(lifecycle_world, ["install", spec])
    assert result.returncode == 0, _text(result)
    assert _instruction_deployed(lifecycle_world), _text(result)


def test_content_types_allow_list_keeps_other_types_out(lifecycle_world) -> None:
    """manifest.content_types {allow: [skill]} -- an instruction is never deployed."""
    _serve_policy(
        lifecycle_world,
        "policy-g2-skills-only",
        {
            "name": "g2",
            "version": "1.0",
            "enforcement": "block",
            "manifest": {"content_types": {"allow": ["skill"]}},
        },
    )
    spec = _skill_and_instruction(lifecycle_world)
    project = lifecycle_world.project
    before = {
        name: (project / name).read_bytes() if (project / name).exists() else None
        for name in ("apm.yml", "apm.lock.yaml")
    }
    result = driver.run(lifecycle_world, ["install", spec])
    assert result.returncode == 1, _text(result)
    assert not _instruction_deployed(lifecycle_world), _text(result)
    # A refusal before any deploy write: no skill either, and apm.yml and the lock unchanged.
    assert not (lifecycle_world.project / ".agents/skills/t-typed").exists(), _text(result)
    after = {
        name: (project / name).read_bytes() if (project / name).exists() else None
        for name in before
    }
    assert after == before, "a refused install changed apm.yml or apm.lock.yaml"
    text = _text(result)
    assert "content_types" in text and "policy-g2-skills-only" in text, text
    assert "instruction" in text, text


_SKILLS_ONLY = {
    "name": "g2",
    "version": "1.0",
    "enforcement": "block",
    "manifest": {"content_types": {"allow": ["skill"]}},
}


def test_content_types_refuse_a_plugin_root_agent(lifecycle_world) -> None:
    """A plugin-format agents/ directory is an agent: allow [skill] refuses it before deploy."""
    _serve_policy(lifecycle_world, "policy-g2-root-agent", _SKILLS_ONLY)
    factory = LocalPackageFactory(lifecycle_world.root / "g2-agent")
    package = factory.create("t-agent", version="1.0.0")
    _skill(factory, package, "t-agent")
    agents = Path(package.root) / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("---\nname: reviewer\n---\n\nReview.\n", encoding="utf-8")
    result = driver.run(lifecycle_world, ["install", str(package.root)])
    assert result.returncode == 1, _text(result)
    deployed = [
        p for p in lifecycle_world.project.rglob("reviewer*") if "apm_modules" not in p.parts
    ]
    assert not deployed, deployed
    assert "content_types" in _text(result) and "agent" in _text(result), _text(result)


def test_audit_ci_fails_a_lock_that_ships_a_disallowed_content_type(lifecycle_world) -> None:
    """A package installed outside the policy still fails audit --ci under it."""
    _serve_policy(lifecycle_world, "policy-g2-audit", _SKILLS_ONLY)
    spec = _skill_and_instruction(lifecycle_world)
    installed = driver.run(lifecycle_world, ["install", "--no-policy", spec])
    assert installed.returncode == 0, _text(installed)
    assert _instruction_deployed(lifecycle_world), _text(installed)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    assert audit.returncode != 0, _text(audit)
    assert "content-types" in _text(audit) and "instruction" in _text(audit), _text(audit)


def _hook_deployed(world) -> bool:
    return any(
        "apm_modules" not in path.parts and "t-first" in path.read_text(encoding="utf-8")
        for path in world.project.rglob("*.json")
        if ".github" in path.parts or ".claude" in path.parts
    )


def test_content_types_judge_a_local_source_not_its_stale_copy(lifecycle_world) -> None:
    """A local dependency whose source gained a disallowed kind after its apm_modules copy.

    Locked, a targeted install of another package leaves it untouched (nothing of it
    deploys). Unlocked (#4198), the targeted install redeploys it from source, so the
    install is refused and nothing deploys.
    """
    _serve_policy(lifecycle_world, "policy-g2-stale-copy", _SKILLS_ONLY)
    factory = LocalPackageFactory(lifecycle_world.root / "g2-stale")
    first = factory.create("t-first", version="1.0.0")
    _skill(factory, first, "t-first")
    assert driver.run(lifecycle_world, ["install", str(first.root)]).returncode == 0
    hooks = Path(first.root) / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "t-first"}]}]}}
    (hooks / "t-first.json").write_text(json.dumps(hook), encoding="utf-8")
    second = factory.create("t-second", version="1.0.0")
    _skill(factory, second, "t-second")

    locked = driver.run(lifecycle_world, ["install", str(second.root)])
    assert locked.returncode == 0, _text(locked)
    assert not _hook_deployed(lifecycle_world), _text(locked)

    lock_path = lifecycle_world.project / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    lock["dependencies"] = [d for d in lock["dependencies"] if d.get("name") != "t-first"]
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    third = factory.create("t-third", version="1.0.0")
    _skill(factory, third, "t-third")
    result = driver.run(lifecycle_world, ["install", str(third.root)])
    assert result.returncode == 1, _text(result)
    assert "content_types" in _text(result) and "hook" in _text(result), _text(result)
    assert not _hook_deployed(lifecycle_world), _text(result)
    assert not (lifecycle_world.project / ".agents/skills/t-third").exists(), _text(result)


# -- G11: manifest.scripts (policy-schema.md, manifest section) --------------------


def _scripts_deny(world) -> None:
    _serve_policy(
        world,
        "policy-g11-scripts-deny",
        {"name": "g11", "version": "1.0", "enforcement": "block", "manifest": {"scripts": "deny"}},
    )


def test_scripts_deny_accepts_a_manifest_without_scripts(lifecycle_world) -> None:
    """Control: under scripts: deny a manifest with no scripts block is compliant."""
    _scripts_deny(lifecycle_world)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    output = " ".join((audit.stdout + audit.stderr).split())
    assert "No scripts section (compliant with deny policy)" in output, output


def test_scripts_deny_fails_audit_for_a_scripts_block(lifecycle_world) -> None:
    """manifest.scripts: deny -- a scripts block in apm.yml fails ``apm audit --ci``, named."""
    _scripts_deny(lifecycle_world)
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["scripts"] = {"hello": "echo hello"}
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    before = manifest.read_bytes()
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    assert audit.returncode == 1, _text(audit)
    assert "scripts-policy" in _text(audit), _text(audit)
    assert "Scripts section present but denied by policy" in _text(audit), _text(audit)
    assert manifest.read_bytes() == before


# -- G6: mcp.self_defined (policy-schema.md, mcp section) ---------------------------

_INLINE = ("install", "--mcp", "p-inline", "--", "apm-lifecycle-inert-server", "--stdio")


def _self_defined(world, level: str) -> None:
    mcp: dict[str, object] = {"self_defined": level}
    if level == "warn":
        # warn is the default: alone it leaves the policy empty and unenforced.
        mcp["transport"] = {"allow": ["stdio"]}
    _serve_policy(
        world,
        f"policy-g6-self-defined-{level}",
        {"name": "g6", "version": "1.0", "enforcement": "block", "mcp": mcp},
    )


def test_self_defined_allow_installs_an_inline_server(lifecycle_world) -> None:
    """Control: self_defined allow writes the inline server with no self-defined finding."""
    _self_defined(lifecycle_world, "allow")
    result = driver.run(lifecycle_world, list(_INLINE))
    assert result.returncode == 0, _text(result)
    assert _configured(lifecycle_world, "p-inline"), _text(result)


_SILENT_WARN = KnownFailure(
    "#5070",
    "install reports no policy finding for a self-defined MCP server under warn",
    r"no policy warning names the self-defined server",
)


@_SILENT_WARN.mark()
def test_self_defined_warn_installs_an_inline_server_and_says_so(lifecycle_world) -> None:
    """mcp.self_defined: warn -- the inline server is written and the warning names it."""
    _self_defined(lifecycle_world, "warn")
    result = driver.run(lifecycle_world, list(_INLINE))
    assert result.returncode == 0, _text(result)
    assert _configured(lifecycle_world, "p-inline"), _text(result)
    # The policy finding itself, not the "(self-defined, stdio)" progress label.
    text = _text(result)
    warned = "Policy warning" in text or "self-defined MCP server(s) (warn)" in text
    assert warned and "p-inline" in text, f"no policy warning names the self-defined server: {text}"


def test_self_defined_deny_refuses_an_inline_server(lifecycle_world) -> None:
    """mcp.self_defined: deny -- the inline server is refused and nothing is written."""
    _self_defined(lifecycle_world, "deny")
    manifest = lifecycle_world.project / "apm.yml"
    before = manifest.read_bytes()
    result = driver.run(lifecycle_world, list(_INLINE))
    assert result.returncode == 1, _text(result)
    assert "self-defined" in _text(result), _text(result)
    assert not _configured(lifecycle_world, "p-inline"), _text(result)
    assert manifest.read_bytes() == before


# -- G3: dependencies.max_depth (policy-schema.md, dependencies section) -----------


def _three_deep(world) -> str:
    """t-a -> t-b -> t-c (local packages); returns t-a."""
    factory = LocalPackageFactory(world.root / "g3")
    for name, child in (("t-c", None), ("t-b", "t-c"), ("t-a", "t-b")):
        package = factory.create(
            name, version="1.0.0", dependencies=[f"../{child}"] if child else []
        )
        _skill(factory, package, name)
    return str(world.root / "g3" / "t-a")


def _max_depth(world, depth: int) -> None:
    _serve_policy(
        world,
        f"policy-g3-max-depth-{depth}",
        {
            "name": "g3",
            "version": "1.0",
            "enforcement": "block",
            "dependencies": {"max_depth": depth},
        },
    )


@pytest.mark.parametrize(("depth", "passes"), [(5, True), (1, False)], ids=["within", "exceeded"])
def test_max_depth_bounds_the_locked_graph_in_audit(lifecycle_world, depth, passes) -> None:
    """dependencies.max_depth -- audit --ci fails when the locked graph is deeper, naming the limit."""
    installed = driver.run(lifecycle_world, ["install", _three_deep(lifecycle_world)])
    assert installed.returncode == 0, _text(installed)
    _max_depth(lifecycle_world, depth)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    if passes:
        assert f"All dependencies within depth limit ({depth})" in _text(audit), _text(audit)
    else:
        assert audit.returncode == 1, _text(audit)
        assert f"exceed max depth {depth}" in _text(audit), _text(audit)


# -- G10: dependencies.require_resolution (policy-schema.md, dependencies section) --


@pytest.mark.parametrize(
    ("resolution", "fails"),
    [("project-wins", False), ("policy-wins", True), ("block", True)],
)
def test_require_resolution_decides_a_pinned_required_package_mismatch(
    lifecycle_world, resolution, fails
) -> None:
    """dependencies.require_resolution -- a required pin the lock does not match fails audit
    under policy-wins and block; project-wins keeps the project's ref."""
    spec = lifecycle_world.packages["p-skill:git"].spec
    installed = driver.run(lifecycle_world, ["install", spec])
    assert installed.returncode == 0, _text(installed)
    _serve_policy(
        lifecycle_world,
        f"policy-g10-{resolution}",
        {
            "name": "g10",
            "version": "1.0",
            "enforcement": "block",
            "dependencies": {
                "require": ["lifecycle/p-skill#v9.9.9"],
                "require_resolution": resolution,
            },
        },
    )
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    if fails:
        assert audit.returncode == 1, _text(audit)
        assert "version mismatch" in _text(audit), _text(audit)
    else:
        # The audit ran require_resolution and accepted the project's ref.
        assert audit.returncode == 0, _text(audit)
        assert "version mismatch" not in _text(audit), _text(audit)


# -- G5: compilation.source_attribution (policy-schema.md, compilation section) ----


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "missing"])
def test_source_attribution_policy_requires_the_manifest_setting(lifecycle_world, enabled) -> None:
    """compilation.source_attribution: true -- audit --ci fails unless apm.yml enables it."""
    _serve_policy(
        lifecycle_world,
        f"policy-g5-attribution-{'on' if enabled else 'off'}",
        {
            "name": "g5",
            "version": "1.0",
            "enforcement": "block",
            "compilation": {"source_attribution": True},
        },
    )
    if enabled:
        manifest = lifecycle_world.project / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["compilation"] = {"source_attribution": True}
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    if enabled:
        assert "Source attribution enabled" in _text(audit), _text(audit)
    else:
        assert audit.returncode == 1, _text(audit)
        assert "Source attribution required by policy but not enabled" in _text(audit), _text(audit)


# -- G7: unmanaged_files (policy-schema.md, unmanaged_files section) ----------------

_UNMANAGED = ".github/instructions/hand-written.instructions.md"


def _unmanaged_policy(world, case: str, **fields) -> None:
    _serve_policy(
        world,
        f"policy-g7-unmanaged-{case}",
        {"name": "g7", "version": "1.0", "enforcement": "block", "unmanaged_files": fields},
    )


def _hand_written_file(world) -> None:
    path = world.project / _UNMANAGED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\napplyTo: "**"\n---\n\nWritten by hand.\n', encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "fields", "fails", "reported"),
    [
        ("deny", {"action": "deny"}, True, True),
        ("warn", {"action": "warn"}, False, False),
        (
            "deny-excluded",
            {"action": "deny", "exclude": [".github/instructions/hand-*"]},
            False,
            False,
        ),
    ],
    ids=["deny", "warn", "deny-excluded"],
)
def test_unmanaged_files_policy_in_audit(lifecycle_world, case, fields, fails, reported) -> None:
    """unmanaged_files -- deny fails audit --ci on a file the lock does not record, warn
    reports it, and exclude suppresses it."""
    _unmanaged_policy(lifecycle_world, case, **fields)
    _hand_written_file(lifecycle_world)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    text = _text(audit)
    assert audit.returncode == (1 if fails else 0), text
    # deny names each file; warn reports only the count; exclude suppresses it.
    assert ("hand-written.instructions.md" in text) is reported, text
    if case == "warn":
        assert "1 unmanaged file(s) found (warn)" in text, text
    assert (lifecycle_world.project / _UNMANAGED).is_file()


def test_unmanaged_files_deny_blocks_an_install_that_would_leave_drift(lifecycle_world) -> None:
    """unmanaged_files.action: deny -- "deny blocks installs that would leave drift" (#4826)."""
    project = lifecycle_world.project
    _unmanaged_policy(lifecycle_world, "install-deny", action="deny")
    _hand_written_file(lifecycle_world)
    kept = {name: (project / name).read_bytes() for name in ("apm.yml", _UNMANAGED)}
    lock = project / "apm.lock.yaml"
    lock_before = lock.read_bytes() if lock.exists() else None
    result = driver.run(lifecycle_world, ["install", lifecycle_world.packages["p-skill:git"].spec])
    assert result.returncode == 1, _text(result)
    assert "unmanaged_files.action: deny" in _text(result), _text(result)
    assert "hand-written.instructions.md" in _text(result), _text(result)
    # A refused install rolls back (P-INV-I7): inputs, lock and the user's file unchanged.
    assert {name: (project / name).read_bytes() for name in kept} == kept, _text(result)
    assert (lock.read_bytes() if lock.exists() else None) == lock_before, _text(result)
    assert not (project / ".agents" / "skills" / "p-skill").exists(), _text(result)


# -- G8: security.audit.fail_on_drift (policy-schema.md, security section) ----------


@pytest.mark.parametrize("fail_on_drift", [True, False], ids=["on", "off"])
def test_fail_on_drift_decides_the_bare_audit_exit_code(lifecycle_world, fail_on_drift) -> None:
    """security.audit.fail_on_drift -- a bare ``apm audit`` exits non-zero on drift only when on."""
    installed = driver.run(
        lifecycle_world, ["install", lifecycle_world.packages["p-skill:git"].spec]
    )
    assert installed.returncode == 0, _text(installed)
    skill = lifecycle_world.project / ".agents/skills/p-skill/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nEdited by hand.\n", encoding="utf-8")
    _serve_policy(
        lifecycle_world,
        f"policy-g8-drift-{'on' if fail_on_drift else 'off'}",
        {
            "name": "g8",
            "version": "1.0",
            "enforcement": "block",
            # mcp.self_defined keeps the off policy non-empty, so it is enforced too.
            "mcp": {"self_defined": "allow"},
            "security": {"audit": {"fail_on_drift": fail_on_drift}},
        },
    )
    audit = driver.run(lifecycle_world, ["audit"])
    assert (audit.returncode != 0) is fail_on_drift, _text(audit)
    assert "SKILL.md" in _text(audit), _text(audit)
    assert "Edited by hand." in skill.read_text(encoding="utf-8")


# -- G9: dependencies.require_pinned_constraint, manifest.require_explicit_includes -


@pytest.mark.parametrize(
    ("ref", "refused"),
    [("", True), ("#main", True), ("#v1.0.0", False)],
    ids=["no-ref", "bare-branch", "literal-tag"],
)
def test_require_pinned_constraint_refuses_an_unbounded_direct_dependency(
    lifecycle_world, ref, refused
) -> None:
    """require_pinned_constraint -- install refuses a direct dep without a bounded ref."""
    _serve_policy(
        lifecycle_world,
        f"policy-g9-pinned-{ref.strip('#') or 'none'}",
        {
            "name": "g9",
            "version": "1.0",
            "enforcement": "block",
            "dependencies": {"require_pinned_constraint": True},
        },
    )
    manifest = lifecycle_world.project / "apm.yml"
    before = manifest.read_bytes()
    result = driver.run(lifecycle_world, ["install", f"lifecycle/p-skill{ref}"])
    if refused:
        assert result.returncode == 1, _text(result)
        assert "dependency-pinned-constraint" in _text(result), _text(result)
        assert "Nothing was changed" in _text(result), _text(result)
        assert manifest.read_bytes() == before
        assert not (lifecycle_world.project / ".agents/skills/p-skill").exists()
    else:
        assert result.returncode == 0, _text(result)


@pytest.mark.parametrize(
    ("includes", "fails"),
    [(None, True), ("auto", True), ([".apm/"], False)],
    ids=["absent", "auto", "explicit-list"],
)
def test_require_explicit_includes_in_audit(lifecycle_world, includes, fails) -> None:
    """manifest.require_explicit_includes -- audit --ci fails unless apm.yml lists includes."""
    _serve_policy(
        lifecycle_world,
        f"policy-g9-includes-{'none' if includes is None else 'auto' if includes == 'auto' else 'list'}",
        {
            "name": "g9",
            "version": "1.0",
            "enforcement": "block",
            "manifest": {"require_explicit_includes": True},
        },
    )
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.pop("includes", None)
    if includes is not None:
        data["includes"] = includes
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    text = _text(audit)
    if fails:
        assert audit.returncode == 1, text
        assert "explicit-includes" in text, text
    else:
        assert "Manifest declares explicit includes paths" in text, text


# -- G4: registry_source (policy-schema.md, registry_source section) ---------------


def _registry_only(world) -> None:
    use_registry(world)
    _serve_policy(
        world,
        "policy-g4-registry-only",
        {
            "name": "g4",
            "version": "1.0",
            "enforcement": "block",
            "registry_source": {"allow_non_registry": False},
        },
    )


def test_registry_only_policy_admits_a_registry_package(lifecycle_world) -> None:
    """Control: registry_source allow_non_registry false installs a registry package."""
    _registry_only(lifecycle_world)
    result = driver.run(
        lifecycle_world, ["install", lifecycle_world.packages["p-skill:registry"].spec]
    )
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    assert (lifecycle_world.project / ".agents/skills/p-skill/SKILL.md").is_file(), output


def test_registry_only_policy_refuses_a_non_registry_package(lifecycle_world) -> None:
    """registry_source.allow_non_registry: false -- a local-path package is refused."""
    _registry_only(lifecycle_world)
    factory = LocalPackageFactory(lifecycle_world.root / "g4")
    package = factory.create("t-local", version="1.0.0")
    _skill(factory, package, "t-local")
    manifest = lifecycle_world.project / "apm.yml"
    before = manifest.read_bytes()
    result = driver.run(lifecycle_world, ["install", str(package.root)])
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "non-registry source not permitted" in output, output
    assert manifest.read_bytes() == before
    assert not (lifecycle_world.project / ".agents/skills/t-local").exists(), output
