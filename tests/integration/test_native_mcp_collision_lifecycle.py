"""Installed-CLI contracts for Claude/Codex native connection collisions."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import tomlkit

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_differing_foreign_connection_is_incomplete_with_truthful_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    foreign_target: str,
) -> None:
    """Preserve foreign bytes while recording only the nonconflicting target write."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if user_scope else project
    codex_home = isolated.home / ".codex"
    environment = isolated.subprocess_env(
        overrides={"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": str(codex_home)}
    )
    paths = {
        "claude": isolated.home / ".claude.json" if user_scope else project / ".mcp.json",
        "codex": (codex_home if user_scope else project / ".codex") / "config.toml",
    }
    requested_url = "http://127.0.0.1:8000/company"
    personal_url = "http://127.0.0.1:9/personal"
    foreign_path = paths[foreign_target]
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    if foreign_target == "claude":
        foreign_path.write_text(
            json.dumps(
                {
                    "preferences": {"keep": True},
                    "mcpServers": {"notes": {"type": "http", "url": personal_url}},
                }
            ),
            encoding="utf-8",
        )
    else:
        foreign_path.write_text(
            f'model = "personal-model"\n\n[mcp_servers.notes]\nurl = "{personal_url}"\n',
            encoding="utf-8",
        )
    original = foreign_path.read_bytes()
    dump_yaml(
        {
            "name": "native-collision-consumer",
            "version": "1.0.0",
            "targets": ["claude", "codex"],
            "dependencies": {
                "mcp": [
                    {
                        "name": "notes",
                        "registry": False,
                        "transport": "http",
                        "url": requested_url,
                    }
                ]
            },
        },
        root / "apm.yml",
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        (
            "install",
            *(("--global",) if user_scope else ()),
            "--target",
            "claude,codex",
            "--no-policy",
        ),
        cwd=project,
        env=environment,
        scenario_id=f"native-collision-{foreign_target}-{'user' if user_scope else 'project'}",
    )
    evidence = f"command={result.command!r}\n{result.stdout}\n{result.stderr}"
    assert foreign_path.read_bytes() == original, evidence
    written_target = "codex" if foreign_target == "claude" else "claude"
    written_path = paths[written_target]
    assert written_path.is_file(), evidence
    if written_target == "codex":
        written = tomlkit.parse(written_path.read_text(encoding="utf-8"))["mcp_servers"]
    else:
        written = json.loads(written_path.read_text(encoding="utf-8"))["mcpServers"]
    assert written["notes"]["url"] == requested_url, evidence
    lock = LockFile.read(root / "apm.lock.yaml")
    assert lock is not None, evidence
    assert lock.mcp_target_servers == {written_target: ["notes"]}, evidence
    assert result.returncode != 0, evidence
    assert foreign_target in (result.stdout + result.stderr).lower(), evidence


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    project: Path
    root: Path
    user_scope: bool
    runner: ApmLifecycleRunner
    environment_overrides: dict[str, str] = field(default_factory=dict)

    def path(self, target: str) -> Path:
        """Resolve only the isolated native target configuration."""
        if target == "claude":
            return (
                self.isolated.home / ".claude.json"
                if self.user_scope
                else self.project / ".mcp.json"
            )
        return (self.isolated.home if self.user_scope else self.project) / ".codex/config.toml"

    def run(self, *args: str) -> CommandResult:
        """Run the installed CLI without host credentials, state, or network."""
        environment = self.isolated.subprocess_env(
            overrides={
                "CLAUDE_CONFIG_DIR": "",
                "CODEX_HOME": str(self.isolated.home / ".codex"),
            }
        )
        environment.update(self.environment_overrides)
        return self.runner.run(
            (*args, *(("--global",) if self.user_scope else ())),
            cwd=self.project,
            env=environment,
            scenario_id="native-mcp-collision-lifecycle",
        )

    def install(self, *flags: str, targets: str = "claude,codex") -> CommandResult:
        """Reconcile the requested scope and exact target selection."""
        return self.run("install", "--target", targets, "--no-policy", *flags)

    def declare(self, servers: list[dict[str, Any]]) -> None:
        """Change root intent without manufacturing native deployment state."""
        manifest = load_yaml(self.root / "apm.yml")
        manifest.setdefault("dependencies", {})["mcp"] = servers
        dump_yaml(manifest, self.root / "apm.yml")

    def write(self, target: str, url: str) -> None:
        """Seed a personal connection and unrelated native settings."""
        path = self.path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if target == "claude":
            path.write_text(
                json.dumps(
                    {
                        "preferences": {"keep": True},
                        "mcpServers": {
                            "notes": {"type": "http", "url": url},
                        },
                    }
                ),
                encoding="utf-8",
            )
        else:
            path.write_text(
                f'# user comment\nmodel = "personal"\n[mcp_servers.notes]\nurl = "{url}"\n',
                encoding="utf-8",
            )

    def servers(self, target: str) -> dict:
        """Read actual native entries, retaining empty-file post-prune evidence."""
        text = self.path(target).read_text(encoding="utf-8")
        if target == "claude":
            return json.loads(text).get("mcpServers", {})
        return dict(tomlkit.parse(text).get("mcp_servers", {}))

    def edit(self, target: str, name: str, entry: dict) -> None:
        """Simulate a user's edit without discarding other native entries."""
        path = self.path(target)
        text = path.read_text(encoding="utf-8")
        if target == "claude":
            data = json.loads(text)
            data.setdefault("mcpServers", {})[name] = entry
            data["preferences"] = {"keep": True}
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            data = tomlkit.parse(text)
            data.setdefault("mcp_servers", {})[name] = entry
            data["model"] = "personal-model"
            path.write_text(tomlkit.dumps(data), encoding="utf-8")

    def lock(self) -> LockFile:
        """Read the actual durable per-target ownership state."""
        lock = LockFile.read(self.root / "apm.lock.yaml")
        assert lock is not None
        return lock


