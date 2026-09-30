"""Hermetic Python CLI lifecycles for Claude transport redeclarations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.lifecycle_smoke]

_SERVER = "transport-fixture"
_OAUTH = {"accountUuid": "fixture-account"}
_HEADERS = {"Authorization": "Bearer fixture-only"}
_TRANSPORT_KEYS = {
    "http": {"command", "args", "env", "cwd"},
    "stdio": {"url", "headers"},
}


def _declaration(transport: str, revision: int = 1) -> dict[str, Any]:
    declaration: dict[str, Any] = {
        "name": _SERVER,
        "registry": False,
        "transport": transport,
    }
    if transport == "http":
        declaration.update(url=f"https://example.invalid/mcp/{revision}", headers=_HEADERS)
    else:
        declaration.update(
            command="python",
            args=["-m", "fixture_server", str(revision)],
            env={"FIXTURE_TOKEN": "stdio-value"},
        )
    return declaration


def _write_config(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    project: Path
    user_scope: bool
    runner: ApmLifecycleRunner

    def root(self, user_scope: bool) -> Path:
        return self.isolated.config_root if user_scope else self.project

    def config_path(self, user_scope: bool) -> Path:
        return self.isolated.home / ".claude.json" if user_scope else self.project / ".mcp.json"

    def document(self) -> dict[str, Any]:
        return json.loads(self.config_path(self.user_scope).read_text(encoding="utf-8"))

    def declare(self, transport: str, revision: int = 1) -> None:
        path = self.root(self.user_scope) / "apm.yml"
        manifest = load_yaml(path)
        manifest["dependencies"]["mcp"] = [_declaration(transport, revision)]
        dump_yaml(manifest, path)

    def install(self, *flags: str, user_scope: bool | None = None) -> CommandResult:
        scope = self.user_scope if user_scope is None else user_scope
        return self.runner.run(
            (
                "install",
                "--target",
                "claude",
                "--no-policy",
                *(("--global",) if scope else ()),
                *flags,
            ),
            scenario_id=f"claude-transport-{'user' if scope else 'project'}",
            cwd=self.project,
            env=self.isolated.subprocess_env(overrides={"CLAUDE_CONFIG_DIR": ""}),
        )

    def snapshot(self, user_scope: bool) -> tuple[LifecycleStateSnapshot, bytes]:
        return (
            LifecycleStateSnapshot.capture(
                self.root(user_scope),
                config_paths=() if user_scope else (PurePosixPath(".mcp.json"),),
            ),
            self.config_path(user_scope).read_bytes(),
        )


@pytest.fixture(params=[False, True], ids=["project", "user"])
def scenario(
    tmp_path: Path, apm_engine_command: tuple[str, ...], request: pytest.FixtureRequest
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    package = LocalPackageFactory(isolated.work_root).create(
        "claude-consumer", mcp_dependencies=(_declaration("stdio"),), targets=("claude",)
    )
    (package.root / ".claude").mkdir()
    dump_yaml(load_yaml(package.manifest_path), isolated.config_root / "apm.yml")
    result = _Scenario(
        isolated, package.root, request.param, ApmLifecycleRunner(apm_engine_command)
    )
    for scope in (False, True):
        _write_config(
            result.config_path(scope),
            {
                "mcpServers": {
                    "unmanaged": {
                        "type": "http",
                        "url": "https://untouched.invalid/mcp",
                        "command": "do-not-normalize",
                    }
                },
                "oauthAccount": {"accountUuid": "top-level-fixture"},
                "preferences": {"keep": True},
                "projects": {"private-project": {"mcpServers": {"keep": {"command": "keep"}}}},
            },
        )
    return result


def _prepare(scenario: _Scenario, transport: str) -> dict[str, Any]:
    scenario.declare(transport)
    _assert_success(scenario.install(user_scope=not scenario.user_scope))
    _assert_success(scenario.install())
    document = scenario.document()
    document["mcpServers"][_SERVER]["oauthAccount"] = _OAUTH
    if transport == "stdio":
        document["mcpServers"][_SERVER]["cwd"] = "/fixture-cwd"
    _write_config(scenario.config_path(scenario.user_scope), document)
    return _unrelated(document)


def _unrelated(document: dict[str, Any]) -> dict[str, Any]:
    return {
        **document,
        "mcpServers": {
            key: value for key, value in document["mcpServers"].items() if key != _SERVER
        },
    }


def _assert_entry(scenario: _Scenario, transport: str, revision: int) -> None:
    entry = scenario.document()["mcpServers"][_SERVER]
    assert entry["type"] == transport
    assert not (_TRANSPORT_KEYS[transport] & entry.keys())
    assert entry["oauthAccount"] == _OAUTH
    if transport == "http":
        parsed = urlparse(entry["url"])
        assert (parsed.scheme, parsed.hostname, parsed.path) == (
            "https",
            "example.invalid",
            f"/mcp/{revision}",
        )
        assert entry["headers"] == _HEADERS
    else:
        assert entry["command"] == "python"
        assert entry["args"] == ["-m", "fixture_server", str(revision)]
        assert entry["env"] == {"FIXTURE_TOKEN": "stdio-value"}


def _assert_repeat(scenario: _Scenario) -> None:
    selected = scenario.snapshot(scenario.user_scope)
    opposite = scenario.snapshot(not scenario.user_scope)
    _assert_success(scenario.install())
    assert scenario.snapshot(scenario.user_scope) == selected
    assert scenario.snapshot(not scenario.user_scope) == opposite


@pytest.mark.parametrize("initial", ["http", "stdio"])
def test_transport_redeclaration_preserves_unowned_state(scenario: _Scenario, initial: str) -> None:
    """Real install, denied frozen rewrite, rewrite and repeat converge safely."""
    unrelated = _prepare(scenario, initial)
    opposite = scenario.snapshot(not scenario.user_scope)
    target = "stdio" if initial == "http" else "http"
    scenario.declare(target)
    before_frozen = scenario.snapshot(scenario.user_scope)
    frozen = scenario.install("--frozen")
    assert frozen.returncode != 0
    assert f"MCP server '{_SERVER}' config differs" in frozen.stdout
    assert scenario.snapshot(scenario.user_scope) == before_frozen
    assert scenario.snapshot(not scenario.user_scope) == opposite

    _assert_success(scenario.install())
    _assert_entry(scenario, target, 1)
    assert _unrelated(scenario.document()) == unrelated
    assert scenario.snapshot(not scenario.user_scope) == opposite
    _assert_repeat(scenario)


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_legacy_mixed_state_is_repaired_only_on_redeclaration(
    scenario: _Scenario, transport: str
) -> None:
    """An unchanged install skips legacy state; actual same-family drift repairs it."""
    unrelated = _prepare(scenario, transport)
    document = scenario.document()
    entry = document["mcpServers"][_SERVER]
    if transport == "http":
        entry.update(command="old", args=["old"], env={"OLD_TOKEN": "fixture-only"}, cwd="/old")
    else:
        entry.update(url="https://stale.invalid/mcp", headers={"Authorization": "old-fixture"})
    _write_config(scenario.config_path(scenario.user_scope), document)
    _assert_repeat(scenario)
    opposite = scenario.snapshot(not scenario.user_scope)

    scenario.declare(transport, revision=2)
    _assert_success(scenario.install())
    _assert_entry(scenario, transport, 2)
    if transport == "stdio":
        assert scenario.document()["mcpServers"][_SERVER]["cwd"] == "/fixture-cwd"
    assert _unrelated(scenario.document()) == unrelated
    assert scenario.snapshot(not scenario.user_scope) == opposite
    _assert_repeat(scenario)
