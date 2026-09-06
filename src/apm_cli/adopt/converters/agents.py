"""Agent definitions (markdown or Codex TOML) -> ``.apm/agents/<name>.agent.md``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import Finding
from . import ConvertContext, ConvertError, ConvertResult
from .base import derive_description, emit_markdown, read_markdown, read_text, split_tools

NEUTRAL_AGENT_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "tools",
    "model",
    "handoffs",
    "applyTo",
)
_TOML_BODY_KEY = "developer_instructions"


def _from_toml(text: str, result: ConvertResult) -> tuple[dict[str, Any], str]:
    import tomlkit

    try:
        document = tomlkit.parse(text).unwrap()
    except Exception as exc:  # tomlkit raises several parse error classes
        raise ConvertError(f"invalid TOML: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise ConvertError("TOML agent must be a table")
    body = str(document.pop(_TOML_BODY_KEY, "") or "")
    result.transform(f"toml.{_TOML_BODY_KEY}", "developer_instructions -> markdown body")
    return document, body


class AgentsConverter:
    """Keep the neutral agent keys; drop and report everything vendor-specific."""

    id = "agents->agent"

    def handles(self, converter_id: str) -> bool:
        return converter_id.endswith("->agent")

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        if finding.abs_path is None:
            raise ConvertError("no source path")
        result = ConvertResult()
        fmt = finding.format_id or ""
        if fmt == "codex_agent" or finding.abs_path.suffix == ".toml":
            meta, body = _from_toml(read_text(finding.abs_path, ctx.limits.max_file_bytes), result)
        else:
            meta, body = read_markdown(finding.abs_path, ctx.limits.max_file_bytes, result)
        out: dict[str, Any] = {}
        for key in NEUTRAL_AGENT_KEYS:
            if key not in meta or meta[key] in (None, ""):
                continue
            value = meta[key]
            if key == "tools":
                tools = split_tools(value)
                if tools is None:
                    result.drop("frontmatter.tools", "unrecognised tools shape")
                    continue
                if not isinstance(value, list):
                    result.transform("frontmatter.tools", "tools normalised to a list")
                if fmt == "kiro_agent":
                    result.transform(
                        "frontmatter.tools",
                        "Kiro capability tags are not tool names on other harnesses",
                        "warning",
                    )
                if fmt == "opencode_agent" and isinstance(value, dict):
                    result.drop(
                        "frontmatter.tools[disabled]", "disabled OpenCode tools not representable"
                    )
                out[key] = tools
            else:
                out[key] = value
                result.keep(f"frontmatter.{key}")
        for key in meta:
            if key not in NEUTRAL_AGENT_KEYS:
                result.drop(f"frontmatter.{key}", "vendor-only agent key has no APM equivalent")
        if not out.get("description"):
            derived = derive_description(body)
            if derived:
                out["description"] = derived
                result.default("frontmatter.description", "derived from first sentence")
        if not body.strip() and not out.get("description"):
            raise ConvertError("agent has neither body nor description")
        ordered = {k: out[k] for k in NEUTRAL_AGENT_KEYS if k in out}
        emit_markdown(dest, ordered, body if body.strip() else f"{out.get('description', '')}\n")
        result.written.append(dest)
        return result


CONVERTERS = (AgentsConverter(),)
