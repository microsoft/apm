"""Installed-CLI coverage for #3252: Claude project ``.mcp.json`` keeps references.

Claude Code expands ``${VAR}`` and ``${VAR:-default}`` in a project ``.mcp.json``
(``command``, ``args``, ``env``, ``url``, ``headers``) when it starts a server, so
``apm install`` must keep references there instead of collecting or copying the
values. User-scope ``~/.claude.json`` keeps install-time resolution. All values
are synthetic sentinels; every test asserts they reach no file or output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

SECRET = "claude-e2e-secret-sentinel"
OTHER_SECRET = "claude-e2e-other-sentinel"

SELF_DEFINED_STDIO = {
    "name": "refs-stdio",
    "registry": False,
    "transport": "stdio",
    "command": "${E2E_CMD:-uvx}",
    "args": ["refs-server", "--token=${E2E_TOKEN}", "--mode=${E2E_MODE:-safe}", "<E2E_TOKEN>"],
    "env": {
        "TOKEN": "${E2E_TOKEN}",
        "PREFIXED": "${env:E2E_TOKEN}",
        "DEFAULTED": "${E2E_UNSET:-fallback}",
        "MISSING": "${E2E_MISSING}",
        "LOG_LEVEL": "debug",
    },
}

SELF_DEFINED_HTTP = {
    "name": "refs-http",
    "registry": False,
    "transport": "http",
    "url": "https://${E2E_HOST:-mcp.example.invalid}/mcp?key=${E2E_TOKEN}",
    "headers": {
        "Authorization": "Bearer ${E2E_TOKEN}",
        "X-Region": "${E2E_REGION:-eu}",
        "X-Missing": "${E2E_HTTP_MISSING}",
    },
}

EXPECTED_STDIO = {
    "type": "stdio",
    "command": "${E2E_CMD:-uvx}",
    "args": ["refs-server", "--token=${E2E_TOKEN}", "--mode=${E2E_MODE:-safe}", "${E2E_TOKEN}"],
    "env": {
        "TOKEN": "${E2E_TOKEN}",
        "PREFIXED": "${E2E_TOKEN}",
        "DEFAULTED": "${E2E_UNSET:-fallback}",
        "MISSING": "${E2E_MISSING}",
        "LOG_LEVEL": "debug",
    },
}

EXPECTED_HTTP = {
    "type": "http",
    "url": "https://${E2E_HOST:-mcp.example.invalid}/mcp?key=${E2E_TOKEN}",
    "headers": {
        "Authorization": "Bearer ${E2E_TOKEN}",
        "X-Region": "${E2E_REGION:-eu}",
        "X-Missing": "${E2E_HTTP_MISSING}",
    },
}

UNSET_NAMES = ("E2E_UNSET", "E2E_MISSING", "E2E_HTTP_MISSING", "E2E_CMD", "E2E_MODE", "E2E_HOST")

Scenario = tuple[Path, dict[str, str], IsolatedApmEnvironment]


@pytest.fixture
def claude_project(tmp_path: Path) -> Scenario:
    """A scrubbed environment with a Claude-enabled project and one set variable."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "claude", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    (project / ".claude").mkdir(parents=True)
    env = isolated.subprocess_env()
    env["E2E_TOKEN"] = SECRET
    env["APM_NON_INTERACTIVE"] = "1"
    for name in UNSET_NAMES:
        env.pop(name, None)
    return project, env, isolated


