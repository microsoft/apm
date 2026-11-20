"""G6 (#4526): MCP stale-entry cleanup keeps the user's JSON style.

``_clean_json_mcp_config`` (VS Code, Cursor, OpenCode, Windsurf, Gemini) and
``_clean_claude_config`` (``.mcp.json`` / ``~/.claude.json``) remove APM's
stale entries from files the user also edits. Removing APM's entry must leave
every other byte as the user wrote it: indentation at every depth, the final
newline (or its absence), CRLF line endings and non-ASCII text, literal or
escaped. Expected bytes are hand-written. Test sources stay ASCII; non-ASCII
bytes are built with escapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.mcp_integrator import _clean_claude_config, _clean_json_mcp_config
from apm_cli.utils.json_config import write_json_config

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-mcp-json-config-writer")]

_E_ACUTE = "\u00e9"


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def progress(self, message: str, *args: object, **kwargs: object) -> None:
        self.messages.append(message)


def _doc(indent: str, *, apm: bool, description: str = "local") -> str:
    """A user's MCP config, nested two levels below the server, with or without APM's entry."""
    i1, i2, i3, i4 = indent, indent * 2, indent * 3, indent * 4
    apm_entry = f'{i2}"apm-srv": {{\n{i3}"command": "tool-a"\n{i2}}},\n' if apm else ""
    return (
        "{\n"
        f'{i1}"mcpServers": {{\n'
        f'{i2}"mine": {{\n'
        f'{i3}"command": "my-tool",\n'
        f'{i3}"description": "{description}",\n'
        f'{i3}"env": {{\n'
        f'{i4}"LEVEL": "2"\n'
        f"{i3}}}\n"
        f"{i2}}},\n"
        f"{apm_entry}"
        f'{i2}"after": {{\n'
        f'{i3}"command": "z"\n'
        f"{i2}}}\n"
        f"{i1}}}\n"
        "}"
    )


def _style(indent: str, *, final_newline: bool, crlf: bool = False, description: str = "local"):
    def render(*, apm: bool) -> bytes:
        text = _doc(indent, apm=apm, description=description)
        if final_newline:
            text += "\n"
        if crlf:
            text = text.replace("\n", "\r\n")
        return text.encode("utf-8")

    return render


# (a) 4 spaces + final newline, (b) tab + no final newline, (c) CRLF,
# (d1) literal non-ASCII, (d2) escaped non-ASCII.
STYLES = {
    "a-4space-newline": _style("    ", final_newline=True),
    "b-tab-no-newline": _style("\t", final_newline=False),
    "c-crlf": _style("    ", final_newline=True, crlf=True),
    "d1-literal-non-ascii": _style("  ", final_newline=True, description=f"caf{_E_ACUTE}"),
    "d2-escaped-non-ascii": _style("    ", final_newline=False, description="caf\\u00e9"),
}


def _clean_generic(path: Path, names: set[str], logger: _Logger) -> int:
    return _clean_json_mcp_config(path, names, logger, "test config", trailing_newline=False)


def _clean_claude(path: Path, names: set[str], logger: _Logger) -> int:
    return _clean_claude_config(path, names, logger)


WRITERS = {"json-mcp-config": _clean_generic, "claude-config": _clean_claude}


@pytest.mark.parametrize("writer", sorted(WRITERS))
@pytest.mark.parametrize("style", sorted(STYLES))
def test_removing_apm_entry_restores_the_user_bytes(tmp_path: Path, writer: str, style: str):
    path = tmp_path / "mcp.json"
    path.write_bytes(STYLES[style](apm=True))

    removed = WRITERS[writer](path, {"apm-srv"}, _Logger())

    assert removed == 1
    assert path.read_bytes() == STYLES[style](apm=False)


@pytest.mark.parametrize("writer", sorted(WRITERS))
@pytest.mark.parametrize("style", sorted(STYLES))
def test_add_then_remove_round_trips_byte_identical(tmp_path: Path, writer: str, style: str):
    path = tmp_path / "mcp.json"
    original = STYLES[style](apm=False)
    path.write_bytes(original)
    data = json.loads(original.decode("utf-8"))
    data["mcpServers"]["apm-srv"] = {"command": "tool-a"}
    assert write_json_config(path, data, default_trailing_newline=True)
    assert path.read_bytes() != original

    WRITERS[writer](path, {"apm-srv"}, _Logger())

    assert path.read_bytes() == original


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_nothing_stale_writes_nothing(tmp_path: Path, writer: str):
    path = tmp_path / "mcp.json"
    original = STYLES["c-crlf"](apm=False)
    path.write_bytes(original)
    before = path.stat().st_mtime_ns

    assert WRITERS[writer](path, {"apm-srv"}, _Logger()) == 0

    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before


_MINIFIED = b'{"mcpServers":{"mine":{"command":"b"},"apm-srv":{"command":"a"}}}'
_MINIFIED_CLEANED = b'{\n  "mcpServers": {\n    "mine": {\n      "command": "b"\n    }\n  }\n}'


@pytest.mark.parametrize(
    ("writer", "expected"),
    [("json-mcp-config", _MINIFIED_CLEANED), ("claude-config", _MINIFIED_CLEANED + b"\n")],
)
def test_minified_file_keeps_todays_default_style(tmp_path: Path, writer: str, expected: bytes):
    path = tmp_path / "mcp.json"
    path.write_bytes(_MINIFIED)

    assert WRITERS[writer](path, {"apm-srv"}, _Logger()) == 1

    assert path.read_bytes() == expected


@pytest.mark.parametrize("writer", sorted(WRITERS))
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"  \n\t\n",
        b'{\n  // user note\n  "mcpServers": {"apm-srv": {"command": "a"}}\n}\n',
        b'{"mcpServers": {"apm-srv": ',
    ],
    ids=["empty", "whitespace-only", "jsonc-comments", "invalid-json"],
)
def test_unparseable_file_is_left_untouched(tmp_path: Path, writer: str, content: bytes):
    path = tmp_path / "mcp.json"
    path.write_bytes(content)

    assert WRITERS[writer](path, {"apm-srv"}, _Logger()) == 0

    assert path.read_bytes() == content


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_missing_file_is_not_created(tmp_path: Path, writer: str):
    path = tmp_path / "mcp.json"

    assert WRITERS[writer](path, {"apm-srv"}, _Logger()) == 0

    assert not path.exists()
