"""Ownership comparison heals APM-written MCP entries in a retired remote shape.

Decision A1 (#3711, #3736): an entry APM wrote in a shape an earlier release
used is UPDATE_MANAGED even when the declaration is unchanged; a foreign entry
stays a CONFLICT, and an environment-only difference on a non-refreshing
adapter does not churn.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.factory import ClientFactory
from apm_cli.install.mcp.ownership import (
    NativeMcpConnectionState,
    compare_native_mcp_connection,
    mcp_native_hash,
)

pytestmark = pytest.mark.unit

_URL = "https://heal.example.invalid/sse"
_REGISTRY = {"name": "io.example/heal"}
_INFO = {"name": "io.example/heal", "id": "", "remotes": [{"type": "sse", "url": _URL}]}
_CURRENT = {"type": "sse", "url": _URL, "tools": ["*"], "id": ""}
_RETIRED = {"type": "http", "url": _URL, "tools": ["*"], "id": ""}


def _compare(tmp_path: Path, native: dict, *, managed: bool, fingerprint: str | None):
    client = ClientFactory.create_client("copilot", project_root=tmp_path)
    return compare_native_mcp_connection(
        client,
        "io.example/heal",
        requested_info=_INFO,
        stored_config=_REGISTRY,
        managed=managed,
        existing_configs={"heal": native},
        native_hash=fingerprint,
        registry_request=True,
        declaration_changed=False,
    )


@pytest.mark.parametrize(
    ("native", "managed", "fingerprinted", "expected"),
    [
        (_RETIRED, True, True, NativeMcpConnectionState.UPDATE_MANAGED),
        (_RETIRED, True, False, NativeMcpConnectionState.UPDATE_MANAGED),
        (_RETIRED, False, False, NativeMcpConnectionState.CONFLICT),
        (_CURRENT, True, True, NativeMcpConnectionState.MATCHING_MANAGED),
        (
            {**_CURRENT, "headers": {"Authorization": "Bearer earlier"}},
            True,
            True,
            NativeMcpConnectionState.MATCHING_MANAGED,
        ),
        (
            {**_RETIRED, "url": "https://other.invalid"},
            True,
            False,
            NativeMcpConnectionState.CONFLICT,
        ),
        (_RETIRED, True, "mismatch", NativeMcpConnectionState.CONFLICT),
    ],
    ids=[
        "fingerprint-retired",
        "legacy-render-retired",
        "foreign-retired",
        "current",
        "env-only-no-churn",
        "unrecognized",
        "edited-after-write-keeps-user-shape",
    ],
)
def test_retired_shape_heal_matrix(
    tmp_path: Path, native: dict, managed: bool, fingerprinted, expected
) -> None:
    if fingerprinted == "mismatch":
        fingerprint = mcp_native_hash(_CURRENT)
    else:
        fingerprint = mcp_native_hash(native) if fingerprinted else None
    assert _compare(tmp_path, native, managed=managed, fingerprint=fingerprint) is expected


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        (_CURRENT, NativeMcpConnectionState.UPDATE_MANAGED),
        ({**_CURRENT, "headers": {"X-Edit": "user"}}, NativeMcpConnectionState.CONFLICT),
        ({**_CURRENT, "type": "streamable-http"}, NativeMcpConnectionState.CONFLICT),
    ],
    ids=["frozen-healed", "edited-after-heal", "user-reshaped"],
)
def test_frozen_heal_keeps_the_retired_receipt_verifiable(
    tmp_path: Path, native: dict, expected: NativeMcpConnectionState
) -> None:
    """--frozen heals without writing the lock, so the receipt still fingerprints _RETIRED."""
    fingerprint = mcp_native_hash(_RETIRED)
    assert _compare(tmp_path, native, managed=True, fingerprint=fingerprint) is expected


def test_self_defined_vscode_streamable_alias_heals_without_fingerprint(tmp_path: Path) -> None:
    client = ClientFactory.create_client("vscode", project_root=tmp_path)
    stored = {"name": "sd", "registry": False, "transport": "streamable-http", "url": _URL}
    native = {"type": "streamable-http", "url": _URL, "headers": {}}
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    info = MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(stored))
    state = compare_native_mcp_connection(
        client,
        "sd",
        requested_info=info,
        stored_config=stored,
        managed=True,
        existing_configs={"sd": native},
        declaration_changed=False,
    )
    assert state is NativeMcpConnectionState.UPDATE_MANAGED


def test_removal_accepts_a_retired_self_defined_entry(tmp_path: Path) -> None:
    from apm_cli.install.mcp.ownership import require_removable_mcp_connections

    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"sd": {"httpUrl": _URL}}}), encoding="utf-8"
    )
    client = ClientFactory.create_client("antigravity", project_root=tmp_path)
    stored = {"name": "sd", "registry": False, "transport": "http", "url": _URL}
    assert require_removable_mcp_connections(client, {"sd"}, {"sd": stored}) == {"sd"}


def _antigravity_with(tmp_path: Path, servers: dict) -> Path:
    (tmp_path / ".agents").mkdir(exist_ok=True)
    path = tmp_path / ".agents" / "mcp_config.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def _skip(tmp_path: Path, claims: dict, hashes: dict, logger) -> bool:
    from apm_cli.integration.mcp_integrator_install import _skip_unsupported_remote_transport

    return _skip_unsupported_remote_transport(
        "antigravity",
        "io.example/heal",
        dict(_INFO),
        logger,
        project_root=tmp_path,
        user_scope=False,
        stored_config=_REGISTRY,
        managed_target_servers=claims,
        native_hashes=hashes,
        registry_info=True,
    )


def test_unsupported_skip_removes_only_the_verified_apm_key(tmp_path: Path) -> None:
    mine = {"url": "https://mine.invalid"}
    path = _antigravity_with(tmp_path, {"heal": {"httpUrl": _URL}, "io.example/heal": mine})
    claims = {"antigravity": {"io.example/heal"}}
    logger = MagicMock()

    assert _skip(tmp_path, claims, {}, logger)

    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"] == {"io.example/heal": mine}
    assert claims == {"antigravity": set()}
    logger.warning.assert_called_once()


def test_unsupported_skip_keeps_an_entry_edited_after_apm_wrote_it(tmp_path: Path) -> None:
    path = _antigravity_with(tmp_path, {"heal": {"httpUrl": _URL}})
    before = path.read_bytes()
    claims = {"antigravity": {"io.example/heal"}}
    hashes = {"antigravity": {"io.example/heal": mcp_native_hash({"httpUrl": "https://old"})}}

    assert _skip(tmp_path, claims, hashes, MagicMock())

    assert path.read_bytes() == before
    assert claims == {"antigravity": set()}


@pytest.mark.windows_compat
def test_unsupported_skip_keeps_the_claim_when_removal_fails(tmp_path: Path) -> None:
    from apm_cli.install.errors import RequiredIntegrationError

    path = _antigravity_with(tmp_path, {"heal": {"httpUrl": _URL}})
    claims = {"antigravity": {"io.example/heal"}}
    # Fail the exact write the cleanup owner performs; permission bits are not
    # enforced the same way on every platform (Windows ignores POSIX modes).
    with (
        patch(
            "apm_cli.integration.mcp_integrator.atomic_write_text",
            side_effect=OSError("simulated write failure"),
        ) as write,
        pytest.raises(RequiredIntegrationError),
    ):
        _skip(tmp_path, claims, {}, MagicMock())
    write.assert_called_once()

    assert claims == {"antigravity": {"io.example/heal"}}
    assert "heal" in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
