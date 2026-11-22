"""The MCP collision preflight never projects a render the install may prompt for (#4741)."""

from __future__ import annotations

import pytest

from apm_cli.install.mcp.collision_preflight import _needs_input

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-commit-order")]


@pytest.mark.parametrize(
    ("info", "prompts"),
    [
        ({"remotes": [{"type": "streamable-http", "url": "https://x.invalid/mcp"}]}, False),
        (
            {
                "remotes": [
                    {
                        "type": "streamable-http",
                        "url": "https://x.invalid/mcp",
                        "headers": [{"name": "X-Custom", "value": "${MISSING_VALUE}"}],
                    }
                ]
            },
            True,
        ),
        ({"remotes": [{"type": "sse", "url": "https://${HOST}/mcp"}]}, True),
        ({"docker": {"args": ["run", "${TOKEN}"]}}, True),
        ({"packages": [{"environment_variables": [{"name": "TOKEN"}]}]}, True),
        ({"packages": [{"runtime_arguments": [{"variables": {"org": {}}}]}]}, True),
        ({"packages": [{"registry_name": "npm", "name": "srv"}]}, False),
    ],
    ids=[
        "plain-remote",
        "header-placeholder",
        "url-placeholder",
        "docker-placeholder",
        "package-env",
        "runtime-variable",
        "plain-package",
    ],
)
def test_a_render_with_unresolved_inputs_is_left_to_the_install(info: dict, prompts: bool) -> None:
    assert _needs_input(info) is prompts