def _http(url: str = "http://127.0.0.1:8000/company") -> dict[str, Any]:
    return {"name": "notes", "registry": False, "transport": "http", "url": url}


def _success(result: CommandResult) -> None:
    assert result.returncode == 0, f"{result.command!r}\n{result.stdout}\n{result.stderr}"


@pytest.fixture(params=[False, True], ids=["project", "user"])
def scenario_factory(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Callable[[ApmLifecycleRunner], _Scenario]:
    """Create isolated state; each test injects its canonical installed CLI."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "native", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if request.param else project
    dump_yaml(
        {
            "name": "native-consumer",
            "version": "1.0.0",
            "targets": ["claude", "codex"],
            "dependencies": {},
        },
        root / "apm.yml",
    )
    return lambda runner: _Scenario(isolated, project, root, request.param, runner)


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
@pytest.mark.parametrize("identical", [False, True], ids=["differing", "identical"])
def test_direct_collision_repeat_force_frozen_and_prune(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
    identical: bool,
) -> None:
    """Direct adds never adopt foreign matches or overwrite differing connections."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    requested = _http()["url"]
    scenario.write(foreign_target, requested if identical else "http://127.0.0.1:9/personal")
    before = scenario.path(foreign_target).read_bytes()
    added = scenario.install("--mcp", "notes", "--url", requested)
    assert (added.returncode == 0) is identical, added.stdout + added.stderr
    other = "codex" if foreign_target == "claude" else "claude"
    assert scenario.path(foreign_target).read_bytes() == before
    assert scenario.servers(other)["notes"]["url"] == requested
    assert scenario.lock().mcp_target_servers == {other: ["notes"]}
    lock_bytes = (scenario.root / "apm.lock.yaml").read_bytes()
    other_bytes = scenario.path(other).read_bytes()
    for flags in ((), ("--force",), ("--frozen",)):
        repeated = scenario.install(*flags)
        expected_success = identical and "--frozen" not in flags
        assert (repeated.returncode == 0) is expected_success, repeated.stdout + repeated.stderr
        if "--frozen" in flags:
            assert "ownership is not recorded" in " ".join(repeated.stdout.split())
        assert scenario.path(foreign_target).read_bytes() == before
        assert scenario.path(other).read_bytes() == other_bytes
        assert scenario.servers(other)["notes"]["url"] == requested
        assert scenario.lock().mcp_target_servers == {other: ["notes"]}
        assert (scenario.root / "apm.lock.yaml").read_bytes() == lock_bytes
    scenario.declare([])
    _success(scenario.install())
    assert scenario.path(foreign_target).read_bytes() == before
    assert scenario.servers(other) == {}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("modified_target", ["claude", "codex"])
def test_managed_connection_user_edits_block_replay_and_removal(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    modified_target: str,
) -> None:
    """An old ownership name cannot authorize overwriting or deleting edited content."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install(targets=modified_target))
    scenario.write(modified_target, "http://127.0.0.1:9/personal")
    before = scenario.path(modified_target).read_bytes()
    original_lock = (scenario.root / "apm.lock.yaml").read_bytes()
    for flags in ((), ("--force",), ("--frozen",)):
        result = scenario.install(*flags, targets=modified_target)
        assert result.returncode != 0, result.stdout + result.stderr
        assert scenario.path(modified_target).read_bytes() == before
        assert (scenario.root / "apm.lock.yaml").read_bytes() == original_lock
    scenario.declare([])
    removed = scenario.install(targets=modified_target)
    assert removed.returncode != 0, removed.stdout + removed.stderr
    assert scenario.path(modified_target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {modified_target: ["notes"]}


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_historical_false_claim_preserves_foreign_connection(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
) -> None:
    """Historical ownership of both targets is not trusted over differing native bytes."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    other = "codex" if foreign_target == "claude" else "claude"
    scenario.declare([_http()])
    _success(scenario.install(targets=other))
    scenario.write(foreign_target, "http://127.0.0.1:9/personal")
    lock = scenario.lock()
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lock, {"claude": ["notes"], "codex": ["notes"]}
    )
    lock.write(scenario.root / "apm.lock.yaml")
    before = scenario.path(foreign_target).read_bytes()
    scenario.declare([])
    result = scenario.install()
    assert result.returncode != 0, result.stdout + result.stderr
    assert scenario.path(foreign_target).read_bytes() == before
    assert "baseline" in (result.stdout + result.stderr).lower()
    assert "notes" in scenario.lock().mcp_target_servers[foreign_target]


