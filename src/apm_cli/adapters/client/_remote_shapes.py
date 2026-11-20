"""Per-harness remote MCP output shape table (single owner).

Each MCP client adapter registered in :class:`apm_cli.factory.ClientFactory`
declares its own row here and binds it in its class body
(``remote_shape = remote_shape_row("<runtime>")``), so a subclass never
silently inherits its parent's remote keys. Every row cites the vendor
evidence it was built from; rows APM has not re-verified against current
vendor docs are marked ``verified=False`` and listed in
:data:`UNVERIFIED_REMOTE_SHAPES`.

A row maps each canonical transport (see :mod:`apm_cli.mcp_transport`) to the
native URL key and, when the harness uses one, the native transport
discriminator. A transport the harness does not support maps to
:data:`UNSUPPORTED`; such a server is skipped for that harness with one
warning and is not claimed (:func:`skip_unsupported_remote`).

``retired`` lists the remote shapes earlier APM releases wrote for the same
row, so the ownership owner can recognize and heal an APM-written entry in a
retired shape (``MCPClientAdapter.legacy_managed_renders``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ...core.command_logger import library_logger
from ...mcp_transport import (
    CANONICAL_REMOTE_TRANSPORTS,
    TRANSPORT_SSE,
    TRANSPORT_STREAMABLE_HTTP,
    canonical_remote_transport,
)


class _Unsupported:
    """Sentinel marking a canonical transport a harness cannot load."""

    _instance: _Unsupported | None = None

    def __new__(cls) -> _Unsupported:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSUPPORTED"


UNSUPPORTED = _Unsupported()

# ``None`` in ``type_values`` means "emit no discriminator for this transport".
_TypeValue = str | None


class UnsupportedRemoteTransportError(ValueError):
    """Raised when a comparison render asks for a transport the harness refuses."""


def _frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class ShapeSpec:
    """Remote keys one harness reads, per canonical transport."""

    url_keys: Mapping[str, str | _Unsupported]
    type_key: str | None = None
    type_values: Mapping[str, _TypeValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "url_keys", _frozen(self.url_keys))
        object.__setattr__(self, "type_values", _frozen(self.type_values))
        if set(self.url_keys) != CANONICAL_REMOTE_TRANSPORTS:
            raise ValueError("A remote shape must state every canonical transport explicitly")
        if self.type_key is None and self.type_values:
            raise ValueError("type_values require a type_key")
        if self.type_key is not None and set(self.type_values) != set(self.supported):
            raise ValueError("type_values must cover exactly the supported transports")

    @property
    def supported(self) -> frozenset[str]:
        return frozenset(t for t, key in self.url_keys.items() if key is not UNSUPPORTED)

    def supports(self, transport: str) -> bool:
        return transport in self.supported

    @property
    def owned_keys(self) -> frozenset[str]:
        keys = {key for key in self.url_keys.values() if isinstance(key, str)}
        if self.type_key is not None:
            keys.add(self.type_key)
        return frozenset(keys)

    def render(self, transport: str, url: str) -> dict:
        """Return the native transport keys for *transport* (discriminator first)."""
        url_key = self.url_keys[transport]
        if url_key is UNSUPPORTED:
            raise UnsupportedRemoteTransportError(f"transport {transport!r} is unsupported")
        rendered: dict = {}
        if self.type_key is not None and self.type_values.get(transport) is not None:
            rendered[self.type_key] = self.type_values[transport]
        rendered[url_key] = url
        return rendered


@dataclass(frozen=True)
class RemoteShape(ShapeSpec):
    """One harness row: current shape, evidence, and earlier APM renders."""

    runtime: str = ""
    label: str = ""
    evidence_url: str = ""
    verified: bool = True
    retired: tuple[ShapeSpec, ...] = ()
    # Explicitly reuse another row's evidence; never implied by subclassing.
    inherits: str | None = None
    inherits_evidence_url: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.runtime or not self.label or not self.evidence_url:
            raise ValueError("A remote shape row needs runtime, label and evidence_url")
        if self.inherits is not None and not self.inherits_evidence_url:
            raise ValueError("An inherited remote shape needs its own evidence URL")

    @property
    def unsupported(self) -> frozenset[str]:
        return CANONICAL_REMOTE_TRANSPORTS - self.supported


_S = TRANSPORT_STREAMABLE_HTTP
_E = TRANSPORT_SSE
_TRANSPORT_DISPLAY = {_S: "Streamable HTTP", _E: "SSE"}

_COPILOT_DOCS = (
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference"
)
_ANTIGRAVITY_DOCS = "https://antigravity.google/docs/mcp"
_GEMINI_DOCS = "https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md"

# APM <= 0.36 wrote ``type: http`` for every remote on the Copilot family.
_RETIRED_TYPE_HTTP_FOR_SSE = ShapeSpec(
    url_keys={_S: "url", _E: "url"}, type_key="type", type_values={_S: "http", _E: "http"}
)
# APM <= 0.36 wrote ``httpUrl`` for registry remotes on Gemini and Antigravity
# (the v0.1 ``type`` was not read) and ``url`` for self-defined SSE.
_RETIRED_GEMINI_HTTPURL = ShapeSpec(url_keys={_S: "httpUrl", _E: "httpUrl"})
_RETIRED_GEMINI_SSE_URL = ShapeSpec(url_keys={_S: "httpUrl", _E: "url"})

REMOTE_SHAPES: Mapping[str, RemoteShape] = MappingProxyType(
    {
        row.runtime: row
        for row in (
            RemoteShape(
                runtime="copilot",
                label="Copilot CLI",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "sse"},
                evidence_url=_COPILOT_DOCS,
                retired=(_RETIRED_TYPE_HTTP_FOR_SSE,),
            ),
            RemoteShape(
                runtime="vscode",
                label="VS Code",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "sse"},
                evidence_url="https://code.visualstudio.com/docs/copilot/reference/mcp-configuration",
                retired=(
                    # Self-defined ``transport: streamable-http`` was written
                    # verbatim; registry SSE remotes were written as ``http``.
                    ShapeSpec(
                        url_keys={_S: "url", _E: "url"},
                        type_key="type",
                        type_values={_S: "streamable-http", _E: "http"},
                    ),
                ),
            ),
            RemoteShape(
                runtime="claude",
                label="Claude Code",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "sse"},
                evidence_url="https://code.claude.com/docs/en/mcp",
                retired=(_RETIRED_TYPE_HTTP_FOR_SSE,),
            ),
            RemoteShape(
                runtime="cursor",
                label="Cursor",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "http"},
                evidence_url="https://cursor.com/docs/context/mcp",
                verified=False,
            ),
            RemoteShape(
                runtime="gemini",
                label="Gemini CLI",
                url_keys={_S: "httpUrl", _E: "url"},
                evidence_url=_GEMINI_DOCS,
                retired=(_RETIRED_GEMINI_HTTPURL,),
            ),
            RemoteShape(
                runtime="antigravity",
                label="Antigravity",
                url_keys={_S: "serverUrl", _E: UNSUPPORTED},
                evidence_url=_ANTIGRAVITY_DOCS,
                retired=(_RETIRED_GEMINI_HTTPURL, _RETIRED_GEMINI_SSE_URL),
            ),
            RemoteShape(
                runtime="codex",
                label="Codex CLI",
                url_keys={_S: "url", _E: UNSUPPORTED},
                evidence_url="https://developers.openai.com/codex/mcp",
                # APM <= 0.36 wrote registry SSE remotes as Streamable HTTP.
                retired=(ShapeSpec(url_keys={_S: "url", _E: "url"}),),
            ),
            # Hermes connects over Streamable HTTP unless ``transport: sse``
            # is set (``None`` omits the key for Streamable HTTP).
            RemoteShape(
                runtime="hermes",
                label="Hermes",
                url_keys={_S: "url", _E: "url"},
                type_key="transport",
                type_values={_S: None, _E: "sse"},
                evidence_url="https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference",
                # APM <= 0.36 wrote no ``transport`` key, so SSE remotes
                # connected as Streamable HTTP (#3749).
                retired=(ShapeSpec(url_keys={_S: "url", _E: "url"}),),
            ),
            # OpenCode converts the entry to ``type: remote`` (opencode.py).
            RemoteShape(
                runtime="opencode",
                label="OpenCode",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "remote", _E: "remote"},
                evidence_url="https://opencode.ai/docs/mcp-servers/",
                verified=False,
            ),
            RemoteShape(
                runtime="kiro",
                label="Kiro",
                url_keys={_S: "url", _E: "url"},
                evidence_url="https://kiro.dev/docs/mcp/configuration/",
                verified=False,
            ),
            RemoteShape(
                runtime="windsurf",
                label="Windsurf",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "http"},
                evidence_url="https://docs.windsurf.com/windsurf/cascade/mcp",
                verified=False,
            ),
            RemoteShape(
                runtime="intellij",
                label="JetBrains Copilot",
                url_keys={_S: "url", _E: "url"},
                type_key="type",
                type_values={_S: "http", _E: "http"},
                evidence_url="https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp/extend-copilot-chat-with-mcp",
                verified=False,
            ),
        )
    }
)

# Rows whose remote keys were not re-verified against current vendor docs.
UNVERIFIED_REMOTE_SHAPES: frozenset[str] = frozenset(
    {"cursor", "intellij", "kiro", "opencode", "windsurf"}
)


def harness_label(runtime: str) -> str:
    """Product name of a runtime for user-facing output ("OpenCode", "VS Code")."""
    row = REMOTE_SHAPES.get(runtime)
    return row.label if row is not None else runtime.title()


def remote_shape_row(runtime: str) -> RemoteShape:
    """Return the row an adapter class binds in its own body."""
    return REMOTE_SHAPES[runtime]


def remote_shape_of(adapter: Any) -> RemoteShape:
    """Return the row the adapter's own class declares (never an inherited one)."""
    cls = adapter if isinstance(adapter, type) else type(adapter)
    shape = cls.__dict__.get("remote_shape")
    if not isinstance(shape, RemoteShape):
        raise TypeError(f"{cls.__name__} must declare its own remote_shape row")
    return shape


