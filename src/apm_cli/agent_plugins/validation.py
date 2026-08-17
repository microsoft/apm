"""Strict local validation helpers for Agent Plugins v1.0.0."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .constants import (
    AGENT_PLUGINS_SCHEMA_PREFIX,
    AGENT_PLUGINS_VERSION,
    COM_MICROSOFT_APM_NAMESPACE,
    COM_MICROSOFT_APM_SCHEMA_VERSION,
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    SUPPORTED_MCP_SCHEMA_IDS,
    SUPPORTED_PLUGIN_SCHEMA_IDS,
)
from .io import read_json_document

_PLUGIN_NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BARE_COMMAND_RE = re.compile(r"^[^\s/\\]+$")
_PLUGIN_RELATIVE_CWD_RE = re.compile(
    r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$)).*"
)
_RESERVED_ENV_NAMES = frozenset({"PLUGIN_ROOT", "PLUGIN_DATA"})
_PLUGIN_TOP_LEVEL_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_MCP_TOP_LEVEL_FIELDS = frozenset({"$schema", "mcpServers"})
_MCP_SERVER_TYPES = frozenset({"stdio", "streamable-http", "sse"})
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|api[-_]?key|access[-_]?key|token|secret|password|credential|"
    r"cookie|private[-_]?key|database[-_]?url)",
    re.IGNORECASE,
)
_VARIABLE_VALUE_RE = re.compile(r"^\$\{[^{}]+\}$")
_VARIABLE_REFERENCE_RE = re.compile(r"\$\{([^{}]+)\}")
_PORTABLE_VARIABLE_NAMES = frozenset({"PLUGIN_ROOT", "PLUGIN_DATA"})
_SECRET_ARG_RE = re.compile(
    r"^--?(?:authorization|api[-_]?key|access[-_]?key|token|secret|password|"
    r"credential|cookie|private[-_]?key|database[-_]?url)(?:=(.*))?$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ValidationResult:
    """Validation outcome for a local Agent Plugins document."""

    schema_id: str | None = None
    normalized: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        """Record a fatal validation error."""
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal validation warning."""
        self.warnings.append(message)

    @property
    def is_valid(self) -> bool:
        """Return True when no fatal errors were recorded."""
        return not self.errors


def is_valid_plugin_name(name: str) -> bool:
    """Return True when *name* satisfies Agent Plugins naming rules.

    Enforces length bounds and the canonical _PLUGIN_NAME_RE so callers
    outside the agent-plugins package can reuse a single source of truth
    for name validation.
    """
    if not isinstance(name, str):
        return False
    if not (1 <= len(name) <= 64):
        return False
    return bool(_PLUGIN_NAME_RE.fullmatch(name))


def supports_plugin_schema_id(schema_id: str) -> bool:
    """Return True when *schema_id* is a locally supported plugin schema."""
    return schema_id in SUPPORTED_PLUGIN_SCHEMA_IDS


def supports_mcp_schema_id(schema_id: str) -> bool:
    """Return True when *schema_id* is a locally supported MCP schema."""
    return schema_id in SUPPORTED_MCP_SCHEMA_IDS


def schema_version_from_id(schema_id: str) -> str | None:
    """Return the Agent Plugins version encoded by *schema_id*."""
    if schema_id in (PLUGIN_SCHEMA_ID, MCP_SCHEMA_ID):
        return AGENT_PLUGINS_VERSION
    return None


def is_agent_plugin_schema_id(schema_id: str) -> bool:
    """Return True when *schema_id* is an Agent Plugins schema family member."""
    return schema_id.startswith(AGENT_PLUGINS_SCHEMA_PREFIX)


def contains_literal_secret_fields(values: object) -> bool:
    """Return whether a secret-shaped mapping field has a literal value."""
    if not isinstance(values, dict):
        return False
    return any(
        _SECRET_KEY_RE.search(str(key))
        and (not isinstance(value, str) or not _VARIABLE_VALUE_RE.fullmatch(value))
        for key, value in values.items()
    )


def args_contain_literal_secret(values: object) -> bool:
    """Return whether command arguments embed a literal credential."""
    if not isinstance(values, list):
        return False
    for index, value in enumerate(values):
        if not isinstance(value, str):
            continue
        match = _SECRET_ARG_RE.fullmatch(value)
        if match is None:
            if re.search(r"\bbearer\s+(?!\$\{)[^\s]+", value, re.IGNORECASE):
                return True
            continue
        inline_value = match.group(1)
        if inline_value is not None:
            if not _VARIABLE_VALUE_RE.fullmatch(inline_value):
                return True
            continue
        if index + 1 >= len(values):
            return True
        next_value = values[index + 1]
        if not isinstance(next_value, str) or not _VARIABLE_VALUE_RE.fullmatch(next_value):
            return True
    return False


