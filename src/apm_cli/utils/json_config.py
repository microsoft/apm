"""The one writer for user-editable JSON MCP config files (#4526).

``.mcp.json``, ``~/.claude.json``, the retired Copilot project MCP file and
the IntelliJ MCP config are shared with the user. Like npm with
``package.json``, APM keeps the
file's existing style when it rewrites one: indentation (N spaces or a tab),
the final newline, LF or CRLF line endings, and literal non-ASCII text. Other
layout is normalized by the JSON round trip: an inline array gets one item per
line, and a file mixing literal and escaped non-ASCII text is written
literally. A new, empty or single-line (minified)
file is written in the caller's default style: 2-space indentation, LF, ASCII
escapes, and a final newline when ``default_trailing_newline`` is set.

Key order is the order of ``data``: callers parse the file (``json.loads``
keeps insertion order), so user keys stay in place and new entries come last.
Comments (JSONC) are not preserved; the file is JSON round-tripped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text

_DEFAULT_INDENT = 2
_LEADING_WHITESPACE = re.compile(r"^([ \t]+)\S")


@dataclass(frozen=True)
class JsonConfigStyle:
    """How a JSON config file is laid out on disk."""

    indent: int | str
    newline: str
    final_newline: bool
    ensure_ascii: bool


def default_json_config_style(*, trailing_newline: bool) -> JsonConfigStyle:
    return JsonConfigStyle(_DEFAULT_INDENT, "\n", trailing_newline, ensure_ascii=True)


def detect_json_config_style(
    text: str | None, *, default_trailing_newline: bool
) -> JsonConfigStyle:
    """Return the style of *text*, or the default for a new or minified file."""
    default = default_json_config_style(trailing_newline=default_trailing_newline)
    if not text or not text.strip():
        return default
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return default
    indent: int | str = _DEFAULT_INDENT
    for line in lines[1:]:
        match = _LEADING_WHITESPACE.match(line)
        if match:
            whitespace = match.group(1)
            indent = "\t" if whitespace.startswith("\t") else len(whitespace)
            break
    return JsonConfigStyle(
        indent=indent,
        newline="\r\n" if "\r\n" in text else "\n",
        final_newline=text.endswith("\n"),
        ensure_ascii=text.isascii(),
    )


def render_json_config(data: Any, style: JsonConfigStyle) -> str:
    """Serialize *data* in *style*; JSON escapes keep raw newlines out of values."""
    text = json.dumps(data, indent=style.indent, ensure_ascii=style.ensure_ascii)
    if style.newline != "\n":
        text = text.replace("\n", style.newline)
    return text + (style.newline if style.final_newline else "")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def json_config_text(path: Path, data: Any, *, default_trailing_newline: bool) -> str:
    """The text :func:`write_json_config` writes for *data* at *path*, in its own style."""
    current = _read_text(path)
    style = detect_json_config_style(current, default_trailing_newline=default_trailing_newline)
    return render_json_config(data, style)


def write_json_config(
    path: Path,
    data: Any,
    *,
    default_trailing_newline: bool,
    new_file_mode: int | None = None,
) -> bool:
    """Write *data* to *path* in the file's own style; return whether it wrote.

    The write is atomic and skipped when the bytes would not change. The
    caller creates the parent directory and owns symlink and mode policy.
    """
    current = _read_text(path)
    style = detect_json_config_style(current, default_trailing_newline=default_trailing_newline)
    text = render_json_config(data, style)
    if current == text:
        return False
    atomic_write_text(path, text, new_file_mode=new_file_mode, normalize_line_endings=False)
    return True
