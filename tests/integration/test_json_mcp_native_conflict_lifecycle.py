"""Same-name MCP connections on every JSON adapter follow the Claude/Codex contract (#3681).

A differing user connection is preserved and reported (exit 1, no ownership
claim); an identical one is left alone and never claimed; connections APM wrote
keep updating, survive a repeat install unchanged and are removed on uninstall.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from tests.integration._target_compile_kit import Scenario, output, write, write_project
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

INSTALL = ("install", "--no-policy")

# target -> (config path, dot dir that opts the project in, servers path, user entry)
TARGETS = {
    "vscode": (".vscode/mcp.json", ".vscode", ("servers",), {"command": "usercmd"}),
    "copilot": (".github/mcp.json", ".github", ("mcpServers",), {"command": "usercmd"}),
    "cursor": (".cursor/mcp.json", ".cursor", ("mcpServers",), {"command": "usercmd"}),
    "gemini": (".gemini/settings.json", ".gemini", ("mcpServers",), {"command": "usercmd"}),
    "kiro": (".kiro/settings/mcp.json", ".kiro", ("mcpServers",), {"command": "usercmd"}),
    "opencode": (
        "opencode.json",
        ".opencode",
        ("mcp",),
        {"type": "local", "command": ["usercmd"]},
    ),
}


def _package(root: Path, command: str = "pkgcmd", env: str = "") -> Path:
    write(
        root / "apm.yml",
        "name: pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n    - name: shared\n"
        f"      registry: false\n      transport: stdio\n      command: {command}\n"
        f"      args: [a]\n{env}",
    )
    return root


def _project(tmp_path: Path, target: str) -> Path:
    project = write_project(tmp_path / "proj", (target,))
    (project / TARGETS[target][1]).mkdir(exist_ok=True)
    return project


def _servers(project: Path, target: str) -> dict:
    path = project / TARGETS[target][0]
    if not path.exists():
        return {}
    node = json.loads(path.read_text(encoding="utf-8"))
    for key in TARGETS[target][2]:
        node = node.get(key, {})
    return node.get("servers", node) if target == "opencode" and "servers" in node else node


def _seed_user(project: Path, target: str, entry: dict) -> bytes:
    relative, _, keys, _ = TARGETS[target]
    write(project / relative, json.dumps({keys[0]: {"shared": entry, "mine": entry}}) + "\n")
    return (project / relative).read_bytes()


def _owned(project: Path) -> dict:
    lock = LockFile.read(project / "apm.lock.yaml")
    return {} if lock is None else dict(lock.mcp_target_servers or {})


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_differing_user_connection_is_preserved_and_reported(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    _package(tmp_path / "pkg")
    project = _project(tmp_path, target)
    before = _seed_user(project, target, TARGETS[target][3])

    for _ in range(2):
        result = scenario.run(*INSTALL, cwd=project, expect=1)
        text = output(result)
        assert (project / TARGETS[target][0]).read_bytes() == before, text
        assert "conflicts with the existing" in text, text
        assert "already configured" not in text, text
        assert "shared" not in _owned(project).get(target, []), text

    forced = scenario.run(*INSTALL, "--force", cwd=project, expect=1)
    assert (project / TARGETS[target][0]).read_bytes() == before, output(forced)

    scenario.run("uninstall", "../pkg", cwd=project)
    assert (project / TARGETS[target][0]).read_bytes() == before


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_identical_user_connection_is_left_unclaimed(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    _package(tmp_path / "pkg")
    project = _project(tmp_path, target)
    scenario.run(*INSTALL, cwd=project)
    rendered = _servers(project, target)["shared"]
    scenario.run("uninstall", "../pkg", cwd=project)
    assert "shared" not in _servers(project, target)

    write_project(tmp_path / "proj", (target,))
    before = _seed_user(project, target, rendered)
    scenario.run(*INSTALL, cwd=project)
    assert (project / TARGETS[target][0]).read_bytes() == before
    assert "shared" not in _owned(project).get(target, [])
    scenario.run("uninstall", "../pkg", cwd=project)
    assert (project / TARGETS[target][0]).read_bytes() == before


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_managed_connection_repeats_updates_and_retires(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = _package(tmp_path / "pkg")
    project = _project(tmp_path, target)
    config = project / TARGETS[target][0]

    scenario.run(*INSTALL, cwd=project)
    assert _owned(project) == {target: ["shared"]}
    receipts = DeploymentLedgerCodec.mcp_native_hashes(LockFile.read(project / "apm.lock.yaml"))
    assert receipts[target]["shared"].startswith("sha256:")
    written = config.read_bytes()

    repeat = scenario.run(*INSTALL, cwd=project)
    assert config.read_bytes() == written, output(repeat)
    assert "conflicts with the existing" not in output(repeat)

    _package(package, command="pkgcmd2")
    scenario.run(*INSTALL, cwd=project)
    assert "pkgcmd2" in json.dumps(_servers(project, target)["shared"])
    assert _owned(project) == {target: ["shared"]}

    scenario.run("uninstall", "../pkg", cwd=project)
    assert "shared" not in _servers(project, target)


def test_resolved_env_value_change_keeps_managed_gemini_connection(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An unchanged declaration keeps APM's written entry; a user edit still conflicts."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    _package(tmp_path / "pkg", env="      env:\n        TOKEN: ${MCP_TOKEN}\n")
    project = _project(tmp_path, "gemini")
    config = project / TARGETS["gemini"][0]

    scenario.run(*INSTALL, cwd=project, env={**scenario.env, "MCP_TOKEN": "first-value"})
    assert "first-value" in json.dumps(_servers(project, "gemini")["shared"])
    written = config.read_bytes()
    repeat = scenario.run(*INSTALL, cwd=project, env={**scenario.env, "MCP_TOKEN": "second-value"})
    assert config.read_bytes() == written, output(repeat)
    assert "conflicts with the existing" not in output(repeat)

    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcpServers"]["shared"]["args"] = ["user-edit"]
    config.write_text(json.dumps(data), encoding="utf-8")
    edited = config.read_bytes()
    scenario.run(*INSTALL, cwd=project, expect=1, env={**scenario.env, "MCP_TOKEN": "third"})
    assert config.read_bytes() == edited


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_registry_connection_receipt_and_user_edit(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    registry = LocalMcpRegistryFactory(scenario.isolated.work_root / "registry").start(
        {
            "name": "io.example/registered",
            "description": "Local lifecycle fixture",
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "http://127.0.0.1:8000/registered"}],
        }
    )
    with registry:
        env = {
            **scenario.env,
            "APM_TEST_LOOPBACK_PORTS": str(urlparse(registry.url).port),
            "MCP_REGISTRY_ALLOW_HTTP": "1",
        }
        project = _project(tmp_path, target)
        write(
            project / "apm.yml",
            f"name: proj\nversion: 1.0.0\ntargets: [{target}]\ndependencies:\n  mcp:\n"
            f"    - name: io.example/registered\n      registry: {registry.url}\n",
        )
        scenario.run(*INSTALL, cwd=project, env=env)
        lock = LockFile.read(project / "apm.lock.yaml")
        assert set(DeploymentLedgerCodec.mcp_native_hashes(lock)[target]) == {
            "io.example/registered"
        }
        config = project / TARGETS[target][0]
        written = config.read_bytes()
        repeat = scenario.run(*INSTALL, cwd=project, env=env)
        assert config.read_bytes() == written, output(repeat)

        text = written.decode("utf-8")
        assert "127.0.0.1:8000/registered" in text
        config.write_text(text.replace("8000/registered", "9/personal"), encoding="utf-8")
        edited = config.read_bytes()
        result = scenario.run(*INSTALL, cwd=project, env=env, expect=1)
        assert config.read_bytes() == edited
        assert "conflicts with the existing" in output(result)


@pytest.mark.parametrize(
    ("target", "relative"),
    [
        ("windsurf", ".codeium/windsurf/mcp_config.json"),
        ("antigravity", ".gemini/config/mcp_config.json"),
    ],
)
def test_user_scope_only_adapters_report_conflict_and_keep_managed_entry(
    tmp_path: Path, apm_binary_path: Path, target: str, relative: str
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = _package(tmp_path / "pkg")
    config = scenario.isolated.home / relative
    install = ("install", "-g", str(package), "--target", target, "--no-policy")
    write(config, json.dumps({"mcpServers": {"shared": {"command": "usercmd"}}}) + "\n")
    before = config.read_bytes()

    result = scenario.run(*install, cwd=tmp_path, expect=1)
    assert config.read_bytes() == before
    assert "conflicts with the existing" in output(result)

    config.unlink()
    scenario.run(*install, cwd=tmp_path)
    written = config.read_bytes()
    assert "pkgcmd" in written.decode("utf-8")
    scenario.run(*install, cwd=tmp_path)
    assert config.read_bytes() == written
