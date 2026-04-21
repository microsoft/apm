"""MCP dependency model."""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

_NAME_REGEX = re.compile(r"^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$")
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


@dataclass
class MCPDependency:
    """Represents an MCP server dependency with optional overlay configuration.

    Supports three forms:
    - String (registry reference): MCPDependency.from_string("io.github.github/github-mcp-server")
    - Object with overlays: MCPDependency.from_dict({"name": "...", "transport": "stdio", ...})
    - Self-defined (registry: false): MCPDependency.from_dict({"name": "...", "registry": False, "transport": "http", "url": "..."})
    """
    name: str
    transport: Optional[str] = None          # "stdio" | "sse" | "streamable-http" | "http"
    env: Optional[Dict[str, str]] = None     # Environment variable overrides
    args: Optional[Any] = None               # Dict for overlay variable overrides, List for self-defined positional args
    version: Optional[str] = None            # Pin specific server version
    registry: Optional[Any] = None           # None=default, False=self-defined, str=custom registry URL
    package: Optional[str] = None            # "npm" | "pypi" | "oci" — select package type
    headers: Optional[Dict[str, str]] = None # Custom HTTP headers for remote endpoints
    tools: Optional[List[str]] = None        # Restrict exposed tools (default is ["*"])
    url: Optional[str] = None                # Required for self-defined http/sse transports
    command: Optional[str] = None            # Required for self-defined stdio transports

    @classmethod
    def from_string(cls, s: str) -> "MCPDependency":
        """Create an MCPDependency from a plain string (registry reference)."""
        instance = cls(name=s)
        instance.validate(strict=False)
        return instance

    @classmethod
    def from_dict(cls, d: dict) -> "MCPDependency":
        """Parse an MCPDependency from a dict.

        Handles backward compatibility: 'type' key is mapped to 'transport'.
        Unknown keys are silently ignored for forward compatibility.
        """
        if 'name' not in d:
            raise ValueError("MCP dependency dict must contain 'name'")

        transport = d.get('transport') or d.get('type')  # legacy 'type' -> 'transport'

        instance = cls(
            name=d['name'],
            transport=transport,
            env=d.get('env'),
            args=d.get('args'),
            version=d.get('version'),
            registry=d.get('registry'),
            package=d.get('package'),
            headers=d.get('headers'),
            tools=d.get('tools'),
            url=d.get('url'),
            command=d.get('command'),
        )

        if instance.registry is False:
            instance.validate(strict=True)
        else:
            instance.validate(strict=False)

        return instance

    @property
    def is_registry_resolved(self) -> bool:
        """True when the dependency is resolved via a registry."""
        return self.registry is not False

    @property
    def is_self_defined(self) -> bool:
        """True when the dependency is self-defined (registry: false)."""
        return self.registry is False

    def to_dict(self) -> dict:
        """Serialize to dict, including only non-None fields."""
        result: Dict[str, Any] = {'name': self.name}
        for field_name in ('transport', 'env', 'args', 'version', 'registry',
                           'package', 'headers', 'tools', 'url', 'command'):
            value = getattr(self, field_name)
            if value is not None or (field_name == 'registry' and value is False):
                result[field_name] = value
        return result

    _VALID_TRANSPORTS = frozenset({"stdio", "sse", "http", "streamable-http"})

    def __str__(self) -> str:
        """Return a redacted, human-friendly identifier for logging and CLI output."""
        if self.transport:
            return f"{self.name} ({self.transport})"
        return self.name

    def __repr__(self) -> str:
        """Return a redacted representation to keep secrets out of debug logs."""
        parts = [f"name={self.name!r}"]
        if self.transport:
            parts.append(f"transport={self.transport!r}")
        if self.env:
            safe_env = {k: '***' for k in self.env}
            parts.append(f"env={safe_env}")
        if self.headers:
            safe_headers = {k: '***' for k in self.headers}
            parts.append(f"headers={safe_headers}")
        if self.args is not None:
            parts.append("args=...")
        if self.tools:
            parts.append(f"tools={self.tools!r}")
        if self.url:
            parts.append(f"url={self.url!r}")
        if self.command:
            parts.append(f"command={self.command!r}")
        return f"MCPDependency({', '.join(parts)})"

    def validate(self, strict: bool = True) -> None:
        """Validate the dependency. Raises ValueError on invalid state.

        Universal hardening checks (name allowlist, URL scheme, header CRLF,
        command path-traversal) always run. Self-defined-only checks
        (transport required, stdio command-required, http/sse url required)
        run only when ``strict=True``.
        """
        # ---- Universal hardening (always) ----
        if not self.name:
            raise ValueError("MCP dependency 'name' must not be empty")
        if not _NAME_REGEX.match(self.name):
            raise ValueError(
                f"Invalid MCP dependency name '{self.name}': "
                f"must start with a letter, digit, '@', or '_' and contain "
                f"only [a-zA-Z0-9._@/:=-] (max 128 chars). "
                f"Example: 'io.github.acme/cool-server' or 'my-server'."
            )
        # C2 (defense-in-depth): reject embedded ``..`` segments. The regex
        # above allows ``a/../../../evil`` because '/', '.', '-' are all in
        # the character class. Today no code path uses this name as a
        # filesystem segment, but downstream consumers should be able to
        # trust the name string.
        if ".." in self.name.split("/"):
            raise ValueError(
                f"Invalid MCP dependency name '{self.name}': must not contain "
                f"'..' path segments. "
                f"Example: 'io.github.acme/cool-server' or 'my-server'."
            )
        if self.url is not None:
            scheme = urlparse(self.url).scheme.lower()
            if scheme not in _ALLOWED_URL_SCHEMES:
                raise ValueError(
                    f"Invalid MCP url '{self.url}': scheme '{scheme}' "
                    f"is not supported; use http:// or https://. "
                    f"WebSocket URLs (ws/wss) are not supported for MCP transports."
                )
        if self.headers:
            for k, v in self.headers.items():
                k_str = str(k) if k is not None else ""
                v_str = str(v) if v is not None else ""
                if "\r" in k_str or "\n" in k_str or "\r" in v_str or "\n" in v_str:
                    raise ValueError(
                        f"Invalid header '{k_str}={v_str}': control characters "
                        f"(CR/LF) not allowed in keys or values"
                    )
        if self.command is not None:
            try:
                validate_path_segments(
                    self.command,
                    context="MCP command",
                    allow_current_dir=True,
                )
            except PathTraversalError:
                raise ValueError(
                    f"Invalid MCP command '{self.command}': must not contain "
                    f"'..' path segments. Use an absolute path or a command "
                    f"name on PATH instead."
                ) from None

        if not strict:
            return

        # ---- Self-defined-only checks (strict=True) ----
        if self.transport and self.transport not in self._VALID_TRANSPORTS:
            raise ValueError(
                f"MCP dependency '{self.name}' has unsupported transport "
                f"'{self.transport}'. Valid values: {', '.join(sorted(self._VALID_TRANSPORTS))}"
            )
        if self.registry is False:
            if not self.transport:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' requires 'transport'"
                )
            if self.transport in ('http', 'sse', 'streamable-http') and not self.url:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' with transport "
                    f"'{self.transport}' requires 'url'"
                )
            if self.transport == 'stdio' and not self.command:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' with transport "
                    f"'stdio' requires 'command'"
                )
