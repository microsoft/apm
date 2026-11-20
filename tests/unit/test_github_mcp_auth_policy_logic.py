"""Shared auth policy across native runtime and literal-only MCP consumers (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import ManifestHeaderValue, MCPDependency

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-mcp-passthrough-denylist")]


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