def active_remote_shape(adapter: Any) -> ShapeSpec:
    """Shape an adapter renders with now: its own row, or a retired shape while
    ``legacy_managed_renders`` reproduces an earlier APM render."""
    override = adapter._remote_shape_override
    return override if isinstance(override, ShapeSpec) else remote_shape_of(adapter)


def requested_remote_transport(adapter: Any, server_info: Mapping) -> str | None:
    """Return the canonical transport of the remote *adapter* would render, if any."""
    remote = adapter.selected_remote(server_info)
    if remote is None:
        return None
    try:
        return canonical_remote_transport(remote)
    except ValueError:
        return None  # the adapter reports an unknown transport when it renders


def unsupported_remote_transport(adapter: Any, server_info: Mapping) -> str | None:
    """Return the requested transport when *adapter*'s harness cannot load it."""
    transport = requested_remote_transport(adapter, server_info)
    if transport is None or remote_shape_of(adapter).supports(transport):
        return None
    return transport


def may_refuse_remote(runtime: str, declared_transport: str | None) -> bool:
    """Whether *runtime* may refuse this server's remote transport (no registry payload).

    ``None`` (a registry server without a declared transport) means only the
    registry payload can decide, so any runtime with an unsupported transport may.
    """
    row = REMOTE_SHAPES.get(runtime)
    if row is None or not row.unsupported:
        return False
    if declared_transport is None:
        return True
    if declared_transport not in ("http", TRANSPORT_SSE, TRANSPORT_STREAMABLE_HTTP):
        return False
    return canonical_remote_transport({"transport": declared_transport}) in row.unsupported


