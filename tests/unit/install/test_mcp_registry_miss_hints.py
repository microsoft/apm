"""Registry-miss hints for MCP servers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

from apm_cli.integration.mcp_integrator_install import (
    CHOOSING_MCP_REGISTRY_DOCS_URL,
    _validate_registry_servers,
)
from apm_cli.registry.client import SimpleRegistryClient, is_valid_mcp_server_name
from apm_cli.registry.operations import MCPServerOperations

pytestmark = pytest.mark.unit

# A second populated registry the user picks by hand when replaying the hint.
DOCS_HOST = urlparse(CHOOSING_MCP_REGISTRY_DOCS_URL).hostname


def _printed_hosts(output: str) -> set[str]:
    """Hostnames of every URL in CLI output, parsed rather than substring-matched."""
    return {
        urlparse(token.strip("(),.;'\"")).hostname for token in output.split() if "://" in token
    }


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


def _validate_missing(operations: MagicMock, server_names: list[str], **kwargs) -> MagicMock:
    logger = MagicMock()
    with pytest.raises(RuntimeError, match=f"Cannot install {len(server_names)} missing server"):
        _validate_registry_servers(
            operations,
            server_names,
            dependency_count=len(server_names),
            verbose=False,
            logger=logger,
            **kwargs,
        )
    return logger


def test_default_registry_miss_hint_is_ordered_and_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each missing name gets a search and a placeholder example, in order."""
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

    logger = _validate_missing(
        operations,
        server_names,
        suggest_registry_choice=True,
        retry_args=("--target", "claude"),
    )

    expected = []
    for name in server_names:
        expected.append(f'Search this registry: apm mcp search "{name}"')
        expected.append(
            "Use another registry (replace <URL>): "
            f'apm install --mcp "{name}" --registry <URL> --target claude'
        )
    expected.append("Or save it for every lookup: apm config set mcp-registry-url <URL>")
    expected.append(f"Choosing an MCP registry: {CHOOSING_MCP_REGISTRY_DOCS_URL}")
    assert [call.args[0] for call in logger.info.call_args_list] == expected
    assert all(call.kwargs == {"soft_wrap": True} for call in logger.info.call_args_list)
    assert all(is_valid_mcp_server_name(server_name) for server_name in server_names)


@pytest.mark.parametrize(
    "server_name",
    ["bad;name", 'bad"name', "bad\nname", "valid-looking\n"],
)
def test_registry_choice_hint_rejects_unsafe_server_names(server_name: str) -> None:
    """Unvalidated names never cross into a pasted shell command."""
    assert not is_valid_mcp_server_name(server_name)
    with pytest.raises(ValueError, match="Invalid server name"):
        SimpleRegistryClient("https://api.mcp.github.com").get_server(server_name)
    operations = _missing_operations([server_name], "default", "https://api.mcp.github.com")

    logger = _validate_missing(operations, [server_name], suggest_registry_choice=True)

    logger.info.assert_not_called()
    logger.progress.assert_called_once_with(
        "Run 'apm mcp search <query>' to find available servers"
    )


def test_flag_registry_hint_never_echoes_credentials() -> None:
    """The searched-registry line uses the redacted URL only."""
    registry_url = "https://user:super-secret@private.example/api"
    operations = _missing_operations(["io.example.weather/alerts"], "flag", registry_url)

    logger = _validate_missing(
        operations, ["io.example.weather/alerts"], suggest_registry_choice=True
    )

    output = " ".join(
        call.args[0]
        for method in (logger.error, logger.progress, logger.info)
        for call in method.call_args_list
    )
    assert _printed_hosts(output) == {"private.example", DOCS_HOST}
    assert "super-secret" not in output
    assert "(with MCP_REGISTRY_URL set to https://private.example/api)" in output


@pytest.mark.parametrize("source", ["explicit", None])
def test_manifest_registry_miss_keeps_existing_guidance(source: str | None) -> None:
    """Sources a direct install cannot produce never get the choice hint."""
    registry_url = "https://user:super-secret@private.example/api"
    operations = _missing_operations(["io.example.weather/alerts"], source, registry_url)

    logger = _validate_missing(
        operations, ["io.example.weather/alerts"], suggest_registry_choice=True
    )

    output = " ".join(
        call.args[0]
        for method in (logger.error, logger.progress, logger.info)
        for call in method.call_args_list
    )
    assert _printed_hosts(output) == {"private.example"}
    assert "<URL>" not in output
    assert "super-secret" not in output
    logger.info.assert_not_called()