def test_managed_package_root_override_and_target_narrowing(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path
) -> None:
    """A root override updates the unchanged old target and writes the new target."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create(
        "notes-package", mcp_dependencies=(_http("http://127.0.0.1:8000/package"),)
    )
    _success(scenario.install(str(package.root), "--trust-transitive-mcp", targets="claude"))
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"]}
    _success(scenario.install("--mcp", "notes", "--url", _http()["url"]))
    for target in ("claude", "codex"):
        assert scenario.servers(target)["notes"]["url"] == _http()["url"]
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}
    before = tuple(scenario.path(target).read_bytes() for target in ("claude", "codex"))
    _success(scenario.install("--trust-transitive-mcp"))
    assert tuple(scenario.path(target).read_bytes() for target in ("claude", "codex")) == before
    _success(scenario.install("--trust-transitive-mcp", targets="codex"))
    assert scenario.servers("claude") == {}
    assert scenario.lock().mcp_target_servers == {"codex": ["notes"]}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_package_uninstall_preserves_user_modified_connection(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """Package removal cannot delete a user's edited connection under an old claim."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create(
        "notes-package", mcp_dependencies=(_http(),)
    )
    _success(scenario.install(str(package.root), "--trust-transitive-mcp", targets=target))
    scenario.write(target, "http://127.0.0.1:9/personal")
    before = scenario.path(target).read_bytes()
    result = scenario.run("uninstall", str(package.root))
    assert result.returncode != 0, result.stdout + result.stderr
    assert scenario.path(target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {target: ["notes"]}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_scoped_retirement_preserves_distinct_personal_short_name(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """Verification of a scoped key never authorizes deleting its short alias."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([{**_http(), "name": "@company/notes"}])
    _success(scenario.install(targets=target))
    personal = {"url": "http://127.0.0.1:9/personal"}
    if target == "claude":
        personal["type"] = "http"
    scenario.edit(target, "notes", personal)
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {"notes": personal}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_matching_foreign_revocation_is_not_target_retirement(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """An explicitly requested user edit is a successful unowned, byte-stable no-op."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install(targets=target))
    personal = {**scenario.servers(target)["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit(target, "notes", personal)
    before = scenario.path(target).read_bytes()
    _success(
        scenario.install("--mcp", "notes", "--url", personal["url"], "--force", targets=target)
    )
    assert scenario.path(target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.path(target).read_bytes() == before


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_partial_install_retains_deferred_names_in_active_target(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """Persist new writes and deferred retirements, but never resurrect a revoked claim."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([{**_http(), "name": "old"}, _http()])
    _success(scenario.install(targets=target))
    personal = {**scenario.servers(target)["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit(target, "notes", personal)
    scenario.declare([{**_http(), "name": "new"}, _http()])
    result = scenario.install(targets=target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert set(scenario.servers(target)) == {"old", "new", "notes"}
    assert scenario.servers(target)["notes"] == personal
    assert scenario.lock().mcp_target_servers == {target: ["new", "old"]}
    assert "old" in scenario.lock().mcp_configs
    scenario.declare([{**_http(), "name": "new"}])
    _success(scenario.install(targets=target))
    assert set(scenario.servers(target)) == {"new", "notes"}
    assert scenario.servers(target)["notes"] == personal
    assert scenario.lock().mcp_target_servers == {target: ["new"]}


@pytest.mark.parametrize("cwd_mode", ["absent", "declared", "native-only"])
def test_claude_managed_env_removal_repeats_and_retires(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    cwd_mode: str,
) -> None:
    """Omitted managed fields are removed, while runtime-owned OAuth state survives."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    dependency = {
        "name": "notes",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": ["hello"],
    }
    old_dependency = {**dependency, "env": {"OLD_SETTING": "value"}}
    if cwd_mode == "declared":
        old_dependency["cwd"] = "/fixture-cwd"
    scenario.declare([old_dependency])
    _success(scenario.install(targets="claude"))
    scenario.edit(
        "claude",
        "notes",
        {
            **scenario.servers("claude")["notes"],
            "oauthAccount": {"id": "runtime-owned"},
            **({"cwd": "/fixture-cwd"} if cwd_mode == "native-only" else {}),
        },
    )
    scenario.declare([dependency])
    _success(scenario.install(targets="claude"))
    assert "env" not in scenario.servers("claude")["notes"]
    assert scenario.servers("claude")["notes"]["oauthAccount"] == {"id": "runtime-owned"}
    assert ("cwd" in scenario.servers("claude")["notes"]) is (cwd_mode == "native-only")
    before = scenario.path("claude").read_bytes()
    _success(scenario.install(targets="claude"))
    assert scenario.path("claude").read_bytes() == before
    scenario.declare([])
    retired = scenario.install(targets="claude")
    if cwd_mode == "native-only":
        assert retired.returncode == 1
        assert scenario.path("claude").read_bytes() == before
    else:
        _success(retired)
        assert scenario.servers("claude") == {}


def _registry(scenario: _Scenario) -> LocalMcpRegistry:
    """Serve one real registry response, allowing only that loopback port."""
    from urllib.parse import urlparse

    registry = LocalMcpRegistryFactory(scenario.isolated.work_root / "registry").start(
        {
            "name": "io.example/registered",
            "description": "Local lifecycle fixture",
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "http://127.0.0.1:8000/registered"}],
        }
    )
    scenario.environment_overrides = {
        "APM_TEST_LOOPBACK_PORTS": str(urlparse(registry.url).port),
        "MCP_REGISTRY_ALLOW_HTTP": "1",
    }
    return registry


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_fresh_registry_write_can_be_retired_without_new_lookup(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """The actual write, not a later registry response, proves removable native content."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
        assert registry.request_paths
        receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
        assert set(receipts[target]) == {"io.example/registered"}
        assert receipts[target]["io.example/registered"].startswith("sha256:")
        _success(scenario.install(targets=target))
        assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    _success(scenario.install("--only", "apm", targets=target))
    assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    assert scenario.lock().mcp_target_servers == {target: ["io.example/registered"]}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_registry_receipt_survives_package_lock_rebuild(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path, target: str
) -> None:
    """An actual APM dependency exercises the full lockfile-builder path."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create("receipt-owner")
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(str(package.root), targets=target))
        assert scenario.lock().dependencies
        receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
        assert receipts[target]["io.example/registered"].startswith("sha256:")
        native_before = scenario.path(target).read_bytes()
        for flags in (("--only", "apm"), ()):
            _success(scenario.install(*flags, targets=target))
            assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
            assert scenario.lock().mcp_target_servers == {target: ["io.example/registered"]}
            assert scenario.path(target).read_bytes() == native_before
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().dependencies


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_registry_partial_conflict_does_not_skip_managed_self_defined_update(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
) -> None:
    """A failed registry target cannot publish an unattempted self-defined baseline."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install())
    personal = {"url": "http://127.0.0.1:9/personal"}
    if foreign_target == "claude":
        personal["type"] = "http"
    scenario.edit(foreign_target, "registered", personal)
    updated = _http("http://127.0.0.1:8000/updated")
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}, updated])
        partial = scenario.install()
        assert partial.returncode == 1, partial.stdout + partial.stderr
        for target in ("claude", "codex"):
            assert urlparse(scenario.servers(target)["notes"]["url"]) == urlparse(updated["url"])
        assert scenario.servers(foreign_target)["registered"] == personal
        assert urlparse(scenario.lock().mcp_configs["notes"]["url"]) == urlparse(updated["url"])
        assert scenario.lock().mcp_target_servers[foreign_target] == ["notes"]
        written_target = "codex" if foreign_target == "claude" else "claude"
        assert scenario.lock().mcp_target_servers[written_target] == [
            "io.example/registered",
            "notes",
        ]
        scenario.edit(
            foreign_target,
            "registered",
            {**personal, "url": "http://127.0.0.1:8000/registered"},
        )
        _success(scenario.install())
        assert scenario.lock().mcp_target_servers[foreign_target] == ["notes"]


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_metadata_only_replacement_write_retains_registry_receipt(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path, target: str
) -> None:
    """Compose replacement-only lock persistence with actual MCP write ownership."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
    receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
    assert receipts[target]["io.example/registered"].startswith("sha256:")
    native_before = scenario.path(target).read_bytes()
    manifest = load_yaml(scenario.root / "apm.yml")
    manifest["$schema"] = (
        "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
    )
    manifest["replace"] = []
    dump_yaml(manifest, scenario.root / "apm.yml")
    _success(scenario.install("--only", "apm", targets=target))
    assert scenario.lock().replacements == []
    assert not scenario.lock().dependencies
    assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    assert scenario.path(target).read_bytes() == native_before
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_registry_write_before_all_self_defined_conflicts_is_persisted(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """A failed ordinary install still records the real registry-phase write."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.write(target, "http://127.0.0.1:9/personal")
    personal = scenario.servers(target)["notes"]
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}, _http()])
        result = scenario.install(targets=target)
        assert result.returncode == 1, result.stdout + result.stderr
        assert registry.request_paths
    assert scenario.servers(target)["notes"] == personal
    assert "registered" in scenario.servers(target)
    assert scenario.lock().mcp_target_servers == {target: ["io.example/registered"]}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {"notes": personal}


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("evidence", ["edited", "hashless", "deleted-lock"])
def test_registry_receipt_never_adopts_or_deletes_unverified_native_state(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
    evidence: str,
) -> None:
    """Names, registry lookups, and missing locks cannot substitute for an actual-write receipt."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
        if evidence == "edited":
            scenario.edit(
                target,
                "registered",
                {
                    **scenario.servers(target)["registered"],
                    "url": "http://127.0.0.1:9/personal",
                },
            )
        elif evidence == "hashless":
            lock = scenario.lock()
            DeploymentLedgerCodec.replace_mcp_target_servers(
                lock, lock.mcp_target_servers, native_hashes={}
            )
            lock.write(scenario.root / "apm.lock.yaml")
            scenario.declare([])
            refused = scenario.install(targets=target)
            assert refused.returncode == 1, refused.stdout + refused.stderr
            scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        else:
            (scenario.root / "apm.lock.yaml").unlink()
        before = scenario.path(target).read_bytes()
        repeated = scenario.install(targets=target)
        assert repeated.returncode == (1 if evidence == "edited" else 0)
        assert scenario.path(target).read_bytes() == before
        if evidence != "edited":
            assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == {}
    scenario.declare([])
    result = scenario.install(targets=target)
    assert result.returncode == (1 if evidence == "edited" else 0)
    assert scenario.path(target).read_bytes() == before
