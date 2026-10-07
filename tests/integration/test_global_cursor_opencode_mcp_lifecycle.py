"""Installed-CLI ownership and refusal contracts for global MCP targets."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_TARGETS = {
    "cursor": (".cursor/mcp.json", "mcpServers"),
    "opencode": (".config/opencode/opencode.json", "mcp"),
}


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    runner: ApmLifecycleRunner
    cwd: Path
    env: dict[str, str]

    def run(self, *args: str) -> CommandResult:
        """Run the installed entrypoint, retaining command output in pytest logs."""
        result = self.runner.run(args, cwd=self.cwd, env=self.env, scenario_id="global-mcp")
        print(result.command, result.returncode, result.stdout, result.stderr)
        return result

    def config(self, target: str) -> Path:
        """Return the native user config, never the caller's workspace config."""
        return self.isolated.home / _TARGETS[target][0]

    def lock(self) -> LockFile:
        """Read the durable global ownership record."""
        lock = LockFile.read(self.isolated.config_root / "apm.lock.yaml")
        assert lock is not None
        return lock


def _scenario(tmp_path: Path, binary: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    cwd = isolated.work_root / "unrelated"
    cwd.mkdir()
    for relative in (".cursor/mcp.json", "opencode.json", "apm.yml"):
        path = cwd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"workspace": "untouched"}\n', encoding="utf-8")
    return _Scenario(isolated, ApmLifecycleRunner((str(binary),)), cwd, isolated.subprocess_env())


def _server(name: str, *args: str) -> dict:
    return {
        "name": name,
        "registry": False,
        "transport": "stdio",
        "command": "printf",
        "args": list(args or ("hello",)),
    }


def _write_root(scenario: _Scenario, servers: list[dict]) -> None:
    dump_yaml(
        {
            "name": "global-mcp",
            "version": "1.0.0",
            "targets": list(_TARGETS),
            "dependencies": {"mcp": servers},
        },
        scenario.isolated.config_root / "apm.yml",
    )


def _seed(scenario: _Scenario, target: str, servers: dict) -> None:
    path = scenario.config(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"personal": {"theme": "mine"}, _TARGETS[target][1]: servers}), encoding="utf-8"
    )


def _servers(scenario: _Scenario, target: str) -> dict:
    document = json.loads(scenario.config(target).read_text(encoding="utf-8"))
    assert document["personal"] == {"theme": "mine"}
    return document[_TARGETS[target][1]]


