"""MCP JSON config writes keep the user's formatting (#4526).

Cells G2 (client adapter base, driven through the Copilot adapter), G3 (Claude
Code user scope, ``~/.claude.json``) and G4 (JetBrains Copilot). Each writer is
driven through its public entry point (``update_config``; G4 also
``remove_managed_servers``) on four user styles: 4-space indent with a final
newline, tab indent without one, CRLF line endings, and a non-ASCII user value
kept literal (d1) or kept escaped (d2).

Oracles are bytes. Expected documents are written by hand below as
``(depth, line)`` pairs; ``_render`` only applies the style's indent unit,
line ending and final newline, so no product serializer computes an oracle.
User keys stay in place and byte-identical, APM's entry follows the user's
style, a present APM entry is updated in place, a new one is appended, and an
unchanged write does not touch the file. New, minified, JSONC, empty,
whitespace-only and invalid files are pinned to today's behavior.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.intellij import IntelliJClientAdapter, IntelliJConfigError

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-copilot-cli-mcp-config-paths",
        "P-OWN-claude-mcp-config-paths",
        "P-OWN-jetbrains-copilot-mcp-config-path",
    ),
]

APM = "apm-srv"
OLD = {"command": "old", "type": "stdio"}
NEW = {"command": "node", "type": "stdio"}


@dataclass(frozen=True)
class _Style:
    indent: str
    eol: str
    final_newline: bool
    value: str  # the user's env value exactly as written in the file


STYLES = {
    "a-4space-final-newline": _Style("    ", "\n", True, '"v"'),
    "b-tab-no-final-newline": _Style("\t", "\n", False, '"v"'),
    "c-crlf": _Style("  ", "\r\n", True, '"v"'),
    "d1-literal-non-ascii": _Style("  ", "\n", True, '"caf\u00e9"'),
    "d2-escaped-non-ascii": _Style("  ", "\n", True, '"caf\\u00e9"'),
}

# The user's own server nests two levels below the servers map, so an indent
# detector that only looks at the top level fails.
_MINE = [
    (2, '"mine": {'),
    (3, '"command": "uvx",'),
    (3, '"env": {'),
    (4, '"K": VALUE'),
    (3, "}"),
    (2, "}"),
]


def _apm(command: str) -> list[tuple[int, str]]:
    return [
        (2, f'"{APM}": {{'),
        (3, f'"command": "{command}",'),
        (3, '"type": "stdio"'),
        (2, "}"),
    ]


def _document(*servers: list[tuple[int, str]]) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = [(0, "{"), (1, '"KEY": {')]
    for index, server in enumerate(servers):
        depth, last = server[-1]
        lines += [*server[:-1], (depth, last + ("," if index < len(servers) - 1 else ""))]
    return [*lines, (1, "},"), (1, '"theme": "dark"'), (0, "}")]


USER_ONLY = _document(_MINE)
APM_ADDED = _document(_MINE, _apm("node"))
APM_OLD_FIRST = _document(_apm("old"), _MINE)
APM_UPDATED_FIRST = _document(_apm("node"), _MINE)


def _render(lines: list[tuple[int, str]], style: _Style, key: str) -> bytes:
    text = style.eol.join(style.indent * depth + line for depth, line in lines)
    text = text.replace("KEY", key).replace("VALUE", style.value)
    return (text + (style.eol if style.final_newline else "")).encode("utf-8")


@dataclass(frozen=True)
class _Writer:
    make: Callable[[Path], object]
    key: str
    final_newline_today: bool


def _copilot(root: Path) -> CopilotClientAdapter:
    return CopilotClientAdapter(project_root=root)


def _claude_user(root: Path) -> ClaudeClientAdapter:
    return ClaudeClientAdapter(project_root=root, user_scope=True)


def _intellij(root: Path) -> IntelliJClientAdapter:
    return IntelliJClientAdapter(project_root=root)


WRITERS = {
    "g2-base-copilot": _Writer(_copilot, "mcpServers", False),
    "g3-claude-user": _Writer(_claude_user, "mcpServers", True),
    "g4-intellij": _Writer(_intellij, "servers", True),
}


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for variable in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(variable, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    root = tmp_path / "project"
    root.mkdir()
    return root


def _seed(writer: _Writer, root: Path, content: bytes | None) -> tuple[object, Path]:
    adapter = writer.make(root)
    path = Path(adapter.get_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_bytes(content)
    return adapter, path


_EVERY_CELL = pytest.mark.parametrize("style_id", list(STYLES))
_EVERY_WRITER = pytest.mark.parametrize("writer_id", list(WRITERS))


@_EVERY_WRITER
@_EVERY_CELL
def test_add_appends_apm_entry_in_the_user_style(
    isolated: Path, writer_id: str, style_id: str
) -> None:
    writer, style = WRITERS[writer_id], STYLES[style_id]
    adapter, path = _seed(writer, isolated, _render(USER_ONLY, style, writer.key))

    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == _render(APM_ADDED, style, writer.key)


@_EVERY_WRITER
@_EVERY_CELL
def test_update_changes_apm_entry_in_place_in_the_user_style(
    isolated: Path, writer_id: str, style_id: str
) -> None:
    writer, style = WRITERS[writer_id], STYLES[style_id]
    adapter, path = _seed(writer, isolated, _render(APM_OLD_FIRST, style, writer.key))

    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == _render(APM_UPDATED_FIRST, style, writer.key)


@_EVERY_WRITER
@_EVERY_CELL
def test_unchanged_update_does_not_write(isolated: Path, writer_id: str, style_id: str) -> None:
    writer, style = WRITERS[writer_id], STYLES[style_id]
    before = _render(APM_ADDED, style, writer.key)
    adapter, path = _seed(writer, isolated, before)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))

    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == 1_000_000_000


@_EVERY_CELL
def test_g4_add_then_remove_restores_the_user_bytes(isolated: Path, style_id: str) -> None:
    writer, style = WRITERS["g4-intellij"], STYLES[style_id]
    original = _render(USER_ONLY, style, writer.key)
    adapter, path = _seed(writer, isolated, original)

    adapter.update_config({APM: dict(NEW)})
    assert adapter.remove_managed_servers({APM}) == {APM}

    assert path.read_bytes() == original


# Pinned as-is: today's bytes for files whose style cannot be detected.
_NEW_ONLY = (
    b'{\n  "KEY": {\n    "apm-srv": {\n      "command": "node",\n'
    b'      "type": "stdio"\n    }\n  }\n}'
)
_MINIFIED_SEED = b'{"KEY":{"mine":{"command":"uvx"}}}'
_MINIFIED_AFTER = (
    b'{\n  "KEY": {\n    "mine": {\n      "command": "uvx"\n    },\n'
    b'    "apm-srv": {\n      "command": "node",\n      "type": "stdio"\n    }\n  }\n}'
)
_JSONC_SEED = b'{\n  // c\n  "KEY": {"mine": {"command": "uvx"}}\n}\n'


def _pinned(content: bytes, writer: _Writer) -> bytes:
    content = content.replace(b"KEY", writer.key.encode("ascii"))
    return content + (b"\n" if writer.final_newline_today else b"")


@_EVERY_WRITER
@pytest.mark.parametrize("seed", [None, b""], ids=["new-file", "empty-file"])
def test_pinned_new_or_empty_file_gets_todays_default_style(
    isolated: Path, writer_id: str, seed: bytes | None
) -> None:
    writer = WRITERS[writer_id]
    adapter, path = _seed(writer, isolated, seed)

    if writer_id == "g4-intellij" and seed is not None:
        with pytest.raises(IntelliJConfigError, match="is malformed JSON"):
            adapter.update_config({APM: dict(NEW)})
        assert path.read_bytes() == seed
        return
    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == _pinned(_NEW_ONLY, writer)


@_EVERY_WRITER
def test_pinned_whitespace_only_file(isolated: Path, writer_id: str) -> None:
    writer = WRITERS[writer_id]
    adapter, path = _seed(writer, isolated, b"  \n")

    if writer_id == "g4-intellij":
        with pytest.raises(IntelliJConfigError) as raised:
            adapter.update_config({APM: dict(NEW)})
        assert str(raised.value) == (
            f"JetBrains Copilot MCP config at '{path}' is malformed JSON (Expecting value: "
            "line 2 column 1 (char 3)). Fix the file, then run 'apm install --target "
            "intellij' to reconcile the JetBrains Copilot MCP config."
        )
        assert path.read_bytes() == b"  \n"
        return
    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == _pinned(_NEW_ONLY, writer)


@_EVERY_WRITER
def test_pinned_minified_file_is_expanded_to_todays_default_style(
    isolated: Path, writer_id: str
) -> None:
    writer = WRITERS[writer_id]
    adapter, path = _seed(writer, isolated, _MINIFIED_SEED.replace(b"KEY", writer.key.encode()))

    adapter.update_config({APM: dict(NEW)})

    assert path.read_bytes() == _pinned(_MINIFIED_AFTER, writer)


@_EVERY_WRITER
def test_pinned_jsonc_file(isolated: Path, writer_id: str) -> None:
    writer = WRITERS[writer_id]
    seed = _JSONC_SEED.replace(b"KEY", writer.key.encode())
    adapter, path = _seed(writer, isolated, seed)

    result = adapter.update_config({APM: dict(NEW)})

    if writer_id == "g4-intellij":
        # JetBrains reads JSONC; the rewrite drops the comment (out of scope, #4526).
        assert path.read_bytes() == _pinned(_MINIFIED_AFTER, writer)
        return
    assert result is False
    assert path.read_bytes() == seed


@_EVERY_WRITER
def test_pinned_invalid_json_file_is_never_rewritten(isolated: Path, writer_id: str) -> None:
    writer = WRITERS[writer_id]
    seed = b'{"KEY": '.replace(b"KEY", writer.key.encode())
    adapter, path = _seed(writer, isolated, seed)

    if writer_id == "g4-intellij":
        with pytest.raises(IntelliJConfigError) as raised:
            adapter.update_config({APM: dict(NEW)})
        assert str(raised.value) == (
            f"JetBrains Copilot MCP config at '{path}' is malformed JSON (Expecting value: "
            "line 1 column 13 (char 12)). Fix the file, then run 'apm install --target "
            "intellij' to reconcile the JetBrains Copilot MCP config."
        )
    else:
        assert adapter.update_config({APM: dict(NEW)}) is False
    assert path.read_bytes() == seed
