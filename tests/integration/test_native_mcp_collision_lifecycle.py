"""Installed-CLI contracts for Claude/Codex native connection collisions."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
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
        return self.runner.run(
            (*args, *(("--global",) if self.user_scope else ())),
            cwd=self.project,
            env=self.isolated.subprocess_env(
                overrides={
                    "CLAUDE_CONFIG_DIR": "",
                    "CODEX_HOME": str(self.isolated.home / ".codex"),
                }
            ),
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