def url_contains_literal_secret(value: object) -> bool:
    """Return whether a URL embeds credentials or secret query values."""
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        return True
    return any(
        _SECRET_KEY_RE.search(key) and not _VARIABLE_VALUE_RE.fullmatch(query_value)
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
    )


def _agent_plugin_contains_secret_fields(values: object) -> bool:
    """Return whether an Agent Plugin mapping declares a secret-shaped field."""
    return isinstance(values, dict) and any(_SECRET_KEY_RE.search(str(key)) for key in values)


def _agent_plugin_args_contain_secret(values: object) -> bool:
    """Return whether Agent Plugin argv appears to carry a credential."""
    if not isinstance(values, list):
        return False
    for value in values:
        if not isinstance(value, str):
            continue
        if _SECRET_ARG_RE.fullmatch(value) is not None:
            return True
        if _SECRET_KEY_RE.search(value):
            return True
        if re.search(r"\bbearer\s+[^\s]+", value, re.IGNORECASE):
            return True
    return False


def _agent_plugin_url_contains_secret(value: object) -> bool:
    """Return whether a literal Agent Plugin URL carries credential material."""
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return bool(
        parsed.username
        or parsed.password
        or any(_SECRET_KEY_RE.search(key) for key, _ in parse_qsl(parsed.query))
    )


def _contains_unsupported_plugin_variable(value: str) -> bool:
    """Return whether a value references a non-portable variable name."""
    return any(
        match.group(1) not in _PORTABLE_VARIABLE_NAMES
        for match in _VARIABLE_REFERENCE_RE.finditer(value)
    )


def _contains_plugin_variable(value: str) -> bool:
    """Return whether a literal-only field contains any plugin placeholder."""
    return _VARIABLE_REFERENCE_RE.search(value) is not None


def validate_plugin_manifest_file(path: Path) -> ValidationResult:
    """Validate a plugin.json file from disk."""
    return validate_plugin_manifest_document(read_json_document(path))


def validate_mcp_config_file(
    path: Path,
    *,
    expected_plugin_schema_id: str = PLUGIN_SCHEMA_ID,
    isolate_invalid_servers: bool = False,
) -> ValidationResult:
    """Validate an mcp.json file from disk."""
    return validate_mcp_config_document(
        read_json_document(path),
        expected_plugin_schema_id=expected_plugin_schema_id,
        isolate_invalid_servers=isolate_invalid_servers,
    )


def validate_lsp_extension_file(path: Path) -> ValidationResult:
    """Validate the APM LSP extension file from disk."""
    return validate_lsp_extension_document(read_json_document(path))


def validate_lsp_extension_document(document: object) -> ValidationResult:
    """Validate the fixed ``com.microsoft.apm/lsp.json`` document shape."""
    result = ValidationResult()
    if not isinstance(document, dict):
        result.add_error("com.microsoft.apm/lsp.json must be a JSON object")
        return result
    if set(document) != {"lspServers"}:
        result.add_error("com.microsoft.apm/lsp.json must contain only the lspServers field")
        return result
    servers = document.get("lspServers")
    if not isinstance(servers, dict):
        result.add_error("com.microsoft.apm/lsp.json lspServers must be a JSON object")
        return result

    from ..models.dependency.lsp import LSPDependency

    normalized: dict[str, dict[str, Any]] = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not isinstance(config, dict):
            result.add_error("Every LSP server must have a string name and object config")
            continue
        spec = {"name": name, **config}
        try:
            dependency = LSPDependency.from_dict(spec)
        except (TypeError, ValueError) as exc:
            result.add_error(f"lspServers.{name} is invalid: {exc}")
            continue
        command = dependency.command or ""
        if not (_BARE_COMMAND_RE.fullmatch(command) or command.startswith("./")):
            result.add_error(f"lspServers.{name}.command must be a bare token or a ./ path")
            continue
        if args_contain_literal_secret(dependency.args) or contains_literal_secret_fields(
            dependency.env
        ):
            result.add_error(
                f"lspServers.{name} contains a literal secret; use a ${{VAR}} reference"
            )
            continue
        normalized[name] = dependency.to_lsp_json_entry()
    if result.errors:
        return result
    result.normalized = {"lspServers": normalized}
    return result


