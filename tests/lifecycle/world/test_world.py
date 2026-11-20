"""Proof that the lifecycle world works on the real CLI (#4094).

Every package installs through ``driver.run`` at project and user scope, every
source variant and service variant is reachable, the egress log records each
request, and a clone costs milliseconds. Cells that fail on main are STRICT
expected failures naming their tracking issue.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.world import (
    World,
    apm_home_outside_home,
    clone,
    registry,
    use_policy,
    use_registry,
)
from tests.lifecycle.world.packages import GIT_PUBLISHED, LSP_SERVER, MCP_SERVER, PACKAGE_NAMES
from tests.lifecycle.world.services import (
    API_HOST,
    GITHUB_HOST,
    HOST_ALIASES,
    ORG,
    POLICY_VARIANTS,
    REGISTRY_HOST,
    REGISTRY_SERVER,
    policy_org,
)
from tests.lifecycle.world.shims import AZ_CALL_LOG, TRAP_EXIT, TRAPPED

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-install-command-scope-selection",
        "P-OWN-apm-home-resolution",
        "P-OWN-executable-trust-context",
        "P-OWN-copilot-cli-mcp-config-paths",
        "P-OWN-lsp-target-contract",
        "P-OWN-post-uninstall-dependency-reachability",
        "P-OWN-claude-native-plugin-registration",
        "P-OWN-codex-native-plugin-lifecycle",
        "P-OWN-git-transport-selection",
        "P-OWN-host-reference-coordinates",
        "P-OWN-marketplace-catalog-origin",
        "P-OWN-local-bundle-hook-trust",
        "P-OWN-unpack-executable-refusal",
        "P-OWN-policy-remote-origin-reader",
        "P-OWN-policy-content-hash",
        "P-OWN-mcp-registry-url-resolution",
        "P-OWN-network-host-loopback-classification",
    ),
]

# A clone must stay well under a second; the median pins the steady cost.
CLONE_BUDGET_SECONDS = 0.25
_GATED = frozenset({"hook", "mcp", "lsp", "bin", "canvas", "native"})
# The Claude bin plugin has its own user-scope test (--trust-bin, claude target).
_APM_GIT_PACKAGES = tuple(name for name in GIT_PUBLISHED if name != "p-native-claude-bin")
_TRACK_USER_TRANSITIVE = "#4153"
_TRACK_NATIVE_APPROVE = "#4154"

# Files each package deploys after an approval, relative to the deploy root
# (the project, or HOME at user scope), with a marker the file must contain.
_SURFACES: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("p-skill", "project"): ((".agents/skills/p-skill/SKILL.md", "p-skill"),),
    ("p-skill", "user"): ((".agents/skills/p-skill/SKILL.md", "p-skill"),),
    ("p-exec", "project"): (
        (".github/hooks/p-exec-session.json", "echo p-exec"),
        (".mcp.json", MCP_SERVER),
    ),
    ("p-exec", "user"): (
        (".copilot/hooks/p-exec-session.json", "echo p-exec"),
        (".copilot/mcp-config.json", MCP_SERVER),
    ),
    ("p-lsp", "project"): ((".github/lsp.json", LSP_SERVER),),
    ("p-lsp", "user"): ((".copilot/lsp-config.json", LSP_SERVER),),
    ("p-child", "project"): ((".agents/skills/p-child/SKILL.md", "p-child"),),
    ("p-child", "user"): ((".agents/skills/p-child/SKILL.md", "p-child"),),
    ("p-parent", "project"): (
        (".agents/skills/p-parent/SKILL.md", "p-parent"),
        (".agents/skills/p-child/SKILL.md", "p-child"),
    ),
    ("p-parent", "user"): (
        (".agents/skills/p-parent/SKILL.md", "p-parent"),
        (".agents/skills/p-child/SKILL.md", "p-child"),
    ),
    ("p-parent2", "project"): (
        (".agents/skills/p-parent2/SKILL.md", "p-parent2"),
        (".agents/skills/p-child/SKILL.md", "p-child"),
    ),
    ("p-parent2", "user"): (
        (".agents/skills/p-parent2/SKILL.md", "p-parent2"),
        (".agents/skills/p-child/SKILL.md", "p-child"),
    ),
    ("p-native-claude", "project"): ((".claude/skills/p-native-claude/SKILL.md", "p-native"),),
    ("p-native-claude", "user"): ((".claude/settings.json", "p-native-claude"),),
    ("p-native-codex", "user"): ((".codex/config.toml", "p-native-codex"),),
    ("p-native-claude-bin", "project"): (
        (".claude/skills/p-native-claude-bin/SKILL.md", "p-native-claude-bin"),
    ),
    ("p-canvas", "project"): ((".github/extensions/p-canvas/extension.mjs", "p-canvas"),),
    # Copilot has no user-scope canvas surface (integration/targets.py "copilot").
    ("p-canvas", "user"): (),
}
_TARGETS = {
    "p-native-claude": "claude",
    "p-native-codex": "codex",
    "p-native-claude-bin": "claude",
}
_KNOWN = {
    ("p-parent", "user"): _TRACK_USER_TRANSITIVE,
    ("p-parent2", "user"): _TRACK_USER_TRANSITIVE,
    ("p-native-claude", "user"): _TRACK_NATIVE_APPROVE,
    ("p-native-codex", "user"): _TRACK_NATIVE_APPROVE,
}


def _deploy_root(world: World, scope: str) -> Path:
    return world.project if scope == "project" else world.home


def _manifest_root(world: World, scope: str) -> Path:
    return world.project if scope == "project" else world.apm_home


def _set_targets(world: World, scope: str, target: str) -> None:
    manifest = _manifest_root(world, scope) / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = [target]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _ok(world: World, argv, scope: str = "project"):
    result = driver.run(world, argv, scope=scope)
    assert result.returncode == 0, f"{argv} -> {result.returncode}\n{result.stdout}{result.stderr}"
    return result


def _locked(world: World, scope: str) -> set[str]:
    lock = LockFile.read(_manifest_root(world, scope) / "apm.lock.yaml")
    assert lock is not None, "no lockfile written"
    return {dependency.name for dependency in lock.get_package_dependencies()}


def _assert_surfaces(world: World, package: str, scope: str) -> None:
    root = _deploy_root(world, scope)
    for relative, marker in _SURFACES[(package, scope)]:
        path = root / relative
        assert path.is_file(), f"{package} at {scope} scope did not deploy {relative}"
        assert marker in path.read_text(encoding="utf-8"), f"{relative} lacks {marker!r}"


def _identity(key: str, world: World) -> str:
    """What ``apm approve`` names: the install spec, or ``owner/repo`` for remote sources."""
    ref = world.packages[key]
    return f"{ORG}/{ref.name}" if ref.source in ("git", "marketplace") else ref.spec


def _install_and_approve(world: World, key: str, scope: str) -> None:
    ref = world.packages[key]
    _ok(world, ["install", ref.spec], scope)
    if ref.kinds & _GATED:
        _ok(world, ["approve", _identity(key, world)], scope)
        _ok(world, ["install"], scope)


def _hosts(world: World) -> set[str]:
    return {entry["host"] for entry in world.services.egress_log()}


# -- clone -------------------------------------------------------------------


def test_clone_is_fast_private_and_moves_every_path(
    lifecycle_template: World, tmp_path: Path
) -> None:
    durations = []
    for index in range(5):
        started = time.perf_counter()
        world = clone(lifecycle_template, tmp_path / f"clone-{index}")
        durations.append(time.perf_counter() - started)
    assert statistics.median(durations) < CLONE_BUDGET_SECONDS, durations
    template_root = str(lifecycle_template.root)
    for path in (world.project, world.home, world.apm_home, world.user_cwd, world.cache_root):
        assert path is not None and path.is_dir() and path.is_relative_to(world.root), path
    leaked = {key: value for key, value in world.env.items() if template_root + "/" in value}
    assert not leaked, f"clone env still points into the template: {leaked}"
    assert world.env["HOME"] == str(world.home)
    assert world.env["APM_HOME"] == str(world.apm_home)
    assert Path(world.env["REQUESTS_CA_BUNDLE"]).is_file()
    for ref in world.packages.values():
        assert template_root + "/" not in ref.spec, ref
    (world.project / "apm.yml").write_text("edited\n", encoding="utf-8")
    assert (lifecycle_template.project / "apm.yml").read_text(encoding="utf-8") != "edited\n"


def test_each_world_starts_with_an_empty_egress_log(lifecycle_world: World) -> None:
    assert lifecycle_world.services.egress_log() == []
    assert set(lifecycle_world.packages) >= set(PACKAGE_NAMES)


# -- packages at project and user scope --------------------------------------


_CELLS = [
    pytest.param(
        package,
        scope,
        marks=(
            [
                pytest.mark.xfail(
                    strict=True,
                    reason=f"tracking {_KNOWN[(package, scope)]}",
                    raises=AssertionError,
                )
            ]
            if (package, scope) in _KNOWN
            else []
        ),
        id=f"{package}-{scope}",
    )
    for package in PACKAGE_NAMES
    for scope in ("project", "user")
    if (package, scope) in _SURFACES
]


@pytest.mark.parametrize(("package", "scope"), _CELLS)
def test_each_package_installs_through_the_real_cli(
    lifecycle_world: World, package: str, scope: str
) -> None:
    world = lifecycle_world
    if package in _TARGETS:
        _set_targets(world, scope, _TARGETS[package])
    _install_and_approve(world, package, scope)
    assert package in _locked(world, scope)
    _assert_surfaces(world, package, scope)
    assert world.services.egress_log() == [], "a local-path install reached the network"


@pytest.mark.parametrize("source", ["marketplace", "git"])
def test_claude_plugin_bin_deploys_at_user_scope_after_trust(
    lifecycle_world: World, source: str
) -> None:
    world = lifecycle_world
    _set_targets(world, "user", "claude")
    if source == "marketplace":
        _ok(world, ["marketplace", "add", world.services.marketplace_url])
    _ok(world, ["install", world.packages[f"p-native-claude-bin:{source}"].spec], "user")
    _ok(world, ["approve", f"{ORG}/p-native-claude-bin"], "user")
    _ok(world, ["install", "--trust-bin"], "user")
    plugin = world.home / ".agents" / "plugins" / "p-native-claude-bin"
    binary = plugin / "bin" / "p-native-claude-bin"
    assert binary.is_file() and binary.stat().st_mode & 0o100, binary
    assert (plugin / ".claude-plugin" / "plugin.json").is_file()
    assert "p-native-claude-bin" in _locked(world, "user")


def test_codex_plugin_is_refused_at_project_scope(lifecycle_world: World) -> None:
    world = lifecycle_world
    _set_targets(world, "project", "codex")
    before = (world.project / "apm.yml").read_bytes()
    result = driver.run(world, ["install", world.packages["p-native-codex"].spec])
    assert result.returncode == 1
    assert "only at user scope" in " ".join((result.stdout + result.stderr).split())
    assert (world.project / "apm.yml").read_bytes() == before


def test_user_scope_with_apm_home_outside_home(lifecycle_world: World) -> None:
    world = apm_home_outside_home(lifecycle_world)
    assert not world.apm_home.is_relative_to(world.home)
    _install_and_approve(world, "p-exec", "user")
    assert "p-exec" in _locked(world, "user")
    _assert_surfaces(world, "p-exec", "user")
    assert not (world.home / ".apm").exists(), "APM_HOME outside HOME still wrote HOME/.apm"


# -- sources -----------------------------------------------------------------


@pytest.mark.parametrize("scope", ["project", "user"])
@pytest.mark.parametrize("package", _APM_GIT_PACKAGES)
def test_git_source_installs_through_the_local_https_server(
    lifecycle_world: World, package: str, scope: str
) -> None:
    world = lifecycle_world
    _install_and_approve(world, f"{package}:git", scope)
    assert package in _locked(world, scope)
    _assert_surfaces(world, package, scope)
    requests = world.services.egress_log()
    assert {(GITHUB_HOST, f"/lifecycle/{package}/info/refs")} <= {
        (entry["host"], entry["path"]) for entry in requests
    }, requests
    assert all(entry["status"] == 200 for entry in requests), requests


@pytest.mark.parametrize("scope", ["project", "user"])
@pytest.mark.parametrize("package", _APM_GIT_PACKAGES)
def test_marketplace_source_installs(lifecycle_world: World, package: str, scope: str) -> None:
    world = lifecycle_world
    _ok(world, ["marketplace", "add", world.services.marketplace_url])
    assert any(
        entry["host"] == API_HOST and entry["path"].startswith("/repos/lifecycle/marketplace/")
        for entry in world.services.egress_log()
    )
    _install_and_approve(world, f"{package}:marketplace", scope)
    assert package in _locked(world, scope)
    _assert_surfaces(world, package, scope)


def test_local_bundle_installs_offline(lifecycle_world: World) -> None:
    world = lifecycle_world
    bundle = world.packages["p-exec:bundle"]
    assert Path(bundle.spec).is_file() and bundle.source == "local-bundle"
    result = _ok(world, ["install", bundle.spec])
    output = " ".join((result.stdout + result.stderr).split())
    assert "Installing local bundle" in output
    # Bundle executables stay parked until this exact bundle is approved.
    assert "Skipped 1 bundle hook file(s) from p-exec" in output
    assert not (world.project / ".github" / "hooks" / "p-exec-session.json").exists()
    assert world.services.egress_log() == []


def test_apm_format_bundle_carries_the_hook_and_unpack_refuses_it(
    lifecycle_world: World, tmp_path: Path
) -> None:
    world = lifecycle_world
    bundle = world.packages["p-exec:apm-bundle"]
    assert Path(bundle.spec).is_file() and bundle.spec != world.packages["p-exec:bundle"].spec
    dest = tmp_path / "unpacked"
    result = driver.run(world, ["unpack", bundle.spec, "-o", str(dest)])
    output = " ".join((result.stdout + result.stderr).split())
    # unpack cannot apply the executables gate, so it refuses the bundled hook.
    assert result.returncode == 1, output
    assert "Refusing to unpack 1 executable file(s)" in output, output
    assert ".github/hooks/p-exec-session.json (hook resource)" in output, output
    assert not dest.exists(), "a refused unpack wrote files"
    assert world.services.egress_log() == []


# -- services ----------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(HOST_ALIASES))
def test_host_alias_serves_the_world_repository(lifecycle_world: World, shape: str) -> None:
    world = lifecycle_world
    _ok(world, ["install", f"{world.services.host_alias(shape)}/p-skill"])
    _assert_surfaces(world, "p-skill", "project")
    host = HOST_ALIASES[shape][0]
    assert _hosts(world) == {host}, world.services.egress_log()
    assert any(entry["path"].endswith("/git-upload-pack") for entry in world.services.egress_log())


# -- OS-aware shims ------------------------------------------------------------


def _shim(world: World, name: str) -> str:
    found = shutil.which(name, path=world.env["PATH"])
    assert found is not None, f"{name} is not on the world PATH"
    assert Path(found).is_relative_to(world.root), f"{name} resolves outside the world: {found}"
    return found


def _run_shim(world: World, name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        (_shim(world, name), *args),
        env=world.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("name", TRAPPED)
def test_trap_binary_records_and_fails_on_this_os(lifecycle_world: World, name: str) -> None:
    world = lifecycle_world
    result = _run_shim(world, name, "--version")
    assert result.returncode == TRAP_EXIT, result
    assert (world.root / f"{name}-was-run").read_text(encoding="utf-8") == "--version\n"


def test_azure_cli_is_the_hermetic_world_stub(lifecycle_world: World) -> None:
    """The runner's real az must never run: it phones home through the proxy."""
    world = lifecycle_world
    config = Path(world.env["AZURE_CONFIG_DIR"])
    assert config.is_relative_to(world.root)
    assert world.env["AZURE_CORE_COLLECT_TELEMETRY"] == "no"
    result = _run_shim(world, "az", "account", "get-access-token", "-o", "json")
    assert result.returncode == 1 and "az login" in result.stderr, result
    calls = (config / AZ_CALL_LOG).read_text(encoding="utf-8").splitlines()
    assert json.loads(calls[-1])[:2] == ["account", "get-access-token"]
    assert world.services.egress_log() == []


