"""Shared auth policy across native runtime and literal-only MCP consumers."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.adapters.client.intellij import IntelliJClientAdapter
from apm_cli.adapters.client.windsurf import WindsurfClientAdapter
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import ManifestHeaderValue, MCPDependency

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    "adapter_class,automatic",
    [
        (CopilotClientAdapter, "Bearer ${GITHUB_TOKEN}"),
        (IntelliJClientAdapter, "Bearer ${env:GITHUB_TOKEN}"),
        (WindsurfClientAdapter, "Bearer ambient-sentinel"),
    ],
)
@pytest.mark.parametrize(
    "manifest_headers",
    [
        {},
        {"authorization": None},
        {"authorization": False},
        {"authorization": 0},
        {"authorization": ""},
        {"authorization": "static-auth"},
        {"authorization": "${env:USER_PAT}"},
    ],
)
@pytest.mark.parametrize("source", ["registry", "self-defined"])
def test_shared_policy_preserves_registry_fallback_and_manifest_precedence(
    tmp_path: Path,
    adapter_class: type[CopilotClientAdapter],
    automatic: str,
    manifest_headers: dict[str, str | bool | int | None],
    source: str,
) -> None:
    """Real model/overlay/formatter consumers agree on the single header winner."""
    declaration = {"name": "github-mcp-server", "headers": manifest_headers}
    if source == "self-defined":
        declaration.update(
            registry=False,
            transport="http",
            url="https://api.githubcopilot.com/mcp/",
            headers={**manifest_headers, "X-Other": "preserved"},
        )
    dep = MCPDependency.from_dict(declaration)
    remote = {
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": [
            {"name": "Authorization", "value": "registry-default"},
            {"name": "X-Other", "value": "preserved"},
        ],
    }
    if source == "self-defined":
        info = MCPIntegrator._build_self_defined_info(dep)
    else:
        info = {"name": dep.name, "remotes": [remote]}
        MCPIntegrator._apply_overlay({dep.name: info}, dep)
    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "APM_HOME": str(tmp_path / ".apm"),
            "GITHUB_TOKEN": "ambient-sentinel",
            "USER_PAT": "user-sentinel",
        },
        clear=True,
    ):
        config = adapter_class()._format_server_config(info)
    expected = automatic
    explicit = manifest_headers.get("authorization")
    if explicit == "static-auth":
        expected = explicit
    elif explicit:
        expected = {
            CopilotClientAdapter: "${USER_PAT}",
            IntelliJClientAdapter: "${env:USER_PAT}",
            WindsurfClientAdapter: "user-sentinel",
        }[adapter_class]
    assert [
        value for name, value in config["headers"].items() if name.casefold() == "authorization"
    ] == [expected]
    assert config["headers"]["X-Other"] == "preserved"


@pytest.mark.parametrize("value", [None, False, 0, "", "static-auth"])
def test_dictionary_overlay_tags_strings_without_coercing_values(
    value: str | bool | int | None,
) -> None:
    """Dictionary-shaped registry headers retain the authored value and type."""
    dep = MCPDependency.from_dict({"name": "github", "headers": {"Authorization": value}})
    info = {"remotes": [{"headers": {"X-Other": "preserved"}}]}
    MCPIntegrator._apply_overlay({dep.name: info}, dep)
    headers = info["remotes"][0]["headers"]
    assert headers["Authorization"] == value
    assert isinstance(headers["Authorization"], ManifestHeaderValue) == isinstance(value, str)
    assert headers["X-Other"] == "preserved"


@pytest.mark.parametrize("adapter_class", [CopilotClientAdapter, CursorClientAdapter])
@pytest.mark.parametrize("header_shape", ["list", "dict"])
@pytest.mark.parametrize(
    "manifest_headers",
    [
        {},
        {"authorization": None},
        {"authorization": False},
        {"authorization": 0},
        {"authorization": ""},
        {"AUTHORIZATION": "authored-static"},
        {"authorization": "${env:USER_PAT}"},
    ],
)
@pytest.mark.parametrize("ambient_token", ["", "ambient-sentinel"])
def test_registry_header_shapes_reach_shared_formatter(
    tmp_path: Path,
    adapter_class: type[CopilotClientAdapter],
    header_shape: str,
    manifest_headers: dict[str, str | bool | int | None],
    ambient_token: str,
) -> None:
    """Accepted registry mappings retain provenance through real rendering."""
    registry_headers = {"Authorization": "registry-default", "X-Other": "preserved"}
    remote = {
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": registry_headers
        if header_shape == "dict"
        else [{"name": name, "value": value} for name, value in registry_headers.items()],
    }
    dep = MCPDependency.from_dict({"name": "github-mcp-server", "headers": manifest_headers})
    info = {"name": dep.name, "remotes": [remote]}
    MCPIntegrator._apply_overlay({dep.name: info}, dep)
    before = deepcopy(info)
    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "GITHUB_TOKEN": ambient_token,
            "USER_PAT": "authored-token-sentinel",
        },
        clear=True,
    ):
        config = adapter_class()._format_server_config(info)
    native = adapter_class._supports_runtime_env_substitution
    explicit = next(iter(manifest_headers.values()), None)
    if explicit == "authored-static":
        expected = explicit
    elif explicit:
        expected = (
            ("${USER_PAT}" if adapter_class is CopilotClientAdapter else "${env:USER_PAT}")
            if native
            else "authored-token-sentinel"
        )
    elif ambient_token:
        expected = (
            (
                "Bearer ${GITHUB_TOKEN}"
                if adapter_class is CopilotClientAdapter
                else "Bearer ${env:GITHUB_TOKEN}"
            )
            if native
            else f"Bearer {ambient_token}"
        )
    else:
        expected = "registry-default"
    assert [
        value for name, value in config["headers"].items() if name.casefold() == "authorization"
    ] == [expected]
    assert config["headers"]["X-Other"] == "preserved"
    assert info == before
    if header_shape == "dict":
        for name, value in remote["headers"].items():
            assert type(value) is type(before["remotes"][0]["headers"][name])


@pytest.mark.parametrize(
    "name,url",
    [
        ("other-server", "https://api.githubcopilot.com/mcp/"),
        ("github", "https://github.com.evil.invalid/mcp/"),
        ("github", "http://api.github.com/mcp/"),
    ],
)
def test_unadmitted_servers_do_not_consult_auth_authority(
    tmp_path: Path, name: str, url: str
) -> None:
    """Both the server-name and secure-host restrictions precede credential selection."""
    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch("apm_cli.core.auth.AuthResolver") as resolver,
    ):
        config = CopilotClientAdapter()._format_server_config(
            {"name": name, "remotes": [{"url": url}]}
        )
    assert config.get("headers", {}) == {}
    resolver.assert_not_called()
