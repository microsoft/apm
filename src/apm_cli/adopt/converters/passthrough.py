"""Converters for files that already use APM's own format."""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.hook_contract import HookContractError, parse_hook_source
from apm_cli.utils.atomic_io import write_text_lf

from ..model import Finding
from . import ConvertContext, ConvertError, ConvertResult
from .base import emit_markdown, read_markdown, read_text, refuse_credentials


class _MarkdownPassthrough:
    """Copy a ``*.instructions.md`` / ``*.agent.md`` / ``*.prompt.md`` verbatim."""

    def __init__(self, converter_id: str) -> None:
        self.id = converter_id

    def handles(self, converter_id: str) -> bool:
        return converter_id == self.id

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        if finding.abs_path is None:
            raise ConvertError("no source path")
        text = read_text(finding.abs_path, ctx.limits.max_file_bytes)
        result = ConvertResult()
        meta, body = read_markdown(finding.abs_path, ctx.limits.max_file_bytes, result)
        refuse_credentials(text)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if result.changes:  # lenient parse: re-emit as strict YAML so apm install can read it
            emit_markdown(dest, meta, body)
        else:
            write_text_lf(dest, text if text.endswith("\n") else text + "\n")
            result.keep("body", "copied verbatim")
        result.written.append(dest)
        return result


class HookFilePassthrough:
    """Copy a Copilot per-file hook document (already the neutral grammar)."""

    id = "passthrough.hook"

    def handles(self, converter_id: str) -> bool:
        return converter_id == self.id

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        if finding.abs_path is None:
            raise ConvertError("no source path")
        text = read_text(finding.abs_path, ctx.limits.max_file_bytes)
        refuse_credentials(text)
        try:
            document = json.loads(text)
            parse_hook_source(document)
        except (ValueError, HookContractError) as exc:
            raise ConvertError(f"hook file is not valid: {type(exc).__name__}") from exc
        result = ConvertResult()
        if isinstance(document, dict) and "version" in document:
            document = {k: v for k, v in document.items() if k != "version"}
            result.default("version", "target-specific schema version re-added on install")
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(dest, json.dumps(document, indent=2) + "\n")
        result.written.append(dest)
        return result


CONVERTERS = (
    _MarkdownPassthrough("passthrough.instruction"),
    _MarkdownPassthrough("passthrough.prompt"),
    _MarkdownPassthrough("passthrough.agent"),
    HookFilePassthrough(),
)
