"""The retired-destination heal keeps the user's .github/mcp.json style (#4526, G5).

``heal_retired_project_mcp_destinations`` removes APM's entry from a retired
``.github/mcp.json`` that also holds a user server. The rewrite must keep the
file's own style: indentation (at every depth), final newline, CRLF line
endings and literal or escaped non-ASCII text. Adding APM's entry to the
user's bytes and healing it out again is byte-identical to the user's bytes.
Expected bytes are hand-written; test sources stay ASCII.

The heal only ever removes APM's entries, so the steward's "update in place"
oracle has no cell for this writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.mcp.retired_destinations import heal_retired_project_mcp_destinations

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-copilot-cli-mcp-config-paths")]

_NOTES = {"name": "notes", "registry": False, "transport": "stdio", "command": "node"}
_E_ACUTE = "\u00e9"


def _user_file(indent: str, newline: str, label: str) -> str:
    """A user server nested three levels deep (mcpServers > mine > env)."""
    i1, i2, i3, i4 = indent, indent * 2, indent * 3, indent * 4
    lines = [
        "{",
        f'{i1}"mcpServers": {{',
        f'{i2}"mine": {{',
        f'{i3}"command": "{label}",',
        f'{i3}"env": {{',
        f'{i4}"LEVEL": "3"',
        f"{i3}}}",
        f"{i2}}}",
        f"{i1}}}",
        "}",
    ]
    return newline.join(lines)


def _with_apm_entry(indent: str, newline: str, label: str) -> str:
    """The same file after 0.36.1 appended APM's ``notes`` entry."""
    i1, i2, i3, i4 = indent, indent * 2, indent * 3, indent * 4
    lines = [
        "{",
        f'{i1}"mcpServers": {{',
        f'{i2}"mine": {{',
        f'{i3}"command": "{label}",',
        f'{i3}"env": {{',
        f'{i4}"LEVEL": "3"',
        f"{i3}}}",
        f"{i2}}},",
        f'{i2}"notes": {{',
        f'{i3}"type": "local",',
        f'{i3}"tools": [',
        f'{i4}"*"',
        f"{i3}],",
        f'{i3}"id": "",',
        f'{i3}"command": "node",',
        f'{i3}"args": []',
        f"{i2}}}",
        f"{i1}}}",
        "}",
    ]
    return newline.join(lines)


# (indent, newline, final newline, user command label)
_STYLES = {
    "a-4-space-final-newline": ("    ", "\n", True, "user-server"),
    "b-tab-no-final-newline": ("\t", "\n", False, "user-server"),
    "c-crlf": ("  ", "\r\n", True, "user-server"),
    "d1-literal-non-ascii": ("  ", "\n", True, f"caf{_E_ACUTE}-server"),
    "d2-escaped-non-ascii": ("  ", "\n", True, "caf\\u00e9-server"),
}


def _bytes(text: str, newline: str, final: bool) -> bytes:
    return (text + (newline if final else "")).encode("utf-8")


def _heal(project: Path):
    return heal_retired_project_mcp_destinations(
        {"copilot": {"notes"}},
        stored_configs={"notes": _NOTES},
        native_hashes={},
        project_root=project,
    )


@pytest.mark.parametrize("style", sorted(_STYLES))
def test_heal_removing_apm_entry_returns_the_user_bytes(tmp_path: Path, style: str) -> None:
    indent, newline, final, label = _STYLES[style]
    user = _bytes(_user_file(indent, newline, label), newline, final)
    with_apm = _bytes(_with_apm_entry(indent, newline, label), newline, final)
    target = tmp_path / ".github" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(with_apm)

    heal = _heal(tmp_path)

    assert heal.removed == {"copilot": {"notes"}}
    assert heal.deleted_files == []
    assert target.read_bytes() == user


@pytest.mark.parametrize("style", sorted(_STYLES))
def test_heal_without_apm_entry_never_rewrites(tmp_path: Path, style: str) -> None:
    indent, newline, final, label = _STYLES[style]
    user = _bytes(_user_file(indent, newline, label), newline, final)
    target = tmp_path / ".github" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(user)
    before = target.stat().st_mtime_ns

    heal = _heal(tmp_path)

    assert heal.removed == {}
    assert target.read_bytes() == user
    assert target.stat().st_mtime_ns == before