# -- package registry ----------------------------------------------------------


def _locked_entry(world: World, scope: str, name: str) -> dict:
    lock = yaml.safe_load((_manifest_root(world, scope) / "apm.lock.yaml").read_text("utf-8"))
    entries = [d for d in lock["dependencies"] if str(d.get("repo_url", "")).endswith(name)]
    assert len(entries) == 1, lock["dependencies"]
    return entries[0]


@pytest.mark.parametrize("scope", ["project", "user"])
def test_registry_source_installs_and_locks_the_archive_hash(
    lifecycle_world: World, scope: str
) -> None:
    world = lifecycle_world
    use_registry(world, scope)
    _ok(world, ["install", world.packages["p-skill:registry"].spec], scope)
    _assert_surfaces(world, "p-skill", scope)
    archive = world.services.package_registry.archives[f"{ORG}/p-skill"]["1.0.0"]
    assert _locked_entry(world, scope, "p-skill")["resolved_hash"] == registry.digest(archive)
    assert _hosts(world) == {registry.HOST}, world.services.egress_log()
    assert world.services.proxy.hosts_receiving(registry.TOKEN) == {registry.HOST}


def test_registry_serves_a_newer_version(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_registry(world)
    _ok(world, ["install", f"{ORG}/p-skill#1.1.0"])
    skill = world.project / ".agents/skills/p-skill/SKILL.md"
    assert "Registry release 1.1.0." in skill.read_text(encoding="utf-8")
    archive = world.services.package_registry.archives[f"{ORG}/p-skill"]["1.1.0"]
    assert _locked_entry(world, "project", "p-skill")["resolved_hash"] == registry.digest(archive)


def test_registry_refuses_a_request_without_its_token(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_registry(world)
    env = {k: v for k, v in world.env.items() if k != registry.TOKEN_ENV}
    result = driver.run(
        replace(world, env=env), ["install", world.packages["p-skill:registry"].spec]
    )
    assert result.returncode != 0, result.stdout
    statuses = {e["status"] for e in world.services.egress_log() if e["host"] == registry.HOST}
    assert statuses == {401}, world.services.egress_log()
    assert not (world.project / ".agents/skills/p-skill").exists()


_POLICY_EXIT = {
    "allow": 0,
    "deny-pkg": 1,
    "exec-deny-all": 0,
    "mcp-deny": 0,
    "unreachable": 0,
    "hash-mismatch": 1,
    "chain-cycle": 1,
    "deny-lsp": 0,
    "deny-native": 0,
    "exec-gate-on": 0,
}
_POLICY_ANNOUNCE = {
    "deny-pkg": "denied by pattern",
    "unreachable": "HTTP 503",
    "hash-mismatch": "Policy hash mismatch",
    "chain-cycle": "Cycle detected in policy extends chain",
}


@pytest.mark.parametrize("variant", POLICY_VARIANTS)
def test_policy_variant_is_served_and_recorded(lifecycle_world: World, variant: str) -> None:
    world = lifecycle_world
    use_policy(world, variant)
    assert world.services.policy_url(variant).endswith(f"/{policy_org(variant)}/.github")
    result = driver.run(world, ["install", world.packages["p-exec"].spec])
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == _POLICY_EXIT[variant], output
    if variant in _POLICY_ANNOUNCE:
        assert _POLICY_ANNOUNCE[variant] in output, output
    fetched = {
        entry["path"].split("/")[2]
        for entry in world.services.egress_log()
        if entry["host"] == API_HOST and entry["path"].endswith("/contents/apm-policy.yml")
    }
    assert policy_org(variant) in fetched, world.services.egress_log()


@pytest.mark.parametrize(
    ("variant", "package"),
    [
        ("deny-pkg", "p-exec"),
        ("deny-lsp", "p-lsp"),
        ("deny-native", "p-native-claude"),
        ("deny-native", "p-native-codex"),
    ],
)
def test_dependency_deny_variant_blocks_its_package(
    lifecycle_world: World, variant: str, package: str
) -> None:
    world = lifecycle_world
    use_policy(world, variant)
    before = (world.project / "apm.yml").read_bytes()
    result = driver.run(world, ["install", world.packages[package].spec])
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert f"denied by pattern: **/{package}" in output, output
    assert (world.project / "apm.yml").read_bytes() == before


def test_exec_gate_on_variant_gates_a_project_without_an_executables_block(
    lifecycle_world: World,
) -> None:
    world = lifecycle_world
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    del data["executables"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    use_policy(world, "exec-gate-on")
    result = _ok(world, ["install", world.packages["p-exec"].spec])
    assert "hooks skipped (not approved" in " ".join((result.stdout + result.stderr).split())
    assert not (world.project / ".github" / "hooks" / "p-exec-session.json").exists()


@pytest.mark.parametrize(
    ("variant", "absent"),
    [
        ("exec-deny-all", ".github/hooks/p-exec-session.json"),
        ("mcp-deny", ".mcp.json"),
    ],
)
def test_org_policy_denies_approved_executables(
    lifecycle_world: World, variant: str, absent: str
) -> None:
    world = lifecycle_world
    use_policy(world, variant)
    driver.run(world, ["install", world.packages["p-exec"].spec])
    driver.run(world, ["approve", world.packages["p-exec"].spec])
    driver.run(world, ["install"])
    path = world.project / absent
    assert not path.exists() or MCP_SERVER not in path.read_text(encoding="utf-8"), absent


def test_registry_resolves_an_mcp_server(lifecycle_world: World) -> None:
    world = lifecycle_world
    _ok(world, ["install", "--mcp", REGISTRY_SERVER])
    paths = {e["path"] for e in world.services.egress_log() if e["host"] == REGISTRY_HOST}
    assert "/v0.1/servers" in paths, paths
    manifest = yaml.safe_load((world.project / "apm.yml").read_text(encoding="utf-8"))
    assert REGISTRY_SERVER in json.dumps(manifest["dependencies"]["mcp"])


def test_proxy_refuses_and_records_unknown_hosts(lifecycle_world: World) -> None:
    world = lifecycle_world
    result = driver.run(world, ["install", "https://unknown.lifecycle.invalid/acme/pkg"])
    assert result.returncode == 1
    refused = [entry for entry in world.services.egress_log() if entry["method"] == "CONNECT"]
    assert refused and all(entry["status"] == 502 for entry in refused), refused