def validate_plugin_manifest_document(document: object) -> ValidationResult:
    """Validate a plugin.json document locally."""
    result = ValidationResult(schema_id=PLUGIN_SCHEMA_ID)
    if not isinstance(document, dict):
        result.add_error("plugin.json must be a JSON object")
        return result

    result.schema_id = _validate_schema_id(document, result, kind="plugin")
    if result.schema_id is None:
        return result

    normalized: dict[str, Any] = {"$schema": result.schema_id}
    for key in document:
        if key in _PLUGIN_TOP_LEVEL_FIELDS:
            continue
        result.add_warning(f"Unknown plugin.json top-level field ignored: {key}")

    name = document.get("name")
    if not isinstance(name, str):
        result.add_error("plugin.json name must be a string")
    elif not (1 <= len(name) <= 64) or not _PLUGIN_NAME_RE.fullmatch(name):
        result.add_error("plugin.json name does not satisfy the Agent Plugins naming rules")
    else:
        normalized["name"] = name

    _copy_optional_string_field(document, normalized, result, "version")
    _copy_optional_string_field(document, normalized, result, "description")
    _copy_optional_string_field(document, normalized, result, "homepage")
    _copy_optional_string_field(document, normalized, result, "repository")
    _copy_optional_string_field(document, normalized, result, "license")
    _copy_keywords_field(document, normalized, result)
    _copy_author_field(document, normalized, result)
    _copy_extensions_field(document, normalized, result)

    extensions = normalized.get("extensions")
    if isinstance(extensions, dict) and isinstance(
        extensions.get(COM_MICROSOFT_APM_NAMESPACE), dict
    ):
        apm_extension = extensions[COM_MICROSOFT_APM_NAMESPACE]
        schema_version = apm_extension.get("schemaVersion")
        if schema_version != COM_MICROSOFT_APM_SCHEMA_VERSION:
            result.add_warning(
                "plugin.json extensions.com.microsoft.apm ignored because schemaVersion is not '1'"
            )
            normalized.pop("extensions", None)

    if result.errors:
        result.normalized = None
        return result

    result.normalized = normalized
    return result


def validate_mcp_config_document(
    document: object,
    *,
    expected_plugin_schema_id: str = PLUGIN_SCHEMA_ID,
    isolate_invalid_servers: bool = False,
) -> ValidationResult:
    """Validate an mcp.json document locally."""
    result = ValidationResult(schema_id=MCP_SCHEMA_ID)
    if not isinstance(document, dict):
        result.add_error("mcp.json must be a JSON object")
        return result

    result.schema_id = _validate_schema_id(
        document, result, kind="mcp", expected_plugin_schema_id=expected_plugin_schema_id
    )
    if result.schema_id is None:
        return result

    for key in document:
        if key not in _MCP_TOP_LEVEL_FIELDS:
            result.add_error(f"mcp.json contains an unknown top-level field: {key}")

    mcp_servers = document.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        result.add_error("mcp.json mcpServers must be a JSON object")
        return result

    normalized_servers: dict[str, Any] = {}
    for server_name, server_value in mcp_servers.items():
        server_result = ValidationResult(schema_id=result.schema_id)
        validated = _validate_mcp_server(server_name, server_value, server_result)
        if server_result.errors:
            if isolate_invalid_servers:
                result.warnings.extend(f"{error}; server ignored" for error in server_result.errors)
            else:
                result.errors.extend(server_result.errors)
        if validated is not None and not server_result.errors:
            normalized_servers[server_name] = validated

    result.normalized = {"$schema": result.schema_id, "mcpServers": normalized_servers}
    return result


def _validate_schema_id(
    document: dict[str, Any],
    result: ValidationResult,
    *,
    kind: str,
    expected_plugin_schema_id: str = PLUGIN_SCHEMA_ID,
) -> str | None:
    schema_id = document.get("$schema")
    if not isinstance(schema_id, str):
        result.add_error(f"{kind}.json must declare a string $schema field")
        return None
    if kind == "plugin":
        if not supports_plugin_schema_id(schema_id):
            result.add_error(f"Unsupported plugin schema id: {schema_id}")
            return None
        return schema_id
    if not supports_mcp_schema_id(schema_id):
        result.add_error(f"Unsupported mcp schema id: {schema_id}")
        return None
    if schema_version_from_id(schema_id) != schema_version_from_id(expected_plugin_schema_id):
        result.add_error("mcp.json $schema version must match the plugin.json $schema version")
        return None
    return schema_id