def _workspace(scenario: _Scenario) -> dict[str, bytes]:
    return {
        path.relative_to(scenario.cwd).as_posix(): path.read_bytes()
        for path in scenario.cwd.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("target", list(_TARGETS))
def test_installed_global_prune_preserves_skipped_foreign_name(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """Removing an unowned declaration must not remove the user's same-name entry."""
    scenario = _scenario(tmp_path, apm_binary_path)
    before = _workspace(scenario)
    foreign = {"command": ["mine"], "enabled": False}
    _seed(scenario, target, {"foreign": foreign})
    _write_root(scenario, [_server("foreign"), _server("managed")])
    install = ("install", "-g", "--target", target, "--no-policy")
    assert scenario.run(*install).returncode == 0
    assert scenario.lock().mcp_target_servers == {target: ["managed"]}
    _write_root(scenario, [_server("foreign", "new-source-value"), _server("managed")])
    assert scenario.run(*install).returncode == 0
    assert "foreign" in _servers(scenario, target)
    assert _servers(scenario, target)["foreign"] == foreign
    assert scenario.lock().mcp_target_servers == {target: ["managed"]}
    _write_root(scenario, [_server("managed")])
    assert scenario.run(*install).returncode == 0
    assert "foreign" in _servers(scenario, target)
    assert _servers(scenario, target)["foreign"] == foreign
    assert scenario.lock().mcp_target_servers == {target: ["managed"]}
    _write_root(scenario, [])
    assert scenario.run(*install).returncode == 0
    assert _servers(scenario, target) == {"foreign": foreign}
    assert scenario.lock().mcp_target_servers == {}
    assert _workspace(scenario) == before


def test_installed_global_prune_intersects_each_targets_ownership(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The same server name can be owned in one target and foreign in another."""
    scenario = _scenario(tmp_path, apm_binary_path)
    foreign = {"command": ["mine"]}
    _seed(scenario, "cursor", {})
    _seed(scenario, "opencode", {"obsolete": foreign})
    _write_root(scenario, [_server("obsolete"), _server("keep")])
    install = ("install", "-g", "--target", "cursor,opencode", "--no-policy")
    assert scenario.run(*install).returncode == 0
    assert scenario.lock().mcp_target_servers == {
        "cursor": ["keep", "obsolete"],
        "opencode": ["keep"],
    }
    _write_root(scenario, [_server("keep")])
    assert scenario.run(*install).returncode == 0
    assert "obsolete" not in _servers(scenario, "cursor")
    assert _servers(scenario, "opencode")["obsolete"] == foreign


@pytest.mark.parametrize("target", list(_TARGETS))
def test_installed_direct_global_mcp_bootstrap_retarget_and_prune(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """Direct install bootstraps user state and retires the old target before ownership."""
    scenario = _scenario(tmp_path, apm_binary_path)
    before = _workspace(scenario)
    for runtime in _TARGETS:
        _seed(scenario, runtime, {"personal-server": {"command": ["mine"]}})
    command = (
        "install",
        "-g",
        "--mcp",
        "probe",
        "--target",
        target,
        "--transport",
        "stdio",
        "--no-policy",
        "--",
        "printf",
        "hello",
    )
    assert scenario.run(*command).returncode == 0
    assert scenario.lock().mcp_target_servers == {target: ["probe"]}
    assert scenario.lock().mcp_config_provenance == {}
    assert (
        load_yaml(scenario.isolated.config_root / "apm.yml")["dependencies"]["mcp"][0]["name"]
        == "probe"
    )
    first = scenario.config(target).read_bytes()
    assert scenario.run(*command).returncode == 0
    assert scenario.config(target).read_bytes() == first
    if os.name != "nt":
        assert stat.S_IMODE(scenario.config(target).stat().st_mode) == 0o600
    other = next(runtime for runtime in _TARGETS if runtime != target)
    switched = tuple(other if argument == target else argument for argument in command)
    assert scenario.run(*switched).returncode == 0
    assert "probe" not in _servers(scenario, target)
    assert "probe" in _servers(scenario, other)
    assert scenario.lock().mcp_target_servers == {other: ["probe"]}
    _write_root(scenario, [])
    assert scenario.run("install", "-g", "--target", other, "--no-policy").returncode == 0
    for runtime in _TARGETS:
        assert _servers(scenario, runtime) == {"personal-server": {"command": ["mine"]}}
    assert _workspace(scenario) == before


@pytest.mark.parametrize("target", list(_TARGETS))
@pytest.mark.parametrize(
    "unsafe", ["jsonc", "file-symlink", "directory-symlink", "unreadable", "invalid-map"]
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and permission refusal contract")
def test_installed_global_mcp_refusal_preserves_owned_state(
    tmp_path: Path, apm_binary_path: Path, target: str, unsafe: str
) -> None:
    """Unsafe input fails even when the declared name was already configured."""
    scenario = _scenario(tmp_path, apm_binary_path)
    _seed(scenario, target, {})
    _write_root(scenario, [_server("probe")])
    command = ("install", "-g", "--target", target, "--no-policy")
    assert scenario.run(*command).returncode == 0
    lock_path = scenario.isolated.config_root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    workspace_before = _workspace(scenario)
    config = scenario.config(target)
    original = config.read_bytes()
    observed = config
    if unsafe == "jsonc":
        config.write_bytes(b"// personal comment\n" + original)
    elif unsafe == "invalid-map":
        config.write_text(json.dumps({_TARGETS[target][1]: ["personal"]}), encoding="utf-8")
    elif unsafe == "file-symlink":
        observed = config.with_name("personal.json")
        config.rename(observed)
        config.symlink_to(observed)
    elif unsafe == "directory-symlink":
        parent = config.parent
        destination = parent.with_name(parent.name + "-personal")
        parent.rename(destination)
        parent.symlink_to(destination, target_is_directory=True)
        observed = destination / config.name
    before = observed.read_bytes()
    if unsafe == "unreadable":
        config.chmod(0)
    try:
        failed = scenario.run(*command)
    finally:
        if unsafe == "unreadable":
            config.chmod(0o600)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert target in failed.stdout.lower()
    assert "failed" in failed.stdout.lower()
    assert "original install command" in failed.stdout
    assert observed.read_bytes() == before
    assert lock_path.read_bytes() == lock_before
    assert _workspace(scenario) == workspace_before


@pytest.mark.parametrize("target", list(_TARGETS))
def test_installed_global_package_update_prune_and_uninstall(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """Local source revisions drive global native state and package provenance."""
    scenario = _scenario(tmp_path, apm_binary_path)
    before = _workspace(scenario)
    factory = LocalPackageFactory(scenario.isolated.work_root / "sources")
    native = "${env:GLOBAL_MCP_TOKEN}" if target == "cursor" else "{env:GLOBAL_MCP_TOKEN}"
    managed = {
        **_server("managed", "./relative/server.js"),
        "env": {"TOKEN": native, "STATIC": "../config"},
    }
    package = factory.create(
        "global-mcp-package", mcp_dependencies=[managed, _server("obsolete"), _server("foreign")]
    )
    foreign = {"command": ["mine"]}
    _seed(scenario, target, {"foreign": foreign})
    install = ("install", "-g", "--target", target, "--no-policy", "--trust-transitive-mcp")
    assert scenario.run(*install, str(package.root)).returncode == 0
    lock = scenario.lock()
    assert lock.mcp_config_provenance == {
        name: "global-mcp-package" for name in ("managed", "obsolete", "foreign")
    }
    assert lock.mcp_target_servers == {target: ["managed", "obsolete"]}
    entry = _servers(scenario, target)["managed"]
    assert entry["env" if target == "cursor" else "environment"] == managed["env"]
    assert entry["args" if target == "cursor" else "command"][-1] == "./relative/server.js"
    stable = scenario.config(target).read_bytes()
    lock_before = (scenario.isolated.config_root / "apm.lock.yaml").read_bytes()
    assert scenario.run(*install).returncode == 0
    assert scenario.config(target).read_bytes() == stable
    assert (scenario.isolated.config_root / "apm.lock.yaml").read_bytes() == lock_before
    manifest = load_yaml(package.manifest_path)
    managed["args"] = ["./relative/revision-two.js"]
    manifest["dependencies"]["mcp"] = [managed]
    dump_yaml(manifest, package.manifest_path)
    assert scenario.run("update", "-g", "--yes", "--target", target).returncode == 0
    servers = _servers(scenario, target)
    assert "foreign" in servers
    assert servers["foreign"] == foreign
    assert "obsolete" not in servers
    assert (
        servers["managed"]["args" if target == "cursor" else "command"][-1]
        == "./relative/revision-two.js"
    )
    assert scenario.lock().mcp_config_provenance == {"managed": "global-mcp-package"}
    assert scenario.lock().mcp_configs["managed"]["args"] == ["./relative/revision-two.js"]
    assert scenario.run("uninstall", "-g", str(package.root)).returncode == 0
    assert _servers(scenario, target) == {"foreign": foreign}
    assert _workspace(scenario) == before


@pytest.mark.parametrize("target", list(_TARGETS))
def test_direct_retarget_failure_retains_old_ownership(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """Failure cleaning a retired target must retain its durable ownership."""
    scenario = _scenario(tmp_path, apm_binary_path)
    for runtime in _TARGETS:
        _seed(scenario, runtime, {})
    command = (
        "install",
        "-g",
        "--mcp",
        "probe",
        "--target",
        target,
        "--transport",
        "stdio",
        "--no-policy",
        "--",
        "printf",
        "hello",
    )
    assert scenario.run(*command).returncode == 0
    old_lock = (scenario.isolated.config_root / "apm.lock.yaml").read_bytes()
    config = scenario.config(target)
    unsafe = b"// keep this comment\n" + config.read_bytes()
    config.write_bytes(unsafe)
    other = next(runtime for runtime in _TARGETS if runtime != target)
    result = scenario.run(*(other if arg == target else arg for arg in command))
    assert result.returncode != 0, result.stdout + result.stderr
    assert config.read_bytes() == unsafe
    assert (scenario.isolated.config_root / "apm.lock.yaml").read_bytes() == old_lock
    assert scenario.lock().mcp_target_servers == {target: ["probe"]}
    assert _servers(scenario, other) == {}
    config.write_bytes(unsafe.split(b"\n", 1)[1])
    assert scenario.run(*(other if arg == target else arg for arg in command)).returncode == 0
    assert scenario.lock().mcp_target_servers == {other: ["probe"]}
    assert "probe" not in _servers(scenario, target)


@pytest.mark.parametrize("enabled", [True, 1])
def test_legacy_global_opencode_adoption_preserves_json_types(
    tmp_path: Path, apm_binary_path: Path, enabled: bool | int
) -> None:
    """A legacy install adopts only a JSON-exact native baseline before cleanup."""
    scenario = _scenario(tmp_path, apm_binary_path)
    _seed(scenario, "opencode", {})
    _write_root(scenario, [_server("probe")])
    command = ("install", "-g", "--target", "opencode", "--no-policy")
    assert scenario.run(*command).returncode == 0
    lock_path = scenario.isolated.config_root / "apm.lock.yaml"
    legacy = load_yaml(lock_path)
    legacy.pop("mcp_target_servers", None)
    legacy.pop("deployment_ledger", None)
    dump_yaml(legacy, lock_path)
    native = _servers(scenario, "opencode")["probe"]
    native["enabled"] = enabled
    _seed(scenario, "opencode", {"probe": native})
    assert scenario.run(*command).returncode == 0
    expected = {"opencode": ["probe"]} if enabled is True else {}
    assert scenario.lock().mcp_target_servers == expected
    _write_root(scenario, [])
    assert scenario.run(*command).returncode == 0
    assert _servers(scenario, "opencode") == ({} if enabled is True else {"probe": native})


@pytest.mark.parametrize("target", list(_TARGETS))
@pytest.mark.skipif(os.name == "nt", reason="POSIX home-alias contract")
def test_global_mcp_accepts_home_alias(tmp_path: Path, apm_binary_path: Path, target: str) -> None:
    """The trusted home alias is not an unsafe descendant symlink."""
    scenario = _scenario(tmp_path, apm_binary_path)
    alias = scenario.isolated.root / "home-alias"
    alias.symlink_to(scenario.isolated.home, target_is_directory=True)
    scenario.env["HOME"] = str(alias)
    _seed(scenario, target, {})
    _write_root(scenario, [_server("probe")])
    assert scenario.run("install", "-g", "--target", target, "--no-policy").returncode == 0
    assert "probe" in _servers(scenario, target)


@pytest.mark.parametrize("target", list(_TARGETS))
@pytest.mark.skipif(os.name == "nt", reason="POSIX cleanup write-failure contract")
def test_direct_retarget_cleanup_write_failure_can_be_repaired(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """A late cleanup failure keeps both actual deployments owned through retry."""
    scenario = _scenario(tmp_path, apm_binary_path)
    foreign = {"command": ["mine"]}
    for runtime in _TARGETS:
        _seed(scenario, runtime, {"foreign": foreign})
    command = (
        "install",
        "-g",
        "--mcp",
        "probe",
        "--target",
        target,
        "--transport",
        "stdio",
        "--no-policy",
        "--",
        "printf",
        "hello",
    )
    assert scenario.run(*command).returncode == 0
    other = next(runtime for runtime in _TARGETS if runtime != target)
    switched = tuple(other if argument == target else argument for argument in command)
    parent = scenario.config(target).parent
    old_mode = stat.S_IMODE(parent.stat().st_mode)
    old_config = scenario.config(target).read_bytes()
    parent.chmod(0o500)
    try:
        failed = scenario.run(*switched)
    finally:
        parent.chmod(old_mode)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert scenario.config(target).read_bytes() == old_config
    assert "probe" in _servers(scenario, other)
    assert scenario.lock().mcp_target_servers == {target: ["probe"], other: ["probe"]}
    assert scenario.run(*switched).returncode == 0
    assert scenario.lock().mcp_target_servers == {other: ["probe"]}
    assert "probe" not in _servers(scenario, target)
    _write_root(scenario, [])
    assert scenario.run("install", "-g", "--target", other, "--no-policy").returncode == 0
    for runtime in _TARGETS:
        assert _servers(scenario, runtime) == {"foreign": foreign}
