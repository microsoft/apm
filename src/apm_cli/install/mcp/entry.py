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
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _MCPEntryOpts:
    transport: str | None = None
    url: str | None = None
    env: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    version: str | None = None
    command_argv: Sequence[str] | None = None
    registry_url: str | None = None


def _build_stdio_entry(name: str, argv: list[str], env: Mapping[str, str] | None) -> dict[str, Any]:
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
    return entry


def _build_remote_entry(
    name: str,
    transport: str | None,
    url: str,
    headers: Mapping[str, str] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "registry": False,
        "transport": transport or "http",
        "url": url,
    }
    if headers:
        entry["headers"] = dict(headers)
    return entry


def _build_registry_entry(name: str, opts: _MCPEntryOpts) -> str | dict[str, Any]:
    if opts.version:
        entry: dict[str, Any] = {"name": name, "version": opts.version}
        if opts.transport:
            entry["transport"] = opts.transport
        if opts.registry_url:
            entry["registry"] = opts.registry_url
        return entry
    if opts.transport:
        entry = {"name": name, "transport": opts.transport}
        if opts.registry_url:
            entry["registry"] = opts.registry_url
        return entry
    if opts.registry_url:
        return {"name": name, "registry": opts.registry_url}
    return name


def build_mcp_entry(
    name: str,
    *,
    opts: _MCPEntryOpts | None = None,
    **kwargs: Any,
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

    if opts is None:
        opts = _MCPEntryOpts(**kwargs)

    if opts.command_argv:
        entry = _build_stdio_entry(name, list(opts.command_argv), opts.env)
        MCPDependency.from_dict(entry)
        return entry, True

    if opts.url:
        entry = _build_remote_entry(name, opts.transport, opts.url, opts.headers)
        MCPDependency.from_dict(entry)
        return entry, True

    entry = _build_registry_entry(name, opts)
    if isinstance(entry, dict):
        MCPDependency.from_dict(entry)
    else:
        MCPDependency.from_string(name)
    return entry, False


# Backward-compatibility alias for tests and legacy callers that imported
# the underscore-prefixed name from apm_cli.commands.install.
_build_mcp_entry = build_mcp_entry
