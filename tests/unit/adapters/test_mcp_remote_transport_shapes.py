"""Owners of MCP remote transport (T-norm) and per-harness remote shape (T-shape).

Covers #3736 (registry v0.1 ``remotes[].type``), #3711 part 2 (Antigravity
``serverUrl``; SSE refused) and the SSE rows of the approved shape table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client._remote_shapes import (
    REMOTE_SHAPES,
    UNVERIFIED_REMOTE_SHAPES,
    UnsupportedRemoteTransportError,
    remote_shape_of,
)
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.factory import _MCP_CLIENT_REGISTRY, ClientFactory
from apm_cli.mcp_transport import (
    CANONICAL_REMOTE_TRANSPORTS,
    TRANSPORT_SSE,
    TRANSPORT_STREAMABLE_HTTP,
    canonical_remote_transport,
)
from apm_cli.registry.client import SimpleRegistryClient

pytestmark = pytest.mark.unit

_URL = "https://mcp.example.invalid/endpoint"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ({"type": "sse"}, TRANSPORT_SSE),
        ({"type": "streamable-http"}, TRANSPORT_STREAMABLE_HTTP),
        ({"transport_type": "sse"}, TRANSPORT_SSE),
        ({"transport": "http"}, TRANSPORT_STREAMABLE_HTTP),
        ({"transport": " sse "}, TRANSPORT_SSE),
        ({}, TRANSPORT_STREAMABLE_HTTP),
        ({"type": "", "transport_type": None}, TRANSPORT_STREAMABLE_HTTP),
        ({"type": "sse", "transport_type": "http"}, TRANSPORT_SSE),
        ({"type": "streamable-http", "transport": "sse"}, TRANSPORT_STREAMABLE_HTTP),
    ],
)
def test_canonical_remote_transport_table(remote: dict, expected: str) -> None:
    assert canonical_remote_transport(remote) == expected
    assert expected in CANONICAL_REMOTE_TRANSPORTS


def test_unknown_transport_keeps_the_runtime_error_text() -> None:
    with pytest.raises(ValueError) as caught:
        canonical_remote_transport({"type": "grpc"}, runtime_label="Copilot", server_name="srv")
    assert str(caught.value) == (
        "Unsupported remote transport 'grpc' for Copilot. Server: srv. "
        "Supported transports: http, sse, streamable-http."
    )


def test_type_and_transport_type_conflict_uses_type_and_logs(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="apm_cli.mcp_transport"):
        result = canonical_remote_transport({"type": "sse", "transport_type": "streamable-http"})
    assert result == TRANSPORT_SSE
    assert "using type" in caplog.text


def test_registry_ingestion_records_the_canonical_transport_on_every_remote() -> None:
    server = {
        "name": "io.example/x",
        "remotes": [
            {"type": "sse", "url": _URL},
            {"url": _URL},
            {"transport_type": "http", "url": _URL},
            {"type": "grpc", "url": _URL},
        ],
    }
    normalized = SimpleRegistryClient._normalize_v0_1_server(server)
    assert [r.get("transport") for r in normalized["remotes"]] == [
        "sse",
        "streamable-http",
        "streamable-http",
        None,
    ]
    assert "transport" not in server["remotes"][0], "input must not be mutated"


# --- T-shape -----------------------------------------------------------------

_GOLDEN = {
    ("copilot", "streamable-http"): {"type": "http", "url": _URL},
    ("copilot", "sse"): {"type": "sse", "url": _URL},
    ("vscode", "streamable-http"): {"type": "http", "url": _URL},
    ("vscode", "sse"): {"type": "sse", "url": _URL},
    ("claude", "streamable-http"): {"type": "http", "url": _URL},
    ("claude", "sse"): {"type": "sse", "url": _URL},
    ("cursor", "streamable-http"): {"type": "http", "url": _URL},
    ("cursor", "sse"): {"type": "http", "url": _URL},
    ("gemini", "streamable-http"): {"httpUrl": _URL},
    ("gemini", "sse"): {"url": _URL},
    ("antigravity", "streamable-http"): {"serverUrl": _URL},
    ("antigravity", "sse"): None,
    ("codex", "streamable-http"): {"url": _URL},
    ("codex", "sse"): None,
    ("hermes", "streamable-http"): {"url": _URL},
    ("hermes", "sse"): {"url": _URL},
    ("opencode", "streamable-http"): {"type": "remote", "url": _URL},
    ("opencode", "sse"): {"type": "remote", "url": _URL},
    ("kiro", "streamable-http"): {"url": _URL},
    ("kiro", "sse"): {"url": _URL},
    ("windsurf", "streamable-http"): {"type": "http", "url": _URL},
    ("windsurf", "sse"): {"type": "http", "url": _URL},
    ("intellij", "streamable-http"): {"type": "http", "url": _URL},
    ("intellij", "sse"): {"type": "http", "url": _URL},
}
_URL_KEYS = ("type", "url", "httpUrl", "serverUrl", "transport")


@pytest.mark.parametrize(("runtime", "transport"), sorted(_GOLDEN))
def test_remote_render_golden(tmp_path: Path, monkeypatch, runtime: str, transport: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    adapter = ClientFactory.create_client(runtime, project_root=tmp_path)
    info = {"name": "srv", "id": "", "remotes": [{"type": transport, "url": _URL}]}
    expected = _GOLDEN[(runtime, transport)]
    if expected is None:
        with pytest.raises(UnsupportedRemoteTransportError):
            adapter.render_server_config(info)
        return
    rendered = adapter.render_server_config(info)
    assert {k: v for k, v in rendered.items() if k in _URL_KEYS} == expected


def test_every_registered_runtime_binds_its_own_row() -> None:
    assert set(REMOTE_SHAPES) == set(ClientFactory.supported_clients())
    for runtime, cls in _MCP_CLIENT_REGISTRY.items():
        assert remote_shape_of(cls) is REMOTE_SHAPES[runtime], runtime
    assert {r for r, row in REMOTE_SHAPES.items() if not row.verified} == UNVERIFIED_REMOTE_SHAPES


def test_a_subclass_without_its_own_row_cannot_render_remotes() -> None:
    class Unbound(CopilotClientAdapter):
        pass

    with pytest.raises(TypeError, match="must declare its own remote_shape"):
        remote_shape_of(Unbound)


@pytest.mark.parametrize("runtime", ["antigravity", "codex"])
def test_unsupported_transport_warns_once_and_writes_nothing(
    tmp_path: Path, monkeypatch, runtime: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    adapter = ClientFactory.create_client(runtime, project_root=tmp_path, user_scope=True)
    info = {"name": "srv", "remotes": [{"type": "sse", "url": "https://sse.example.invalid"}]}
    with patch("apm_cli.adapters.client._remote_shapes._rich_warning") as warn:
        assert adapter._format_server_config(info) is None
    warn.assert_called_once()
    message = warn.call_args[0][0]
    assert "srv" in message and "SSE" in message and remote_shape_of(adapter).label in message


@pytest.mark.parametrize(
    ("runtime", "transport", "smuggled"),
    [("antigravity", "streamable-http", "serverUrl"), ("gemini", "sse", "httpUrl")],
)
def test_extra_cannot_add_a_second_endpoint(
    tmp_path: Path, monkeypatch, runtime: str, transport: str, smuggled: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    adapter = ClientFactory.create_client(runtime, project_root=tmp_path)
    info = {
        "name": "srv",
        "remotes": [{"type": transport, "url": _URL}],
        "_extra": {"serverUrl": "https://evil.invalid", "httpUrl": "https://evil.invalid"},
    }
    rendered = adapter.render_server_config(info)
    assert "https://evil.invalid" not in rendered.values(), smuggled
