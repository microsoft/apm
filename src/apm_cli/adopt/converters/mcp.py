"""Native MCP client entries -> self-defined ``dependencies.mcp`` manifest entries.

Secrets never reach ``apm.yml``: literal credential-looking values are replaced
by ``${<SERVER>_<KEY>}`` placeholders that ``apm install`` resolves from the
environment at install time.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from apm_cli.adapters.client.base import _has_env_placeholder, _translate_env_placeholder
from apm_cli.models.dependency.mcp import _NAME_REGEX, MCPDependency

from ..model import Finding
from ..redact import looks_like_secret
from . import ConvertContext, ConvertError, ConvertResult

_HTTP_TRANSPORTS = frozenset({"http", "sse", "streamable-http"})
_PASSTHROUGH_TO_EXTRA: dict[str, tuple[str, ...]] = {
    "gemini": ("includeTools", "excludeTools", "trust", "timeout"),
    "antigravity": ("includeTools", "excludeTools", "trust", "timeout"),
    "kiro": ("autoApprove", "disabledTools", "disabled"),
    "codex": ("startup_timeout_sec", "tool_timeout_sec", "enabled_tools", "disabled_tools"),
    "claude": ("oauth",),
    "cursor": ("oauth",),
}


def placeholder_name(server: str, key: str) -> str:
    server_slug = re.sub(r"[^A-Z0-9]+", "_", server.upper()).strip("_")
    key_slug = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
    slug = key_slug if key_slug.startswith(server_slug + "_") else f"{server_slug}_{key_slug}"
    return "${" + slug.strip("_") + "}"


def _sanitize_name(name: str, result: ConvertResult) -> str:
    if _NAME_REGEX.match(name):
        return name
    cleaned = re.sub(r"[^a-zA-Z0-9._@/:=-]", "-", name).strip("-") or "mcp-server"
    result.transform("name", "server name sanitised for apm.yml")
    return cleaned[:128]


def _scrub_map(
    values: Mapping[str, Any], server: str, field: str, result: ConvertResult
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, raw in values.items():
        text = str(raw)
        if _has_env_placeholder(text):
            translated = _translate_env_placeholder(text)
            if translated != text:
                result.transform(f"{field}.{key}", "placeholder spelling normalised to ${VAR}")
            out[str(key)] = translated
        elif re.fullmatch(r"\$\{input:([^}]+)\}", text):
            var = re.sub(
                r"[^A-Z0-9]+", "_", re.fullmatch(r"\$\{input:([^}]+)\}", text).group(1).upper()
            )
            out[str(key)] = "${" + var + "}"
            result.transform(f"{field}.{key}", "${input:ID} -> ${ID}")
        elif looks_like_secret(str(key), text):
            out[str(key)] = placeholder_name(server, str(key))
            result.redacted(
                f"{field}.{key}", f"literal replaced; export {out[str(key)]} before apm install"
            )
        else:
            out[str(key)] = text
    return out


def _scrub_url(url: str, server: str, result: ConvertResult) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        netloc = f"{parts.username or 'user'}:{placeholder_name(server, 'URL_PASSWORD')}@{netloc}"
        result.redacted("url.userinfo", "credentials replaced by a placeholder")
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if looks_like_secret(key, value):
            query_pairs.append(f"{key}={placeholder_name(server, 'URL_' + key)}")
            result.redacted(f"url.query.{key}", "query credential replaced by a placeholder")
        else:
            query_pairs.append(f"{key}={value}")
    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(query_pairs), parts.fragment))


def _scrub_args(args: Any, server: str, result: ConvertResult) -> list[str]:
    if not isinstance(args, list):
        return []
    out: list[str] = []
    for index, item in enumerate(args):
        text = str(item)
        if _has_env_placeholder(text):
            out.append(_translate_env_placeholder(text))
        elif looks_like_secret(None, text) and not text.startswith("-") and "/" not in text:
            out.append(placeholder_name(server, f"ARG{index}"))
            result.redacted(
                f"args[{index}]", "credential-looking argument replaced by a placeholder"
            )
        else:
            out.append(text)
    return out


def _transport_for(
    tool: str, config: Mapping[str, Any], result: ConvertResult
) -> tuple[str, str | None]:
    """Return (transport, url_key) for the source config."""
    kind = str(config.get("type", "")).strip().lower()
    if tool in ("gemini", "antigravity"):
        if config.get("httpUrl"):
            return "http", "httpUrl"
        if config.get("url"):
            return "sse", "url"
        return "stdio", None
    if tool == "opencode":
        return (
            ("stdio", None)
            if kind == "local" or isinstance(config.get("command"), list)
            else ("http", "url")
        )
    if config.get("command"):
        if kind and kind not in ("stdio", "local"):
            result.drop("type", "type inconsistent with command; stdio assumed")
        return "stdio", None
    if kind in _HTTP_TRANSPORTS:
        return kind, "url"
    if config.get("url"):
        result.default("transport", "no type given; http assumed")
        return "http", "url"
    raise ConvertError("server has neither command nor url")


def to_manifest_entry(
    tool: str, name: str, config: Mapping[str, Any], result: ConvertResult
) -> dict[str, Any]:
    """Build a redacted, validated ``dependencies.mcp`` entry."""
    server = _sanitize_name(name, result)
    transport, url_key = _transport_for(tool, config, result)
    entry: dict[str, Any] = {"name": server, "registry": False, "transport": transport}
    consumed: set[str] = {"type"}
    if transport == "stdio":
        command = config.get("command")
        args = config.get("args", [])
        if tool == "opencode" and isinstance(command, list):
            command, args = (command[0] if command else ""), list(command[1:])
            result.transform("command", "OpenCode command array split into command + args")
            consumed.add("environment")
        if isinstance(command, str) and " " in command.strip() and not args:
            parts = shlex.split(command)
            command, args = parts[0], parts[1:]
            result.transform("command", "command string split into command + args")
        if not command:
            raise ConvertError("stdio server without a command")
        entry["command"] = str(command)
        consumed.update({"command", "args"})
        scrubbed_args = _scrub_args(args, server, result)
        if scrubbed_args:
            entry["args"] = scrubbed_args
        env = config.get("environment") if tool == "opencode" else config.get("env")
        consumed.add("env")
        if isinstance(env, Mapping) and env:
            entry["env"] = _scrub_map(env, server, "env", result)
        if config.get("cwd"):
            entry["cwd"] = str(config["cwd"])
            consumed.add("cwd")
    else:
        url = str(config.get(url_key or "url", ""))
        consumed.update({"url", "httpUrl"})
        entry["url"] = _scrub_url(url, server, result)
        headers: dict[str, Any] = {}
        raw_headers = config.get("headers") or config.get("http_headers")
        consumed.update({"headers", "http_headers"})
        if isinstance(raw_headers, Mapping):
            headers.update(raw_headers)
        if tool == "codex":
            bearer = config.get("bearer_token_env_var")
            if bearer:
                headers["Authorization"] = "Bearer ${" + str(bearer) + "}"
                result.transform("bearer_token_env_var", "-> headers.Authorization placeholder")
            env_headers = config.get("env_http_headers")
            if isinstance(env_headers, Mapping):
                for header, var in env_headers.items():
                    headers[str(header)] = "${" + str(var) + "}"
                result.transform("env_http_headers", "-> headers placeholders")
            consumed.update({"bearer_token_env_var", "env_http_headers"})
        if headers:
            entry["headers"] = _scrub_map(headers, server, "headers", result)
    tools = config.get("tools")
    consumed.add("tools")
    if isinstance(tools, list) and tools and tools != ["*"]:
        entry["tools"] = [str(t) for t in tools]
    extra: dict[str, Any] = {}
    for key in _PASSTHROUGH_TO_EXTRA.get(tool, ()):
        if key in config:
            extra[key] = config[key]
            consumed.add(key)
    for key in config:
        if key in consumed:
            continue
        if key in ("id", "enabled", "inputs", "envFile", "dev", "environment"):
            result.drop(key, "client-specific key not carried into apm.yml")
        else:
            result.drop(key, "unknown client key not carried into apm.yml")
    if extra:
        entry["extra"] = {k: v for k, v in extra.items()}
        result.keep("extra", "client passthrough keys kept under extra")
    try:
        MCPDependency.from_dict(dict(entry))
    except ValueError as exc:
        # Never echo the message: MCPDependency interpolates command/url values.
        raise ConvertError(f"entry rejected by MCP validation ({type(exc).__name__})") from None
    return entry


class McpConverter:
    """Produce a manifest fragment; writes no files."""

    id = "mcp->dependencies.mcp"

    def handles(self, converter_id: str) -> bool:
        return converter_id == self.id

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        payload = finding.payload if isinstance(finding.payload, dict) else None
        if not payload or "config" not in payload:
            raise ConvertError("no MCP payload")
        result = ConvertResult()
        entry = to_manifest_entry(
            str(payload["tool"]), str(payload["name"]), payload["config"], result
        )
        result.manifest_fragment = {"dependencies": {"mcp": [entry]}}
        return result


CONVERTERS = (McpConverter(),)