def unsupported_remote_message(adapter: Any, server_name: str, transport: str) -> str:
    """Wording shared by every unsupported-transport skip."""
    shape = remote_shape_of(adapter)
    supported = ", ".join(sorted(shape.supported)) or "none"
    display = _TRANSPORT_DISPLAY.get(transport, transport)
    return (
        f"Skipping MCP server '{server_name}' for {shape.label}: the {display} transport "
        f"is not supported by {shape.label} (supported remote transports: {supported}). "
        f"The server was not configured for {shape.label}. See {shape.evidence_url}"
    )


def skip_unsupported_remote(
    adapter: Any,
    server_name: str,
    transport: str,
    *,
    warn: Callable[[str], Any] | None = None,
) -> None:
    """Disposition for a transport the harness refuses: warn, skip, do not claim.

    Returns ``None`` so an adapter's ``_format_server_config`` can return it
    directly. A comparison render raises instead, so preview and ownership
    comparisons never print the warning a second time.
    """
    message = unsupported_remote_message(adapter, server_name, transport)
    if adapter._comparison_render_depth:
        raise UnsupportedRemoteTransportError(message)
    if warn is None:
        library_logger().warning(message, symbol="warning")
    else:
        warn(message)


__all__ = [
    "REMOTE_SHAPES",
    "UNSUPPORTED",
    "UNVERIFIED_REMOTE_SHAPES",
    "RemoteShape",
    "ShapeSpec",
    "UnsupportedRemoteTransportError",
    "active_remote_shape",
    "may_refuse_remote",
    "remote_shape_of",
    "remote_shape_row",
    "requested_remote_transport",
    "skip_unsupported_remote",
    "unsupported_remote_message",
    "unsupported_remote_transport",
]