def _copy_optional_string_field(
    source: dict[str, Any],
    target: dict[str, Any],
    result: ValidationResult,
    field_name: str,
) -> None:
    value = source.get(field_name)
    if value is None:
        return
    if not isinstance(value, str):
        result.add_error(f"plugin.json {field_name} must be a string")
        return
    target[field_name] = value


def _copy_keywords_field(
    source: dict[str, Any], target: dict[str, Any], result: ValidationResult
) -> None:
    value = source.get("keywords")
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        result.add_error("plugin.json keywords must be an array of strings")
        return
    target["keywords"] = list(value)


def _copy_author_field(
    source: dict[str, Any], target: dict[str, Any], result: ValidationResult
) -> None:
    value = source.get("author")
    if value is None:
        return
    if not isinstance(value, dict):
        result.add_error("plugin.json author must be an object")
        return
    allowed = {"name", "email", "url"}
    extra = sorted(set(value) - allowed)
    if extra:
        result.add_error("plugin.json author contains unsupported fields: " + ", ".join(extra))
        return
    cleaned: dict[str, str] = {}
    for field_name, field_value in value.items():
        if not isinstance(field_value, str):
            result.add_error(f"plugin.json author.{field_name} must be a string")
            return
        cleaned[field_name] = field_value
    target["author"] = cleaned


def _copy_extensions_field(
    source: dict[str, Any], target: dict[str, Any], result: ValidationResult
) -> None:
    value = source.get("extensions")
    if value is None:
        return
    if not isinstance(value, dict):
        result.add_warning("plugin.json extensions field ignored because it is not an object")
        return
    payload = value.get(COM_MICROSOFT_APM_NAMESPACE)
    if payload is None:
        return
    if not isinstance(payload, dict):
        result.add_warning(
            "plugin.json extensions.com.microsoft.apm ignored because it is not an object"
        )
        return
    target["extensions"] = {COM_MICROSOFT_APM_NAMESPACE: payload}


def _validate_mcp_server(
    server_name: str, server_value: object, result: ValidationResult
) -> dict[str, Any] | None:
    if not isinstance(server_value, dict):
        result.add_error(f"mcpServers.{server_name} must be an object")
        return None

    server_type = server_value.get("type")
    if not isinstance(server_type, str):
        result.add_error(f"mcpServers.{server_name}.type must be a string")
        return None
    if server_type not in _MCP_SERVER_TYPES:
        result.add_error(f"mcpServers.{server_name} has unsupported type: {server_type}")
        return None

    allowed_fields = {"type"}
    if server_type == "stdio":
        allowed_fields.update({"command", "args", "env", "cwd"})
    else:
        allowed_fields.update({"url", "headers"})

    for key in server_value:
        if key not in allowed_fields:
            result.add_error(f"mcpServers.{server_name} contains an unknown field: {key}")

    if server_type == "stdio":
        return _validate_stdio_server(server_name, server_value, result)
    return _validate_http_server(server_name, server_value, result, server_type=server_type)


def _validate_stdio_server(
    server_name: str, server_value: dict[str, Any], result: ValidationResult
) -> dict[str, Any] | None:
    command = server_value.get("command")
    if not isinstance(command, str) or not command:
        result.add_error(f"mcpServers.{server_name}.command must be a non-empty string")
        return None
    if not (_BARE_COMMAND_RE.fullmatch(command) or command.startswith("./")):
        result.add_error(f"mcpServers.{server_name}.command must be a bare token or a ./ path")
        return None

    normalized: dict[str, Any] = {"type": "stdio", "command": command}

    args, args_error = _normalize_stdio_args(server_name, server_value.get("args"))
    if args_error is not None:
        result.add_error(args_error)
        return None
    if args is not None:
        normalized["args"] = args

    env, env_error = _normalize_stdio_env(server_name, server_value.get("env"))
    if env_error is not None:
        result.add_error(env_error)
        return None
    if env is not None:
        normalized["env"] = env

    cwd = server_value.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            result.add_error(f"mcpServers.{server_name}.cwd must be a string")
            return None
        if _contains_unsupported_plugin_variable(cwd):
            result.add_error(
                f"mcpServers.{server_name}.cwd contains an unsupported placeholder; "
                "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable"
            )
            return None
        if not _PLUGIN_RELATIVE_CWD_RE.fullmatch(cwd):
            result.add_error(
                f"mcpServers.{server_name}.cwd must be a ./, ${'{'}PLUGIN_ROOT{'}'}, or ${'{'}PLUGIN_DATA{'}'} path"
            )
            return None
        normalized["cwd"] = cwd

    return normalized


