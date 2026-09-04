"""`apm install` must query the registry the precedence chain names (#2740).

``apm config set mcp-registry-url <url>`` was honoured by the ``apm mcp``
commands and by ``apm install --mcp NAME``, but the manifest-driven
``dependencies.mcp`` path built its registry client straight off
``MCP_REGISTRY_URL``. A server that ``apm mcp show`` found on a private
registry was therefore looked up on the public default during ``apm install``
and reported as missing.

These tests drive the real CLI over a recorded HTTP layer and assert on the
hosts actually contacted, so they fail if any future refactor reintroduces a
second resolution path.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner

from apm_cli.cli import cli

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
def recorded_registry(monkeypatch):
    """Serve ``ado-mcp`` from CONFIG_REGISTRY only; record every host contacted."""
    hosts: list[str] = []

    def _get(_self, url, **_kwargs):
        hosts.append(urlparse(url).hostname or "")
        if urlparse(url).hostname != urlparse(CONFIG_REGISTRY).hostname:
            return _RecordedResponse({"servers": []})
        if "/versions/" in url:
            return _RecordedResponse({"server": SERVER})
        return _RecordedResponse({"servers": [{"server": SERVER}]})

    monkeypatch.setattr(requests.Session, "get", _get)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    return hosts


@pytest.fixture
def project(monkeypatch, tmp_path):
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


def _install(args=()):
    return CliRunner().invoke(cli, ["install", "--no-policy", *args])


def _printed_hosts(output: str) -> set[str]:
    """Hostnames of every URL in CLI output, parsed rather than substring-matched."""
    return {
        urlparse(token.strip("(),.;'\"")).hostname for token in output.split() if "://" in token
    }


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


def test_public_default_registry_stays_quiet(monkeypatch, recorded_registry, project):
    """Defaults are quiet: no override in effect means no breadcrumb."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)

    result = _install()

    assert "Using MCP registry" not in result.output
