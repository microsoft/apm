"""Shared helpers for converters: markdown I/O, naming, description defaults."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from apm_cli.integration.skill_transformer import to_hyphen_case
from apm_cli.utils.atomic_io import write_text_lf
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.path_security import ensure_path_within, validate_path_segments
from apm_cli.utils.yaml_io import loads_frontmatter, yaml_to_str

from ..redact import contains_credential
from . import ConvertError

_MAX_DESCRIPTION = 200


def read_text(path: Path, limit: int) -> str:
    """Read a UTF-8 text source, refusing symlinks and oversize files."""
    if path.is_symlink():
        raise ConvertError("source is a symlink")
    try:
        if path.stat().st_size > limit:
            raise ConvertError("source exceeds the size limit")
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConvertError("source is not UTF-8 text") from exc
    except OSError as exc:
        raise ConvertError(f"unreadable source: {type(exc).__name__}") from exc


_FENCE_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*):(?:[ \t]+(.*))?$")
_ITEM_RE = re.compile(r"^[ \t]+-[ \t]+(.*)$")


def lenient_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    """Parse ``key: value`` frontmatter the way Claude Code does when YAML is invalid.

    Values are taken verbatim to the end of the line (so colons and escapes
    inside a description do not break parsing); indented ``- item`` lines
    build a list. Returns ``None`` when there is no frontmatter fence.
    """
    match = _FENCE_RE.match(text.lstrip("\ufeff"))
    if not match:
        return None
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in match.group(1).splitlines():
        key_match = _KEY_RE.match(line)
        item_match = _ITEM_RE.match(line)
        if key_match and current is not None:
            # Example transcripts inside a description ("Context: ...", "user: ...")
            # are continuations, not keys: capitalised names or an open <example>.
            previous = meta.get(current)
            open_example = isinstance(previous, str) and previous.count(
                "<example>"
            ) > previous.count("</example>")
            if key_match.group(1)[0].isupper() or open_example:
                key_match = None
        if key_match:
            current = key_match.group(1)
            value = (key_match.group(2) or "").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[current] = value
        elif item_match and current is not None:
            existing = meta.get(current)
            items = (
                existing
                if isinstance(existing, list)
                else ([] if existing in ("", None) else [existing])
            )
            items.append(item_match.group(1).strip().strip("\"'"))
            meta[current] = items
        elif current is not None and line.strip():
            meta[current] = f"{meta[current]} {line.strip()}".strip()
    return meta, text[match.end() :]


def read_markdown(path: Path, limit: int, result: Any | None = None) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body) for a markdown source.

    Uses the bounded YAML loader; when the frontmatter is not valid YAML (a
    common Claude Code idiom: unquoted descriptions with colons), fall back
    to the lenient line parser and record a ``transformed`` change on
    *result* when given.
    """
    text = read_text(path, limit)
    try:
        post = loads_frontmatter(text, preserve_body=True)
    except yaml.YAMLError as exc:
        lenient = lenient_frontmatter(text)
        if lenient is None:
            raise ConvertError(f"invalid frontmatter: {type(exc).__name__}") from exc
        if result is not None:
            result.transform(
                "frontmatter", "source frontmatter is not strict YAML; parsed leniently", "warning"
            )
        return lenient
    metadata = post.metadata if isinstance(post.metadata, dict) else {}
    return dict(metadata), post.content


def refuse_credentials(text: str) -> None:
    """Raise when *text* carries literal credential material (never copied into .apm/)."""
    label = contains_credential(text)
    if label is not None:
        raise ConvertError(f"possible {label} in content; refusing to import (redact it first)")


def emit_markdown(dest: Path, metadata: Mapping[str, Any], body: str) -> None:
    """Write an APM markdown primitive with ordered frontmatter and LF endings."""
    refuse_credentials(body)
    refuse_credentials(yaml_to_str(dict(metadata)) if metadata else "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = body.lstrip("\r\n")
    if not text.endswith("\n"):
        text += "\n"
    if metadata:
        text = "---\n" + yaml_to_str(dict(metadata)) + "---\n\n" + text
    write_text_lf(dest, text)


def derive_description(body: str) -> str:
    """First meaningful sentence of *body*, the same default the integrators use."""
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith(("<!--", "```", "@", "---")):
            sentence = stripped.split(". ")[0].rstrip(".").strip()
            return sentence[:_MAX_DESCRIPTION]
    return ""


def kebab(name: str) -> str:
    """Kebab-case a file stem, collapsing runs of separators."""
    lowered = to_hyphen_case(name).lower()
    lowered = re.sub(r"[^a-z0-9.-]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-.")
    return lowered or "item"


def flatten_relative(rel: PurePosixPath, strip_suffixes: Iterable[str]) -> tuple[str, bool]:
    """Turn ``frontend/component.md`` into ``frontend-component``; report nesting."""
    name = rel.name
    for suffix in sorted(strip_suffixes, key=len, reverse=True):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    parts = [*rel.parts[:-1], name]
    return kebab("-".join(parts)), len(rel.parts) > 1


def split_tools(value: Any) -> list[str] | None:
    """Normalise a ``tools`` field (string list or comma string) into a list."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, dict):
        return [str(k) for k, enabled in value.items() if enabled]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return None


class NameAllocator:
    """Hand out unique destination names inside one ``.apm/`` write run."""

    def __init__(self, apm_dir: Path) -> None:
        self.apm_dir = apm_dir
        self._taken: dict[str, str] = {}
        self._by_content: dict[str, str] = {}

    def allocate(self, subdir: str, stem: str, suffix: str, tool: str) -> tuple[str, bool]:
        """Return (relative destination under .apm/, renamed?)."""
        validate_path_segments(subdir, context="adopt destination")
        base = kebab(stem)
        candidates = [base, f"{base}-{kebab(tool)}"] + [f"{base}-{n}" for n in range(2, 50)]
        for candidate in candidates:
            rel = f"{subdir}/{candidate}{suffix}"
            if rel in self._taken:
                continue
            self._taken[rel] = tool
            return rel, candidate != base
        raise ConvertError(f"could not allocate a unique name for {stem!r}")

    def claim_content(self, key: str, rel: str) -> str | None:
        """Register content *key* for *rel*; return an earlier holder if duplicate."""
        existing = self._by_content.get(key)
        if existing is not None:
            return existing
        self._by_content[key] = rel
        return None

    def within(self, dest: Path) -> Path:
        return ensure_path_within(dest, self.apm_dir)


def content_key(path: Path) -> str:
    return compute_file_hash(path)
