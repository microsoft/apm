"""Hermes keeps every documented MCP server key APM declares (#3749, 0.36.1).

Hermes reads ``cwd`` for stdio servers, needs ``transport: sse`` for SSE
remotes, and documents per-server keys (``timeout``, ``connect_timeout``, ...)
that APM's allowlist dropped in v0.36.0. Writes must stay idempotent, keep
unrelated top-level keys and foreign servers, and uninstall must remove only
APM's entries. Runs with ``$HERMES_HOME`` outside ``$HOME`` at user scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.integration._harness_output_kit import HarnessScene, describe
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_SSE_URL = "https://sse.example.invalid/sse"
_HTTP_URL = "https://http.example.invalid/mcp"
_DECLARATIONS = [
    {
        "name": "hermes-stdio",
        "registry": False,
        "transport": "stdio",
        "command": "node",
        "args": ["server.js"],
        "cwd": "/srv/hermes-fixture",
        "env": {"FIXTURE_MODE": "safe"},
        "timeout": 120,
        "connect_timeout": 15,
    },
    {
        "name": "hermes-sse",
        "registry": False,
        "transport": "sse",
        "url": _SSE_URL,
        "extra": {"timeout": 30},
    },
    {"name": "hermes-http", "registry": False, "transport": "http", "url": _HTTP_URL},
]
_FOREIGN = {"command": "user-owned", "args": ["--keep"], "enabled": False}
_UNRELATED = {
    "model": {"provider": "fixture-provider", "name": "fixture-model"},
    "telegram": {"enabled": False},
}


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    hermes_home = result.isolated.root / "profiles" / "hermes-home"
    hermes_home.mkdir(parents=True)
    result.env["HERMES_HOME"] = str(hermes_home)
    document = {**_UNRELATED, "mcp_servers": {"user-own": _FOREIGN}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    try:
        yield result
    finally:
        result.close()


def _config_path(scene: HarnessScene) -> Path:
    return Path(scene.env["HERMES_HOME"]) / "config.yaml"


def _document(scene: HarnessScene) -> dict:
    return yaml.safe_load(_config_path(scene).read_text(encoding="utf-8")) or {}


def test_hermes_keeps_cwd_extra_keys_and_sse_transport_idempotently(scene: HarnessScene) -> None:
    scene.write_manifest(_DECLARATIONS, user=True)

    result = scene.install("hermes", user=True)

    assert result.returncode == 0, describe(result)
    assert not (scene.home / ".hermes" / "config.yaml").exists(), "HERMES_HOME ignored"
    document = _document(scene)
    servers = document["mcp_servers"]
    stdio = servers["hermes-stdio"]
    assert stdio["command"] == "node" and stdio["args"] == ["server.js"]
    assert stdio["cwd"] == "/srv/hermes-fixture"
    assert stdio["timeout"] == 120 and stdio["connect_timeout"] == 15
    sse = servers["hermes-sse"]
    assert sse["url"] == _SSE_URL and sse["transport"] == "sse" and sse["timeout"] == 30
    http = servers["hermes-http"]
    assert http["url"] == _HTTP_URL and http.get("transport") != "sse"
    assert servers["user-own"] == _FOREIGN
    for key, value in _UNRELATED.items():
        assert document[key] == value

    before = _config_path(scene).read_bytes()
    scene.ok(*result.command[1:])
    assert _config_path(scene).read_bytes() == before


def test_hermes_uninstall_removes_only_apm_entries(scene: HarnessScene) -> None:
    scene.write_manifest(_DECLARATIONS, user=True)
    scene.ok("install", "--no-policy", "--target", "hermes", "--global")

    scene.write_manifest([], user=True)
    result = scene.install("hermes", user=True)

    assert result.returncode == 0, describe(result)
    document = _document(scene)
    assert document.get("mcp_servers") == {"user-own": _FOREIGN}
    for key, value in _UNRELATED.items():
        assert document[key] == value


def test_hermes_package_uninstall_removes_only_its_entries(scene: HarnessScene) -> None:
    factory = LocalPackageFactory(scene.isolated.work_root / "sources")
    package = factory.create("hermes-mcp-package", mcp_dependencies=_DECLARATIONS)
    scene.write_manifest([], user=True)
    install = ("install", "-g", "--target", "hermes", "--no-policy", "--trust-transitive-mcp")
    scene.ok(*install, str(package.root))
    servers = _document(scene)["mcp_servers"]
    assert servers["hermes-stdio"]["cwd"] == "/srv/hermes-fixture"
    assert servers["hermes-sse"]["transport"] == "sse"

    scene.ok("uninstall", "-g", str(package.root))

    document = _document(scene)
    assert document.get("mcp_servers") == {"user-own": _FOREIGN}
    for key, value in _UNRELATED.items():
        assert document[key] == value
