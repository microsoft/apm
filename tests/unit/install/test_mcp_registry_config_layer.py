"""`apm install` must query the registry the precedence chain names (#2740).

``apm config set mcp-registry-url <url>`` was honoured by the ``apm mcp``
commands and by ``apm install --mcp NAME``, but the manifest-driven
``dependencies.mcp`` path built its registry client straight off
``MCP_REGISTRY_URL``. A server that ``apm mcp show`` found on a private
registry was therefore looked up on the public default during ``apm install``
and reported as missing.

These tests drive the real CLI over a recorded HTTP layer and assert on the
hosts actually contacted. Direct helper tests cover the recovery guidance.
Together they fail if any future refactor reintroduces a second resolution
path or silently changes the retry boundary.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner, Result

from apm_cli.cli import cli
from apm_cli.integration.mcp_integrator_install import _validate_registry_servers
from apm_cli.registry.client import (
    OFFICIAL_MCP_REGISTRY_URL,
    SimpleRegistryClient,
    is_valid_mcp_server_name,
)
from apm_cli.registry.operations import MCPServerOperations

CONFIG_REGISTRY = "https://registry.internal.example"
ENV_REGISTRY = "https://env-registry.internal.example"

SERVER = {
    "name": "ado-mcp",
    "description": "internal Azure DevOps server",
    "version": "1.0.0",
    "packages": [{"registryType": "npm", "identifier": "@internal/ado-mcp", "version": "1.0.0"}],
}

MANIFEST = (
    "name: registry-layer-probe\n"
    "version: 0.1.0\n"
    "targets: copilot\n"
    "dependencies:\n"
    "  mcp:\n"
    "    - name: ado-mcp\n"
    "      registry: true\n"
)


class _RecordedResponse:
    """Minimal stand-in honouring the capped-stream read contract."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self) -> None:
        return None


@pytest.fixture
def recorded_registry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve ``ado-mcp`` from configured and Official registries; record hosts."""
    hosts: list[str] = []
    populated_hosts = {
        urlparse(CONFIG_REGISTRY).hostname,
        urlparse(OFFICIAL_MCP_REGISTRY_URL).hostname,
    }

    def _get(_self: requests.Session, url: str, **_kwargs: object) -> _RecordedResponse:
        hostname = urlparse(url).hostname or ""
        hosts.append(hostname)
        if hostname not in populated_hosts:
            return _RecordedResponse({"servers": []})
        if "/versions/" in url:
            return _RecordedResponse({"server": SERVER})
        return _RecordedResponse({"servers": [{"server": SERVER}]})

    monkeypatch.setattr(requests.Session, "get", _get)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    return hosts


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A temp project declaring one registry-backed MCP dependency."""
    from apm_cli import config as config_mod

    (tmp_path / "apm.yml").write_text(MANIFEST, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config_dir = tmp_path / ".apm"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_dir / "config.json"))
    config_mod._invalidate_config_cache()
    return tmp_path


def _install(args: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(cli, ["install", "--no-policy", *args])


def _printed_hosts(output: str) -> set[str]:
    """Hostnames of every URL in CLI output, parsed rather than substring-matched."""
    return {
        urlparse(token.strip("(),.;'\"")).hostname for token in output.split() if "://" in token
    }


def _extract_try_argv(output: str) -> list[str]:
    """Extract the literal copyable ``Try:`` command from one output line."""
    match = re.search(
        r'Try: (?P<command>apm install --mcp "[^"]+" --registry \S+)',
        output,
    )
    assert match is not None, output
    return shlex.split(match.group("command"))


def test_manifest_install_uses_configured_registry(monkeypatch, recorded_registry, project):
    """The persisted config layer reaches the manifest-driven install path."""
    from apm_cli.config import set_mcp_registry_url

    set_mcp_registry_url(CONFIG_REGISTRY)

    result = _install()

    assert result.exit_code == 0, result.output
    assert set(recorded_registry) == {urlparse(CONFIG_REGISTRY).hostname}


def test_manifest_install_announces_the_configured_registry(
    monkeypatch, recorded_registry, project
):
    """A redirect away from the public registry is stated, never inferred."""
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: CONFIG_REGISTRY
    )

    result = _install()

    assert "Using MCP registry" in result.output
    assert "from apm config" in result.output


def test_env_layer_still_outranks_the_config_layer(monkeypatch, recorded_registry, project):
    """MCP_REGISTRY_URL keeps precedence, so the lookup misses and fails loudly."""
    monkeypatch.setenv("MCP_REGISTRY_URL", ENV_REGISTRY)
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: CONFIG_REGISTRY
    )

    result = _install()

    assert result.exit_code != 0
    assert set(recorded_registry) == {urlparse(ENV_REGISTRY).hostname}


def test_missing_server_error_names_the_registry_queried(monkeypatch, recorded_registry, project):
    """The failure names the endpoint, so a wrong registry is diagnosable."""
    monkeypatch.setenv("MCP_REGISTRY_URL", ENV_REGISTRY)

    result = _install()

    assert result.exit_code != 0
    assert "not found in registry" in result.output
    assert _printed_hosts(result.output) == {urlparse(ENV_REGISTRY).hostname}


