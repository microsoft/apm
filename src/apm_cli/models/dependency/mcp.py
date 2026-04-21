"""MCP dependency model."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


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
        return cls(name=s)

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
            instance.validate()

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

    def validate(self) -> None:
        """Validate the dependency. Raises ValueError on invalid state."""
        if not self.name:
            raise ValueError("MCP dependency 'name' must not be empty")
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
            if (
                self.transport == 'stdio'
                and isinstance(self.command, str)
                and any(ch.isspace() for ch in self.command)
                and not self.args
            ):
                first, _, rest = self.command.strip().partition(' ')
                rest_tokens = rest.split() if rest else []
                suggested_args = '[' + ', '.join(f'"{tok}"' for tok in rest_tokens) + ']'
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}': "
                    f"'command' must be a single binary path, not a shell line. "
                    f"APM does not split 'command' on whitespace. "
                    f"Got: command={self.command!r}. "
                    f"Did you mean: command: {first}, args: {suggested_args} ? "
                    f"See https://microsoft.github.io/apm/guides/mcp-servers/ "
                    f"for the canonical stdio shape."
                )