def _write_apm_yml(directory: Path, servers: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "claude-refs-e2e",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": servers},
    }
    (directory / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _install(binary: Path, project: Path, env: dict[str, str], *extra: str):
    runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
    return runner.run(("install", "--only", "mcp", "--no-policy", *extra), cwd=project, env=env)


def _servers(project: Path) -> dict:
    return json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def _flat(text: str) -> str:
    """Join the CLI's terminal-width line wrapping back into single spaces."""
    return " ".join(text.split())


def _assert_no_value(texts: list[str], *values: str) -> None:
    for text in texts:
        for value in values:
            assert value not in text


def test_project_config_keeps_references_in_every_field(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, _isolated = claude_project
    _write_apm_yml(project, [SELF_DEFINED_STDIO, SELF_DEFINED_HTTP])

    result = _install(apm_binary_path, project, env, "--target", "claude")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    servers = _servers(project)
    assert servers["refs-stdio"] == EXPECTED_STDIO
    assert servers["refs-http"] == EXPECTED_HTTP
    _assert_no_value(
        [(project / ".mcp.json").read_text(), (project / "apm.lock.yaml").read_text(), output],
        SECRET,
    )
    # Names only, once per run, covering both servers; defaulted names are not missing.
    assert output.count("Claude Code will resolve") == 1
    assert "E2E_HTTP_MISSING, E2E_MISSING" in _flat(output)
    for defaulted in ("E2E_UNSET", "E2E_CMD", "E2E_MODE", "E2E_HOST", "E2E_REGION"):
        assert defaulted not in output
    assert "Enter value for" not in output


def test_registry_server_keeps_references(claude_project: Scenario, apm_binary_path: Path) -> None:
    project, env, isolated = claude_project
    document = {
        "name": "io.github.apm/claude-refs-registry",
        "description": "Claude reference fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@apm/claude-refs-registry",
                "transport": {"type": "stdio"},
                "environmentVariables": [
                    {"name": "E2E_REQUIRED", "required": True},
                    {"name": "E2E_OPTIONAL", "required": False},
                ],
                "packageArguments": [
                    {
                        "type": "named",
                        "name": "--api-key",
                        "value": "{api_key}",
                        "variables": {"api_key": {"isSecret": True}},
                    }
                ],
            }
        ],
    }
    env["E2E_REQUIRED"] = SECRET
    env["api_key"] = OTHER_SECRET
    with LocalMcpRegistryFactory(isolated.root / "registries").start(document) as registry:
        env["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(registry.url).port)
        env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        _write_apm_yml(project, [{"name": document["name"], "registry": registry.url}])
        result = _install(apm_binary_path, project, env, "--target", "claude")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    server = _servers(project)["claude-refs-registry"]
    assert server["env"] == {"E2E_REQUIRED": "${E2E_REQUIRED}"}
    assert server["args"][-2:] == ["--api-key", "${api_key}"]
    _assert_no_value(
        [(project / ".mcp.json").read_text(), (project / "apm.lock.yaml").read_text(), output],
        SECRET,
        OTHER_SECRET,
    )


def test_user_scope_keeps_install_time_resolution(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, isolated = claude_project
    user_config = isolated.home / ".claude.json"
    user_config.write_text(json.dumps({"numStartups": 3}), encoding="utf-8")
    user_config.chmod(0o600)
    _write_apm_yml(
        isolated.config_root,
        [
            {
                "name": "user-refs",
                "registry": False,
                "transport": "stdio",
                "command": "uvx",
                "env": {"TOKEN": "${E2E_TOKEN}"},
            }
        ],
    )

    result = _install(apm_binary_path, project, env, "--global", "--target", "claude")

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(user_config.read_text(encoding="utf-8"))
    assert data["numStartups"] == 3
    assert data["mcpServers"]["user-refs"]["env"] == {"TOKEN": SECRET}
    assert user_config.stat().st_mode & 0o077 == 0
    assert not (project / ".mcp.json").exists()


def test_mixed_targets_use_their_own_native_representation(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, _isolated = claude_project
    for signal in (".cursor", ".github", ".vscode"):
        (project / signal).mkdir()
    _write_apm_yml(project, [SELF_DEFINED_STDIO])

    result = _install(apm_binary_path, project, env, "--target", "claude,cursor,copilot,vscode")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    configs = {
        "claude": project / ".mcp.json",
        "cursor": project / ".cursor" / "mcp.json",
        "copilot": project / ".github" / "mcp.json",
        "vscode": project / ".vscode" / "mcp.json",
    }
    loaded = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in configs.items()}
    assert loaded["claude"]["mcpServers"]["refs-stdio"]["env"]["TOKEN"] == "${E2E_TOKEN}"
    assert loaded["cursor"]["mcpServers"]["refs-stdio"]["env"]["TOKEN"] == "${env:E2E_TOKEN}"
    assert loaded["copilot"]["mcpServers"]["refs-stdio"]["env"]["TOKEN"] == "${E2E_TOKEN}"
    assert loaded["vscode"]["servers"]["refs-stdio"]["env"]["TOKEN"] == "${env:E2E_TOKEN}"
    _assert_no_value([path.read_text() for path in configs.values()] + [output], SECRET)


def test_unrelated_entries_and_native_consent_are_untouched(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, _isolated = claude_project
    user_entry = {"type": "stdio", "command": "node", "env": {"KEY": "user-authored-value"}}
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"user-kept": user_entry}}), encoding="utf-8"
    )
    consent = project / ".claude" / "settings.local.json"
    consent.write_text(json.dumps({"enabledMcpjsonServers": ["user-kept"]}), encoding="utf-8")
    consent_before = consent.read_bytes()
    _write_apm_yml(project, [SELF_DEFINED_STDIO])

    result = _install(apm_binary_path, project, env, "--target", "claude")

    assert result.returncode == 0, result.stdout + result.stderr
    servers = _servers(project)
    assert servers["user-kept"] == user_entry
    assert servers["refs-stdio"] == EXPECTED_STDIO
    assert consent.read_bytes() == consent_before


def _expand_like_earlier_releases(project: Path) -> None:
    """Rewrite .mcp.json the way releases before #3252 wrote it."""
    path = project / ".mcp.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("${E2E_TOKEN}", SECRET), encoding="utf-8"
    )


def test_reinstall_replaces_values_apm_wrote_earlier(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, _isolated = claude_project
    _write_apm_yml(project, [SELF_DEFINED_STDIO, SELF_DEFINED_HTTP])
    assert _install(apm_binary_path, project, env, "--target", "claude").returncode == 0
    _expand_like_earlier_releases(project)
    assert SECRET in (project / ".mcp.json").read_text(encoding="utf-8")

    result = _install(apm_binary_path, project, env, "--target", "claude")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert _servers(project) == {"refs-stdio": EXPECTED_STDIO, "refs-http": EXPECTED_HTTP}
    assert "previously stored as plaintext in .mcp.json" in _flat(output)
    assert "Affected: E2E_TOKEN" in _flat(output)
    assert "rotate these credentials" in _flat(output)
    _assert_no_value([output, (project / "apm.lock.yaml").read_text()], SECRET)


def test_reinstall_preserves_values_it_cannot_prove_it_wrote(
    claude_project: Scenario, apm_binary_path: Path
) -> None:
    project, env, _isolated = claude_project
    _write_apm_yml(project, [SELF_DEFINED_STDIO])
    assert _install(apm_binary_path, project, env, "--target", "claude").returncode == 0
    path = project / ".mcp.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("${E2E_TOKEN}", OTHER_SECRET), encoding="utf-8"
    )
    before = path.read_bytes()

    result = _install(apm_binary_path, project, env, "--target", "claude")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert path.read_bytes() == before
    assert "Preserved that connection" in _flat(output)
    assert "Fields args[1], args[3], env.PREFIXED, env.TOKEN hold values" in _flat(output)
    assert "rotate those credentials" in _flat(output)
    _assert_no_value([output], SECRET, OTHER_SECRET)
