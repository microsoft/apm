"""Proof that the lifecycle world works on the real CLI (#4094).

Every package installs through ``driver.run`` at project and user scope, every
source variant and service variant is reachable, the egress log records each
request, and a clone costs milliseconds. Cells that fail on main are STRICT
expected failures naming their tracking issue.
"""

from __future__ import annotations

import base64
import json
import shutil
import statistics
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest
import requests
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.world import (
    World,
    apm_home_outside_home,
    clone,
    login_azure,
    registry,
    use_ado_pat,
    use_policy,
    use_registry,
    use_registry_proxy,
)
from tests.lifecycle.world.packages import GIT_PUBLISHED, LSP_SERVER, MCP_SERVER, PACKAGE_NAMES
from tests.lifecycle.world.routes import MIRROR_HOSTS, PROXY_HOST, PROXY_TOKEN
from tests.lifecycle.world.services import (
    ADO_BEARER,
    ADO_HOST,
    ADO_PAT,
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
# Native plugins have their own source tests (Claude targets, user-scope bin/).
_APM_GIT_PACKAGES = tuple(name for name in GIT_PUBLISHED if not name.startswith("p-native-"))
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


# p-lsp declares only LSP servers. Locally its empty .apm/ satisfies package
# validation; git cannot carry an empty directory, so the served copy is
# refused ("missing the required .apm/ directory"). Found by the world.
_LSP_ONLY_REMOTE = "tracking #4257: LSP-only package rejected from git/marketplace (no .apm/)"
_REMOTE_CELLS = [
    pytest.param(
        package,
        scope,
        marks=(
            [pytest.mark.xfail(strict=True, reason=_LSP_ONLY_REMOTE, raises=AssertionError)]
            if package == "p-lsp"
            else []
        ),
        id=f"{package}-{scope}",
    )
    for package in _APM_GIT_PACKAGES
    for scope in ("project", "user")
]


@pytest.mark.parametrize(("package", "scope"), _REMOTE_CELLS)
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


@pytest.mark.parametrize(("package", "scope"), _REMOTE_CELLS)
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


def test_lsp_plugin_bundle_installs_offline(lifecycle_world: World) -> None:
    world = lifecycle_world
    bundle = world.packages["p-lsp:bundle"]
    assert Path(bundle.spec).is_file() and bundle.source == "local-bundle"
    result = _ok(world, ["install", bundle.spec])
    assert "Installing local bundle" in " ".join((result.stdout + result.stderr).split())
    assert world.services.egress_log() == []


@pytest.mark.parametrize("source", ["marketplace", "git"])
def test_native_claude_plugin_installs_from_the_world_sources(
    lifecycle_world: World, source: str
) -> None:
    world = lifecycle_world
    if source == "marketplace":
        _ok(world, ["marketplace", "add", world.services.marketplace_url])
    _set_targets(world, "project", "claude")
    _install_and_approve(world, f"p-native-claude:{source}", "project")
    assert "p-native-claude" in _locked(world, "project")
    _assert_surfaces(world, "p-native-claude", "project")


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


# -- Azure DevOps auth and Items API --------------------------------------------


def _ado_statuses(world: World) -> set[int]:
    return {e["status"] for e in world.services.egress_log() if e["host"] == ADO_HOST}


def test_ado_auth_required_refuses_an_anonymous_install(lifecycle_world: World) -> None:
    world = lifecycle_world
    world.services.require_ado_auth()
    result = driver.run(world, ["install", f"{world.services.host_alias('ado')}/p-skill"])
    assert result.returncode != 0, result.stdout
    assert 401 in _ado_statuses(world), world.services.egress_log()
    assert not (world.project / ".agents/skills/p-skill").exists()


@pytest.mark.parametrize("credential", ["pat", "az"])
def test_ado_auth_required_accepts_the_world_credential(
    lifecycle_world: World, credential: str
) -> None:
    world = lifecycle_world
    world.services.require_ado_auth()
    if credential == "pat":
        use_ado_pat(world)
    else:
        login_azure(world)
    _ok(world, ["install", f"{world.services.host_alias('ado')}/p-skill"])
    _assert_surfaces(world, "p-skill", "project")
    assert _hosts(world) == {ADO_HOST}, world.services.egress_log()
    presented = world.services.proxy.hosts_receiving
    if credential == "pat":
        assert presented(base64.b64encode(f":{ADO_PAT}".encode()).decode()) | presented(
            base64.b64encode(f"x:{ADO_PAT}".encode()).decode()
        ) == {ADO_HOST}
    else:
        assert presented(ADO_BEARER) == {ADO_HOST}
        calls = (Path(world.env["AZURE_CONFIG_DIR"]) / AZ_CALL_LOG).read_text("utf-8")
        assert '"get-access-token"' in calls


def test_ado_auth_mode_is_reset_for_the_next_world(
    lifecycle_template: World, tmp_path: Path
) -> None:
    lifecycle_template.services.require_ado_auth()
    world = clone(lifecycle_template, tmp_path / "next")
    world.services.reset()
    assert world.services.ado_auth_required is False


def test_ado_policy_is_read_through_the_items_api(lifecycle_world: World) -> None:
    world = lifecycle_world
    world.services.require_ado_auth()
    use_ado_pat(world)
    use_policy(world, "deny-pkg", host="ado")
    result = driver.run(world, ["install", world.packages["p-exec"].spec])
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "denied by pattern" in output, output
    items = [
        e
        for e in world.services.egress_log()
        if e["host"] == ADO_HOST and "/_apis/git/repositories/apm-policy/items" in e["path"]
    ]
    assert items and items[0]["status"] == 200, world.services.egress_log()


# -- GHE Cloud /api/v3 ------------------------------------------------------------


def _get(world: World, url: str, accept: str = "application/vnd.github+json"):
    return requests.get(
        url,
        headers={"Accept": accept},
        proxies={"https": world.services.proxy_url},
        verify=str(world.services.ca_path),
        timeout=30,
    )


def test_ghe_cloud_serves_contents_and_commits_under_api_v3(lifecycle_world: World) -> None:
    world = lifecycle_world
    base = f"https://{HOST_ALIASES['ghe'][0]}/api/v3/repos/{ORG}/p-skill"
    contents = _get(world, f"{base}/contents/apm.yml?ref=v1.0.0")
    assert contents.status_code == 200, contents.text
    assert b"p-skill" in base64.b64decode(contents.json()["content"])
    commit = _get(world, f"{base}/commits/v1.0.0")
    assert commit.status_code == 200 and len(commit.json()["sha"]) == 40, commit.text
    sha = _get(world, f"{base}/commits/v1.0.0", accept="application/vnd.github.sha")
    assert sha.text == commit.json()["sha"]
    assert _get(world, f"{base}/contents/missing.md").status_code == 404


def _repo_api_urls(shape: str) -> tuple[str, str]:
    """(apm.yml contents URL, commit-of-v1.0.0 URL) of world repo p-skill on *shape*."""
    host = HOST_ALIASES[shape][0]
    if shape == "gitlab":
        project = quote(f"{HOST_ALIASES[shape][1]}/p-skill", safe="")
        base = f"https://{host}/api/v4/projects/{project}/repository"
        return f"{base}/files/apm.yml/raw?ref=v1.0.0", f"{base}/commits/v1.0.0"
    if shape == "ado":
        base = f"https://{host}/{ORG}/{ORG}/_apis/git/repositories/p-skill"
        items = f"{base}/items?path=apm.yml&versionDescriptor.version=v1.0.0&api-version=7.0"
        return items, f"{base}/commits?searchCriteria.itemVersion.version=v1.0.0"
    prefix = "/api/v1" if shape == "generic" else "/api/v3"
    base = f"https://{host}{prefix}/repos/{ORG}/p-skill"
    return f"{base}/contents/apm.yml?ref=v1.0.0", f"{base}/commits/v1.0.0"


def _commit_sha(response: requests.Response) -> str:
    payload = response.json()
    if "value" in payload:
        return payload["value"][0]["commitId"]
    return payload.get("sha") or payload["id"]


@pytest.mark.parametrize("shape", sorted(HOST_ALIASES))
def test_every_git_host_serves_repository_contents_and_commits(
    lifecycle_world: World, shape: str
) -> None:
    world = lifecycle_world
    expected = _get(world, f"https://{API_HOST}/repos/{ORG}/p-skill/commits/v1.0.0").json()["sha"]
    contents_url, commit_url = _repo_api_urls(shape)
    contents = _get(world, contents_url)
    assert contents.status_code == 200, contents.text
    body = contents.content
    if contents.headers.get("Content-Type", "").startswith("application/json"):
        body = base64.b64decode(contents.json()["content"])
    assert b"name: p-skill" in body
    commit = _get(world, commit_url)
    assert commit.status_code == 200 and _commit_sha(commit) == expected, commit.text
    missing = _get(world, contents_url.replace("apm.yml", "missing.md"))
    assert missing.status_code == 404


def test_ghe_cloud_policy_is_read_through_api_v3(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_policy(world, "deny-pkg", host="ghe")
    result = driver.run(world, ["install", world.packages["p-exec"].spec])
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "denied by pattern" in output, output
    served = f"/api/v3/repos/{policy_org('deny-pkg')}/.github/contents/apm-policy.yml"
    statuses = [
        e["status"]
        for e in world.services.egress_log()
        if e["host"] == HOST_ALIASES["ghe"][0] and e["path"] == served
    ]
    assert 200 in statuses, world.services.egress_log()


# -- unserve / restore ---------------------------------------------------------


def test_unserved_repository_is_gone_until_restored(lifecycle_world: World) -> None:
    world = lifecycle_world
    slug = f"{ORG}/p-skill"
    spec = world.packages["p-skill:git"].spec
    world.services.unserve(slug)
    result = driver.run(world, ["install", spec])
    assert result.returncode != 0, result.stdout
    assert not (world.project / ".agents/skills/p-skill").exists()
    refused = {e["status"] for e in world.services.egress_log() if "p-skill" in e["path"]}
    assert refused == {404}, world.services.egress_log()
    world.services.restore(slug)
    _ok(world, ["install", spec])
    _assert_surfaces(world, "p-skill", "project")


def test_reset_restores_every_unserved_repository(
    lifecycle_template: World, tmp_path: Path
) -> None:
    lifecycle_template.services.unserve(f"{ORG}/p-exec")
    world = clone(lifecycle_template, tmp_path / "next")
    world.services.reset()
    _ok(world, ["install", world.packages["p-exec:git"].spec])
    with pytest.raises(KeyError):
        world.services.restore(f"{ORG}/p-exec")
    with pytest.raises(KeyError):
        world.services.unserve(f"{ORG}/never-served")


# -- scripted routes, registry proxy, proxy-only ---------------------------------


def test_scripted_redirect_answers_with_a_3xx_to_another_host(lifecycle_world: World) -> None:
    world = lifecycle_world
    mirror = MIRROR_HOSTS[0]
    world.services.redirect(API_HOST, f"/repos/{ORG}/p-skill", f"https://{mirror}/moved")
    world.services.script(mirror, "/moved", 200, {"Content-Type": "text/plain"}, b"mirrored")
    first = requests.get(
        f"https://{API_HOST}/repos/{ORG}/p-skill",
        proxies={"https": world.services.proxy_url},
        verify=str(world.services.ca_path),
        allow_redirects=False,
        timeout=30,
    )
    assert first.status_code == 307 and first.headers["Location"] == f"https://{mirror}/moved"
    assert _get(world, f"https://{API_HOST}/repos/{ORG}/p-skill").text == "mirrored"
    assert {e["host"] for e in world.services.egress_log()} == {API_HOST, mirror}


def test_cross_origin_registry_redirect_is_refused(lifecycle_world: World) -> None:
    """A 3xx off the configured registry origin: no request, no token, reaches the mirror."""
    world = lifecycle_world
    use_registry(world)
    mirror = MIRROR_HOSTS[1]
    world.services.redirect(registry.HOST, registry.BASE, f"https://{mirror}{registry.BASE}")
    result = driver.run(world, ["install", world.packages["p-skill:registry"].spec])
    assert result.returncode != 0, result.stdout
    assert mirror not in _hosts(world), world.services.egress_log()
    assert not (world.project / ".agents/skills/p-skill").exists()


def test_proxy_only_install_never_goes_direct(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_registry_proxy(world)
    world.services.forbid_direct()
    _ok(world, ["install", world.packages["p-skill:git"].spec])
    _assert_surfaces(world, "p-skill", "project")
    assert _hosts(world) == {PROXY_HOST}, world.services.egress_log()
    assert world.services.proxy.hosts_receiving(PROXY_TOKEN) == {PROXY_HOST}


def test_forbid_direct_refuses_and_records_direct_content(lifecycle_world: World) -> None:
    world = lifecycle_world
    world.services.forbid_direct()
    result = driver.run(world, ["install", world.packages["p-skill:git"].spec])
    assert result.returncode != 0, result.stdout
    statuses = {e["status"] for e in world.services.egress_log() if e["host"] != PROXY_HOST}
    assert statuses == {403}, world.services.egress_log()
    # Operator-configured registries are sanctioned routes under PROXY_REGISTRY_ONLY.
    assert _get(world, f"https://{REGISTRY_HOST}/v0.1/servers").status_code == 200
    registry_versions = f"{registry.URL}/v1/packages/{ORG}/p-skill/versions"
    sanctioned = requests.get(
        registry_versions,
        headers={"Authorization": f"Bearer {registry.TOKEN}"},
        proxies={"https": world.services.proxy_url},
        verify=str(world.services.ca_path),
        timeout=30,
    )
    assert sanctioned.status_code == 200, sanctioned.text
    world.services.reset()
    assert world.services.routes.direct_forbidden is False
    assert world.services.routes.scripted == []


# -- new git revisions ---------------------------------------------------------


def _tag_sha(world: World, tag: str) -> int | str:
    response = _get(world, f"https://{API_HOST}/repos/{ORG}/p-skill/commits/{tag}")
    return response.json()["sha"] if response.status_code == 200 else response.status_code


def test_published_revision_changes_content_under_a_ref_until_reset(
    lifecycle_template: World, tmp_path: Path
) -> None:
    services = lifecycle_template.services
    world = clone(lifecycle_template, tmp_path / "first")
    services.reset()
    original = _tag_sha(world, "v1.0.0")
    skill = "skills/p-skill/SKILL.md"
    changed = "---\nname: p-skill\ndescription: changed upstream.\n---\n\nRevision 2.\n"
    sha = services.publish_revision(f"{ORG}/p-skill", {skill: changed}, tag="v1.0.0")
    services.publish_revision(f"{ORG}/p-skill", {skill: changed}, tag="v2.0.0")
    assert _tag_sha(world, "v1.0.0") == sha != original
    _ok(world, ["install", f"{ORG}/p-skill#v1.0.0"])
    deployed = world.project / ".agents/skills/p-skill/SKILL.md"
    assert "Revision 2." in deployed.read_text(encoding="utf-8")
    services.reset()
    assert _tag_sha(world, "v1.0.0") == original
    assert _tag_sha(world, "v2.0.0") == 404
    with pytest.raises(KeyError):
        services.publish_revision(f"{ORG}/never-served", {"x": "y"})


# -- publish / override_repository -------------------------------------------------


def _skill_tree(world: World, dest: Path, body: str) -> Path:
    shutil.copytree(world.packages["p-skill"].spec, dest)
    skill = dest / "skills/p-skill/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + f"\n{body}\n", encoding="utf-8")
    return dest


def test_publish_serves_a_test_authored_repository_until_reset(
    lifecycle_world: World, tmp_path: Path
) -> None:
    world = lifecycle_world
    tree = _skill_tree(world, tmp_path / "tree", "Published by the test.")
    world.services.publish(f"{ORG}/p-published", tree)
    _ok(world, ["install", f"{ORG}/p-published#v1.0.0"])
    deployed = world.project / ".agents/skills/p-skill/SKILL.md"
    assert "Published by the test." in deployed.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        world.services.publish(f"{ORG}/p-skill", tree)
    world.services.reset()
    assert _get(world, f"https://{API_HOST}/repos/{ORG}/p-published").status_code == 404


def test_override_repository_replaces_a_world_repository_until_reset(
    lifecycle_world: World, tmp_path: Path
) -> None:
    world = lifecycle_world
    original = _get(world, f"https://{API_HOST}/repos/{ORG}/p-skill/commits/v1.0.0").json()["sha"]
    tree = _skill_tree(world, tmp_path / "tree", "Overridden upstream.")
    world.services.override_repository(f"{ORG}/p-skill", tree)
    _ok(world, ["install", world.packages["p-skill:git"].spec])
    deployed = world.project / ".agents/skills/p-skill/SKILL.md"
    assert "Overridden upstream." in deployed.read_text(encoding="utf-8")
    world.services.reset()
    restored = _get(world, f"https://{API_HOST}/repos/{ORG}/p-skill/commits/v1.0.0").json()["sha"]
    assert restored == original
    with pytest.raises(KeyError):
        world.services.override_repository(f"{ORG}/never-served", tree)


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
    "malformed": 0,
    "garbage_response": 0,
    "cached_stale": 1,
}
_POLICY_ANNOUNCE = {
    "deny-pkg": "denied by pattern",
    "unreachable": "HTTP 503",
    "hash-mismatch": "Policy hash mismatch",
    "chain-cycle": "Cycle detected in policy extends chain",
    "malformed": "is not valid YAML",
    "garbage_response": "is not a YAML mapping",
    "cached_stale": "Using stale cached policy",
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
        ("deny-native", "p-native-claude-bin"),
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
