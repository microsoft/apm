"""Registry-miss guidance for ``apm install --mcp`` (split from mcp_integrator_install).

Names the searched registry's own search command and the user's choices,
never endorsing an alternative registry.
"""

from __future__ import annotations

import re
from typing import Any

from apm_cli.core.remediation import MCP_REGISTRY_ALTERNATIVE

CHOOSING_MCP_REGISTRY_DOCS_URL = (
    "https://microsoft.github.io/apm/consumer/install-mcp-servers/#choosing-an-mcp-registry"
)

# Version pins are opaque strings. Only portable literal tokens may appear in
# a printed command, never shell syntax or control characters.
_SAFE_RETRY_ARG_RE = re.compile(r"[A-Za-z0-9._+,\-]+")


def _registry_choice_hint_is_safe(server_names: list[str], retry_args: tuple[str, ...]) -> bool:
    from apm_cli.registry.client import is_valid_mcp_server_name

    return all(map(is_valid_mcp_server_name, server_names)) and all(
        _SAFE_RETRY_ARG_RE.fullmatch(arg) for arg in retry_args
    )


def _emit_registry_choice_hint(
    logger: Any,
    server_names: list[str],
    *,
    registry_url: str,
    registry_source: str,
    retry_args: tuple[str, ...],
) -> None:
    """Explain a registry miss without endorsing any alternative registry.

    The searched registry is already named by the error line. ``<URL>`` stays a
    literal placeholder: the user picks the registry, APM never does.
    """
    suffix = "".join(f" {arg}" for arg in retry_args)
    for server_name in server_names:
        search = f'apm mcp search "{server_name}"'
        if registry_source == "flag":
            # ``apm mcp search`` has no --registry flag; it reads the same
            # precedence chain minus the flag.
            search = f"{search} (with MCP_REGISTRY_URL set to {registry_url})"
        logger.info(f"Search this registry: {search}", soft_wrap=True)
        logger.info(
            MCP_REGISTRY_ALTERNATIVE.bind(server=server_name, retry=suffix).sentence(),
            soft_wrap=True,
        )
    if registry_source == "env":
        logger.info("Or point MCP_REGISTRY_URL at <URL> for every lookup", soft_wrap=True)
    else:
        logger.info(
            "Or save it for every lookup: apm config set mcp-registry-url <URL>",
            soft_wrap=True,
        )
    logger.info(f"Choosing an MCP registry: {CHOOSING_MCP_REGISTRY_DOCS_URL}", soft_wrap=True)