def test_manifest_default_registry_miss_keeps_generic_guidance(
    monkeypatch, recorded_registry, project
):
    """A manifest miss keeps generic guidance and never names another registry."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)

    result = _install()

    assert result.exit_code != 0
    assert "Using MCP registry" not in result.output
    assert set(recorded_registry) == {"api.mcp.github.com"}
    assert _printed_hosts(result.output) == {"api.mcp.github.com"}
    assert "apm mcp search" in result.output
    assert "Try:" not in result.output


@pytest.mark.windows_compat
def test_direct_default_miss_prints_round_trippable_official_retry(
    monkeypatch: pytest.MonkeyPatch,
    recorded_registry: list[str],
    project: Path,
) -> None:
    """The direct hint parses as printed and opts into only the Official registry."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)
    (project / "apm.yml").write_text(
        "name: registry-layer-probe\nversion: 0.1.0\ntargets: copilot\ndependencies:\n  mcp: []\n",
        encoding="utf-8",
    )

    result = _install(("--mcp", "ado-mcp"))

    assert result.exit_code != 0
    assert result.output.count("Try:") == 1
    assert set(recorded_registry) == {"api.mcp.github.com"}
    assert _printed_hosts(result.output) == {
        "api.mcp.github.com",
        urlparse(OFFICIAL_MCP_REGISTRY_URL).hostname,
    }
    retry_argv = _extract_try_argv(result.output)
    assert retry_argv[:-1] == ["apm", "install", "--mcp", "ado-mcp", "--registry"]
    retry_url = urlparse(retry_argv[-1])
    assert (retry_url.scheme, retry_url.hostname, retry_url.path) == (
        "https",
        "registry.modelcontextprotocol.io",
        "",
    )

    recorded_registry.clear()
    retry_result = CliRunner().invoke(cli, retry_argv[1:])

    assert retry_result.exit_code == 0, retry_result.output
    assert set(recorded_registry) == {urlparse(OFFICIAL_MCP_REGISTRY_URL).hostname}


def _missing_operations(
    server_names: list[str], source: str | None, registry_url: str
) -> MagicMock:
    operations = MagicMock()
    operations.validate_servers_exist.return_value = ([], server_names)
    operations.registry_client = SimpleNamespace(
        registry_url=registry_url,
        registry_url_source=source,
    )
    return operations


def test_default_registry_miss_suggests_ordered_exact_name_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each missing name gets a copyable, deterministic official-registry retry."""
    server_names = ["io.example.weather/alerts~v2", "io.example.weather/forecast"]
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url",
        lambda *, create_config=False: None,
    )
    operations = MCPServerOperations()
    monkeypatch.setattr(
        operations.registry_client,
        "find_server_by_reference",
        MagicMock(return_value=None),
    )
    logger = MagicMock()

    with pytest.raises(RuntimeError, match="Cannot install 2 missing server"):
        _validate_registry_servers(
            operations,
            server_names,
            dependency_count=2,
            verbose=False,
            logger=logger,
            suggest_official_registry=True,
        )

    assert [call.args[0] for call in logger.info.call_args_list] == [
        (
            f'Try: apm install --mcp "{server_name}" '
            "--registry https://registry.modelcontextprotocol.io"
        )
        for server_name in server_names
    ]
    assert all(is_valid_mcp_server_name(server_name) for server_name in server_names)


@pytest.mark.parametrize(
    "server_name",
    ["bad;name", 'bad"name', "bad\nname", "valid-looking\n"],
)
def test_official_retry_rejects_unsafe_server_names(server_name: str) -> None:
    """Unvalidated names never cross into a pasted shell command."""
    assert not is_valid_mcp_server_name(server_name)
    with pytest.raises(ValueError, match="Invalid server name"):
        SimpleRegistryClient("https://api.mcp.github.com").get_server(server_name)
    operations = _missing_operations(
        [server_name],
        "default",
        "https://api.mcp.github.com",
    )
    logger = MagicMock()

    with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
        _validate_registry_servers(
            operations,
            [server_name],
            dependency_count=1,
            verbose=False,
            logger=logger,
            suggest_official_registry=True,
        )

    logger.info.assert_not_called()
    logger.progress.assert_called_once_with(
        "Run 'apm mcp search <query>' to find available servers"
    )


@pytest.mark.parametrize("source", ["explicit", "flag", "env", "config", None])
def test_registry_override_miss_does_not_suggest_another_registry(source: str | None) -> None:
    """A selected registry remains the sole trust authority after a miss."""
    registry_url = "https://user:super-secret@private.example/api?token=hidden#fragment"
    operations = _missing_operations(["io.example.weather/alerts"], source, registry_url)
    logger = MagicMock()

    with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
        _validate_registry_servers(
            operations,
            ["io.example.weather/alerts"],
            dependency_count=1,
            verbose=False,
            logger=logger,
            suggest_official_registry=True,
        )

    output = " ".join(
        call.args[0]
        for method in (logger.error, logger.progress, logger.info)
        for call in method.call_args_list
    )
    assert _printed_hosts(output) == {"private.example"}
    assert "Try:" not in output
    assert "super-secret" not in output
    assert "token=hidden" not in output
