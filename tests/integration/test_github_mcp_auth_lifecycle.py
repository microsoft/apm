"""Installed-CLI proofs for GitHub MCP auth provenance and targeted repair."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]
SERVER = "github-mcp-server"
SOURCES = (
    "GITHUB_COPILOT_PAT",
    "GITHUB_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)
CONFIG = PurePosixPath(".github/mcp.json")
RUNTIME_CONFIGS = {"copilot": CONFIG, "cursor": PurePosixPath(".cursor/mcp.json")}
INSTALL = ("install", "--runtime", "copilot", "--no-policy", "--parallel-downloads", "0")
USER_SERVER = {"command": "user-command", "args": ["keep-me"]}


def _run(
    binary: Path, root: Path, env: dict[str, str], *args: str, runtime: str = "copilot"
) -> CommandResult:
    """Execute the installed CLI without substituting the integration boundary."""
    result = ApmLifecycleRunner((str(binary),)).run(
        args or ("install", "--runtime", runtime, "--no-policy", "--parallel-downloads", "0"),
        cwd=root,
        env=env,
        scenario_id="github-mcp-auth",
    )
    assert result.returncode == 0, f"{result.command}\n{result.stdout}\n{result.stderr}"
    return result


def _config(root: Path, runtime: str = "copilot") -> dict:
    """Read the generated client configuration from disk."""
    return json.loads((root / RUNTIME_CONFIGS[runtime]).read_text(encoding="utf-8"))


def _authorization(root: Path, runtime: str = "copilot") -> list[str]:
    """Read effective Authorization values without relying on casing."""
    headers = _config(root, runtime)["mcpServers"][SERVER].get("headers", {})
    return [value for name, value in headers.items() if name.casefold() == "authorization"]


def _seed_user_config(root: Path, runtime: str = "copilot") -> None:
    """Seed unrelated native configuration that every scenario must preserve."""
    config = root / RUNTIME_CONFIGS[runtime]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"mcpServers": {"user-owned": USER_SERVER}, "user-setting": True}),
        encoding="utf-8",
    )


def _assert_no_secret(root: Path, secrets: list[str], runtime: str = "copilot") -> None:
    """Check every credential sentinel against the complete native file."""
    text = (root / RUNTIME_CONFIGS[runtime]).read_text(encoding="utf-8")
    assert all(not secret or secret not in text for secret in secrets)
    config = _config(root, runtime)
    assert config["mcpServers"]["user-owned"] == USER_SERVER
    assert config["user-setting"] is True


@pytest.mark.parametrize(
    "winner,manifest_headers",
    [
        *((winner, {}) for winner in range(len(SOURCES) + 1)),
        (1, {"authorization": None}),
        (1, {"authorization": False}),
        (1, {"authorization": 0}),
    ],
)
@pytest.mark.parametrize("runtime", ["copilot", "cursor"])
def test_installed_auto_auth_uses_selected_source(
    tmp_path: Path,
    apm_binary_path: Path,
    winner: int,
    manifest_headers: dict[str, str | bool | int | None],
    runtime: str,
) -> None:
    """All four precedence winners and the absent-source case reach disk safely."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "source", base_env=dict(os.environ))
    tokens = {
        name: f"sentinel-{index}-must-not-persist" if index >= winner else ""
        for index, name in enumerate(SOURCES)
    }
    env = isolated.subprocess_env()
    env.update(tokens)
    project = LocalPackageFactory(isolated.work_root).create(
        "auth-source",
        targets=(runtime,),
        mcp_dependencies=(
            {
                "name": SERVER,
                "registry": False,
                "transport": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": manifest_headers,
            },
        ),
    )
    _seed_user_config(project.root, runtime)
    first = _run(apm_binary_path, project.root, env, runtime=runtime)
    prefix = "env:" if runtime == "cursor" else ""
    expected = [f"Bearer ${{{prefix}{SOURCES[winner]}}}"] if winner < len(SOURCES) else []
    assert _authorization(project.root, runtime) == expected
    _assert_no_secret(project.root, list(tokens.values()), runtime)
    config_path = project.root / RUNTIME_CONFIGS[runtime]
    before = config_path.read_bytes()
    changed_env = {**env, "GITHUB_COPILOT_PAT": "changed-precedence-sentinel"}
    repeated = _run(apm_binary_path, project.root, changed_env, runtime=runtime)
    assert config_path.read_bytes() == before
    for secret in [*tokens.values(), "changed-precedence-sentinel"]:
        if secret:
            assert secret not in first.stdout + first.stderr + repeated.stdout + repeated.stderr


