"""Rule files (Claude, Cursor, Kiro, Windsurf, Antigravity, Grok) -> ``.apm/instructions``.

Inverse of ``InstructionIntegrator._convert_to_*``: each vendor's scoping
frontmatter becomes APM's ``applyTo``; a missing description is derived from
the body exactly as the forward direction does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.utils.patterns import normalize_apply_to

from ..model import Finding
from . import ConvertContext, ConvertError, ConvertResult
from .base import derive_description, emit_markdown, read_markdown

_NEUTRAL_KEYS = frozenset({"description", "applyTo"})
ALWAYS_ON = "**"
"""``applyTo`` value that keeps an always-on source always-on on every target.

Targets render ``applyTo`` as their native scoping (Cursor ``globs``, Claude
``paths``, Kiro ``fileMatchPattern``); a rule with no ``applyTo`` becomes a
description-triggered rule on Cursor, which would silently demote imported
root context. ``**`` matches every file on all of them.
"""


def _apply_to_from(value: Any) -> str:
    if isinstance(value, list):
        return normalize_apply_to(value, default="")
    if value is None:
        return ""
    return str(value).strip()


def _cursor(meta: dict[str, Any], result: ConvertResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if meta.get("description"):
        out["description"] = str(meta["description"]).strip()
        result.keep("frontmatter.description")
    globs = _apply_to_from(meta.get("globs"))
    always = bool(meta.get("alwaysApply"))
    if always:
        out["applyTo"] = ALWAYS_ON
        result.transform("frontmatter.alwaysApply", "always-on rule expressed as applyTo: '**'")
        if globs:
            result.drop("frontmatter.globs", "alwaysApply wins over globs")
    elif globs:
        out["applyTo"] = globs
        result.transform("frontmatter.globs", "globs -> applyTo")
    elif out.get("description"):
        result.transform(
            "frontmatter.alwaysApply",
            "Cursor 'agent requested' rule has no APM trigger; imported as always-on",
            "warning",
        )
    else:
        result.transform(
            "frontmatter.alwaysApply",
            "Cursor 'manual' rule has no APM trigger; imported as always-on",
            "warning",
        )
    return out


def _claude(meta: dict[str, Any], result: ConvertResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    paths = _apply_to_from(meta.get("paths"))
    if paths:
        out["applyTo"] = paths
        result.transform("frontmatter.paths", "paths -> applyTo")
    else:
        out["applyTo"] = ALWAYS_ON
        result.default(
            "frontmatter.applyTo", "Claude rule without paths is always-on; applyTo: '**'"
        )
    if meta.get("description"):
        out["description"] = str(meta["description"]).strip()
    return out


def _kiro(meta: dict[str, Any], result: ConvertResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    inclusion = str(meta.get("inclusion", "always")).strip()
    pattern = _apply_to_from(meta.get("fileMatchPattern"))
    if inclusion == "fileMatch" and pattern:
        out["applyTo"] = pattern
        result.transform("frontmatter.fileMatchPattern", "fileMatchPattern -> applyTo")
    elif inclusion == "always":
        out["applyTo"] = ALWAYS_ON
        result.transform("frontmatter.inclusion", "always-on steering expressed as applyTo: '**'")
    elif inclusion == "manual":
        result.transform(
            "frontmatter.inclusion", "Kiro on-demand steering imported as always-on", "warning"
        )
    if meta.get("description"):
        out["description"] = str(meta["description"]).strip()
    return out


def _trigger_globs(meta: dict[str, Any], result: ConvertResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    trigger = str(meta.get("trigger", "always_on")).strip()
    globs = _apply_to_from(meta.get("globs"))
    if trigger == "glob" and globs:
        out["applyTo"] = globs
        result.transform("frontmatter.globs", "trigger: glob + globs -> applyTo")
    elif trigger == "always_on":
        out["applyTo"] = ALWAYS_ON
        result.transform("frontmatter.trigger", "always-on rule expressed as applyTo: '**'")
    elif trigger in ("model_decision", "manual"):
        result.transform(
            "frontmatter.trigger",
            f"'{trigger}' rule has no APM trigger; imported as always-on",
            "warning",
        )
    if meta.get("description"):
        out["description"] = str(meta["description"]).strip()
    return out


def _generic(meta: dict[str, Any], result: ConvertResult) -> dict[str, Any]:
    if "applyTo" in meta or "description" in meta:
        out: dict[str, Any] = {}
        if meta.get("description"):
            out["description"] = str(meta["description"]).strip()
        apply_to = _apply_to_from(meta.get("applyTo"))
        if apply_to:
            out["applyTo"] = apply_to
        return out
    if "globs" in meta or "alwaysApply" in meta:
        return _cursor(meta, result)
    return _claude(meta, result)


_BY_FORMAT = {
    "cursor_rules": _cursor,
    "claude_rules": _claude,
    "kiro_steering": _kiro,
    "windsurf_rules": _trigger_globs,
    "antigravity_rules": _trigger_globs,
}


class RulesConverter:
    """Convert any vendor rule file into an APM instruction."""

    id = "rules->instruction"

    def handles(self, converter_id: str) -> bool:
        return converter_id.endswith("->instruction") and not converter_id.startswith(
            "root_context"
        )

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        if finding.abs_path is None:
            raise ConvertError("no source path")
        result = ConvertResult()
        meta, body = read_markdown(finding.abs_path, ctx.limits.max_file_bytes, result)
        mapper = _BY_FORMAT.get(finding.format_id or "", _generic)
        out = mapper(meta, result)
        consumed = {
            "description",
            "applyTo",
            "globs",
            "alwaysApply",
            "paths",
            "inclusion",
            "fileMatchPattern",
            "trigger",
        }
        for key in meta:
            if key not in consumed:
                result.drop(f"frontmatter.{key}", "vendor-only key has no APM equivalent")
        if not out.get("description"):
            derived = derive_description(body)
            if derived:
                out = {"description": derived, **out}
                result.default("frontmatter.description", "derived from first sentence")
        ordered = {k: out[k] for k in ("description", "applyTo") if k in out}
        if not body.strip():
            raise ConvertError("rule body is empty")
        emit_markdown(dest, ordered, body)
        result.written.append(dest)
        return result


CONVERTERS = (RulesConverter(),)
