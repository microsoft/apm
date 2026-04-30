"""Pure builder for MCP ``apm.yml`` entries.

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``build_mcp_entry`` returns a tagged-union value -- a bare
string for the registry-shorthand-with-no-overlays path (preserving the
``mcp: [foo]`` ``apm.yml`` UX contract) and a dict otherwise. Callers
must dispatch with ``isinstance(entry, dict)`` or treat the result as
opaque; see #938 for the regression that motivates this rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional, Tuple, Union  # noqa: F401, UP035


def build_mcp_entry(
    name: str,
    *,
    transport: str | None,
    url: str | None,
    env: Mapping[str, str] | None,
    headers: Mapping[str, str] | None,
    version: str | None,
    command_argv: Sequence[str] | None,
    registry_url: str | None = None,
) -> tuple[str | dict[str, Any], bool]:
    """Pure builder. Return ``(entry, is_self_defined)``.

    Routing:
    - ``command_argv`` non-empty -> stdio self-defined dict.
    - ``url`` set -> remote self-defined dict (transport defaults to http).
    - else -> registry shorthand (bare string when no overlays, dict when
      ``version`` / ``transport`` / ``registry_url`` is set; the URL is
      then persisted to the entry's ``registry:`` field for reproducible
      installs). ``registry_url`` is incompatible with self-defined
      entries; the CLI layer enforces that via E15.

    Round-trips through :class:`MCPDependency.from_dict` (or
    :meth:`from_string`) for the validation chokepoint.  Validation
    failures surface as :class:`ValueError` from the model.
    """
    from ...models.dependency.mcp import MCPDependency

    if command_argv:
        # Self-defined stdio
        argv = list(command_argv)
        entry: dict[str, Any] = {
            "name": name,
            "registry": False,
            "transport": "stdio",
            "command": argv[0],
        }
        if len(argv) > 1:
            entry["args"] = argv[1:]
        if env:
            entry["env"] = dict(env)
        MCPDependency.from_dict(entry)
        return entry, True

    if url:
        # Self-defined remote
        chosen_transport = transport or "http"
        entry = {
            "name": name,
            "registry": False,
            "transport": chosen_transport,
            "url": url,
        }
        if headers:
            entry["headers"] = dict(headers)
        MCPDependency.from_dict(entry)
        return entry, True

    # Registry shorthand
    if version:
        entry = {"name": name, "version": version}
        if transport:
            entry["transport"] = transport
        if registry_url:
            entry["registry"] = registry_url
        MCPDependency.from_dict(entry)
        return entry, False

    if transport:
        entry = {"name": name, "transport": transport}
        if registry_url:
            entry["registry"] = registry_url
        MCPDependency.from_dict(entry)
        return entry, False

    if registry_url:
        # No other overlays but a custom registry URL -- promote to dict
        # form so the URL is captured in apm.yml.
        entry = {"name": name, "registry": registry_url}
        MCPDependency.from_dict(entry)
        return entry, False

    # Bare string registry shorthand -- no overlays at all.
    MCPDependency.from_string(name)
    return name, False