def test_global_install_keeps_runtime_auth_outside_project(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """User-scope auth stays native, secret-free and isolated from the project."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "global", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["GITHUB_TOKEN"] = "global-sentinel-must-not-persist"
    project = LocalPackageFactory(isolated.work_root).create("global-consumer")
    copilot_home = Path(env.get("COPILOT_HOME", str(isolated.home / ".copilot")))
    config_path = copilot_home / "mcp-config.json"
    copilot_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"mcpServers": {"user-owned": USER_SERVER}}), encoding="utf-8"
    )
    args = (
        *INSTALL,
        "--global",
        "--mcp",
        SERVER,
        "--url",
        "https://api.githubcopilot.com/mcp/",
    )
    _run(apm_binary_path, project.root, env, *args)
    first = config_path.read_bytes()
    config = json.loads(first)
    assert config["mcpServers"][SERVER]["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"
    assert config["mcpServers"]["user-owned"] == USER_SERVER
    assert b"global-sentinel-must-not-persist" not in first
    assert not (project.root / CONFIG).exists()
    _run(apm_binary_path, project.root, env, *args)
    assert config_path.read_bytes() == first


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("other-server", "https://api.githubcopilot.com/mcp/"),
        (SERVER, "https://github.com.evil.invalid/mcp/"),
        (SERVER, "http://api.github.com/mcp/"),
    ],
)
def test_cursor_unadmitted_server_does_not_receive_automatic_auth(
    tmp_path: Path, apm_binary_path: Path, name: str, url: str
) -> None:
    """Cursor consumes the shared secure-host/name boundary without local policy."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "unadmitted", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["GITHUB_TOKEN"] = "unadmitted-secret-sentinel"
    project = LocalPackageFactory(isolated.work_root).create(
        "cursor-unadmitted",
        targets=("cursor",),
        mcp_dependencies=(
            {
                "name": name,
                "registry": False,
                "transport": "http",
                "url": url,
                "headers": {"X-Static": "authored-value"},
            },
        ),
    )
    _seed_user_config(project.root, "cursor")
    result = _run(apm_binary_path, project.root, env, runtime="cursor")
    server = _config(project.root, "cursor")["mcpServers"][name]
    assert server["headers"] == {"X-Static": "authored-value"}
    _assert_no_secret(project.root, ["unadmitted-secret-sentinel"], "cursor")
    assert "unadmitted-secret-sentinel" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "manifest_headers,expected",
    [
        ({}, "Bearer ${GITHUB_TOKEN}"),
        ({"authorization": ""}, "Bearer ${GITHUB_TOKEN}"),
        ({"authorization": "Bearer ${env:USER_PAT}"}, "Bearer ${USER_PAT}"),
        ({"AUTHORIZATION": "explicit-static-value"}, "explicit-static-value"),
    ]
    + [({"authorization": value}, "Bearer ${GITHUB_TOKEN}") for value in (None, False, 0)],
)
@pytest.mark.parametrize("runtime", ["copilot", "cursor"])
def test_registry_provenance_survives_install_reinstall_and_repair(
    tmp_path: Path,
    apm_binary_path: Path,
    manifest_headers: dict[str, str | bool | int | None],
    expected: str,
    runtime: str,
) -> None:
    """Registry defaults cannot impersonate users, including after targeted repair."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "registry", base_env=dict(os.environ))
    document = {
        "name": SERVER,
        "description": "GitHub authentication provenance fixture",
        "version": "1.0.0",
        "remotes": [
            {
                "transport_type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": [
                    {"name": "Authorization", "value": "registry-default"},
                    {"name": "X-Registry", "value": "retained"},
                ],
            }
        ],
    }
    with LocalMcpRegistryFactory(isolated.root / "registries").start(document) as registry:
        registry_url = urlparse(registry.url)
        assert registry_url.hostname == "127.0.0.1"
        assert registry_url.port is not None
        env = isolated.subprocess_env(
            overrides={
                "MCP_REGISTRY_ALLOW_HTTP": "1",
                "USER_PAT": "explicit-token-must-not-persist",
            }
        )
        env["APM_TEST_LOOPBACK_PORTS"] = str(registry_url.port)
        env["GITHUB_TOKEN"] = "ambient-token-must-not-persist"
        project = LocalPackageFactory(isolated.work_root).create(
            "auth-provenance",
            targets=(runtime,),
            mcp_dependencies=(
                {"name": SERVER, "registry": registry.url, "headers": manifest_headers},
            ),
        )
        config_path = RUNTIME_CONFIGS[runtime]
        if runtime == "cursor":
            expected = expected.replace("${", "${env:")
        _seed_user_config(project.root, runtime)
        sentinel = project.root / "unrelated.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        first_result = _run(apm_binary_path, project.root, env, runtime=runtime)
        assert registry.request_paths
        assert _authorization(project.root, runtime) == [expected]
        assert (
            _config(project.root, runtime)["mcpServers"][SERVER]["headers"]["X-Registry"]
            == "retained"
        )
        first = LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,))
        changed_env = {**env, "GITHUB_COPILOT_PAT": "new-higher-priority-token"}
        repeat_result = _run(apm_binary_path, project.root, changed_env, runtime=runtime)
        repeated = LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,))
        assert repeated.semantic_bytes == first.semantic_bytes
        assert repeated.files == first.files
        assert repeated.lockfile_bytes == first.lockfile_bytes
        # Existing entries are preserved, not migrated; explicitly remove only
        # the affected server before applying the documented repair procedure.
        config = _config(project.root, runtime)
        del config["mcpServers"][SERVER]
        (project.root / config_path).write_text(json.dumps(config), encoding="utf-8")
        repair_result = _run(apm_binary_path, project.root, changed_env, runtime=runtime)
        repaired_expected = (
            "Bearer ${GITHUB_COPILOT_PAT}" if not any(manifest_headers.values()) else expected
        )
        if runtime == "cursor" and not any(manifest_headers.values()):
            repaired_expected = "Bearer ${env:GITHUB_COPILOT_PAT}"
        assert _authorization(project.root, runtime) == [repaired_expected]
        _assert_no_secret(
            project.root,
            [
                "ambient-token-must-not-persist",
                "explicit-token-must-not-persist",
                "new-higher-priority-token",
            ],
            runtime,
        )
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        repaired = LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,))
        final_result = _run(apm_binary_path, project.root, changed_env, runtime=runtime)
        assert (
            LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,)).files
            == repaired.files
        )
        output = "".join(
            result.stdout + result.stderr
            for result in (first_result, repeat_result, repair_result, final_result)
        )
        for secret in (
            "ambient-token-must-not-persist",
            "explicit-token-must-not-persist",
            "new-higher-priority-token",
        ):
            assert secret not in output


@pytest.mark.parametrize("runtime", ["copilot", "cursor"])
@pytest.mark.parametrize(
    "manifest_headers,has_token",
    [
        ({}, True),
        ({}, False),
        ({"authorization": None}, True),
        ({"authorization": False}, True),
        ({"authorization": 0}, True),
        ({"authorization": ""}, True),
        ({"AUTHORIZATION": "authored-static"}, True),
        ({"authorization": "Bearer ${env:USER_PAT}"}, True),
    ],
)
def test_dictionary_registry_headers_install_and_repeat(
    tmp_path: Path,
    apm_binary_path: Path,
    runtime: str,
    manifest_headers: dict[str, str | bool | int | None],
    has_token: bool,
) -> None:
    """Custom-registry mapping headers traverse ingestion, overlay and real CLI."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "dictionary", base_env=dict(os.environ))
    document = {
        "name": SERVER,
        "description": "Custom registry dictionary compatibility fixture",
        "version": "1.0.0",
        "remotes": [
            {
                "transport_type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": {"Authorization": "registry-default", "X-Registry": "retained"},
            }
        ],
    }
    with LocalMcpRegistryFactory(isolated.root / "registries").start(document) as registry:
        parsed = urlparse(registry.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port is not None
        env = isolated.subprocess_env(
            overrides={"MCP_REGISTRY_ALLOW_HTTP": "1", "USER_PAT": "authored-sentinel"}
        )
        env["APM_TEST_LOOPBACK_PORTS"] = str(parsed.port)
        if has_token:
            env["GITHUB_TOKEN"] = "ambient-sentinel"
        project = LocalPackageFactory(isolated.work_root).create(
            "dictionary-consumer",
            targets=(runtime,),
            mcp_dependencies=(
                {"name": SERVER, "registry": registry.url, "headers": manifest_headers},
            ),
        )
        config_path = PurePosixPath(
            ".github/mcp.json" if runtime == "copilot" else ".cursor/mcp.json"
        )
        path = project.root / config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcpServers": {"user-owned": USER_SERVER}, "user-setting": True}),
            encoding="utf-8",
        )
        args = ("install", "--runtime", runtime, "--no-policy", "--parallel-downloads", "0")
        _run(apm_binary_path, project.root, env, *args)
        assert registry.request_paths
        config = json.loads(path.read_text(encoding="utf-8"))
        explicit = next(iter(manifest_headers.values()), None)
        variable = "USER_PAT" if explicit else "GITHUB_TOKEN"
        expected = "registry-default"
        if explicit == "authored-static":
            expected = explicit
        elif explicit or has_token:
            reference = f"${{{variable}}}" if runtime == "copilot" else f"${{env:{variable}}}"
            expected = f"Bearer {reference}"
        headers = config["mcpServers"][SERVER]["headers"]
        assert [value for key, value in headers.items() if key.casefold() == "authorization"] == [
            expected
        ]
        assert headers["X-Registry"] == "retained"
        assert config["mcpServers"]["user-owned"] == USER_SERVER
        assert config["user-setting"] is True
        assert "ambient-sentinel" not in path.read_text(encoding="utf-8")
        assert "authored-sentinel" not in path.read_text(encoding="utf-8")
        first = LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,))
        _run(apm_binary_path, project.root, {**env, "GITHUB_TOKEN": "changed-ambient"}, *args)
        repeated = LifecycleStateSnapshot.capture(project.root, config_paths=(config_path,))
        assert repeated.files == first.files
        assert repeated.lockfile_bytes == first.lockfile_bytes