def _normalize_stdio_args(
    server_name: str,
    value: object,
) -> tuple[list[str] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None, f"mcpServers.{server_name}.args must be an array of strings"
    if _agent_plugin_args_contain_secret(value):
        return (
            None,
            f"mcpServers.{server_name}.args contains a literal secret or credential material; "
            "Agent Plugins defines no portable credential references",
        )
    if any(_contains_unsupported_plugin_variable(item) for item in value):
        return (
            None,
            f"mcpServers.{server_name}.args contains an unsupported placeholder; "
            "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
        )
    return list(value), None


def _normalize_stdio_env(
    server_name: str,
    value: object,
) -> tuple[dict[str, str] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, f"mcpServers.{server_name}.env must be an object"
    if _agent_plugin_contains_secret_fields(value):
        return (
            None,
            f"mcpServers.{server_name}.env contains credential material; "
            "Agent Plugins defines no portable credential references",
        )
    cleaned: dict[str, str] = {}
    seen_names: set[str] = set()
    for key, item in value.items():
        if not isinstance(key, str):
            return None, f"mcpServers.{server_name}.env keys must be strings"
        if key.upper() in _RESERVED_ENV_NAMES:
            return None, f"mcpServers.{server_name}.env cannot define reserved name: {key}"
        lowered = key.lower()
        if lowered in seen_names:
            return None, f"mcpServers.{server_name}.env contains a duplicate name: {key}"
        if not isinstance(item, str):
            return None, f"mcpServers.{server_name}.env.{key} must be a string"
        if _contains_unsupported_plugin_variable(item):
            return (
                None,
                f"mcpServers.{server_name}.env.{key} contains an unsupported placeholder; "
                "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
            )
        seen_names.add(lowered)
        cleaned[key] = item
    return cleaned, None


def _validate_http_server(
    server_name: str,
    server_value: dict[str, Any],
    result: ValidationResult,
    *,
    server_type: str,
) -> dict[str, Any] | None:
    url = server_value.get("url")
    if not isinstance(url, str) or not url:
        result.add_error(f"mcpServers.{server_name}.url must be a non-empty string")
        return None
    if not _is_valid_http_url(url):
        result.add_error(f"mcpServers.{server_name}.url must be a valid http or https URL")
        return None
    if _contains_plugin_variable(url):
        result.add_error(
            f"mcpServers.{server_name}.url must be literal; placeholder expansion is not defined"
        )
        return None
    if _agent_plugin_url_contains_secret(url):
        result.add_error(
            f"mcpServers.{server_name}.url contains credential material; "
            "Agent Plugins URLs are literal and authentication is client-managed"
        )
        return None

    normalized: dict[str, Any] = {"type": server_type, "url": url}
    headers = server_value.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            result.add_error(f"mcpServers.{server_name}.headers must be an object")
            return None
        cleaned_headers: dict[str, str] = {}
        seen_headers: set[str] = set()
        for key, value in headers.items():
            if not isinstance(key, str) or not key:
                result.add_error(f"mcpServers.{server_name}.headers keys must be non-empty strings")
                return None
            if not _HTTP_HEADER_NAME_RE.fullmatch(key):
                result.add_error(
                    f"mcpServers.{server_name}.headers contains an invalid header name: {key}"
                )
                return None
            lowered = key.lower()
            if lowered in seen_headers:
                result.add_error(
                    f"mcpServers.{server_name}.headers contains a duplicate name: {key}"
                )
                return None
            if not isinstance(value, str):
                result.add_error(f"mcpServers.{server_name}.headers.{key} must be a string")
                return None
            if _contains_plugin_variable(value):
                result.add_error(
                    f"mcpServers.{server_name}.headers.{key} must be literal; "
                    "placeholder expansion is not defined"
                )
                return None
            seen_headers.add(lowered)
            cleaned_headers[key] = value
        normalized["headers"] = cleaned_headers
        if _agent_plugin_contains_secret_fields(cleaned_headers):
            result.add_error(
                f"mcpServers.{server_name}.headers contains credential material; "
                "Agent Plugins headers are literal and authentication is client-managed"
            )
            return None

    return normalized


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        return False
    host = parsed.hostname
    if host is None:
        return False
    if parsed.scheme == "http":
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return True
