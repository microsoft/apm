"""Every ``apm <command> [flags]`` literal APM prints resolves to a real command.

CLI-surface truth (steward condition, #4090): a hint, help text or remediation
that names an ``apm`` command must name a subcommand that exists, and only
flags that command accepts, or the user is sent nowhere. The scan walks every
string literal under ``src/apm_cli`` (f-strings are flattened with ``{}`` for
each placeholder) and resolves each command line through
:mod:`tests.utils.click_tree`.

- A quoted command (``'apm ...'``, ``"apm ..."`` or a backtick span) is checked
  in full: subcommand path and flags.
- An unquoted ``apm <word> ...`` is checked only when ``<word>`` is a top-level
  command, so prose such as "apm package" is not mistaken for a command.

There is no baseline: a non-resolving literal fails.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.utils.click_tree import iter_commands, resolve

pytestmark = pytest.mark.component

_SRC = Path(__file__).resolve().parents[2] / "src" / "apm_cli"
_QUOTED = re.compile(r"(?:(?<=['\"])|(?<=`))apm ([^'\"`\n]+?)(?=['\"`])")
_BARE = re.compile(r"(?<![\w./`'\"-])apm ((?:[a-z][\w-]*)(?: (?:[a-z][\w-]*|--?[\w-]+(?:=\S+)?))*)")
_PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\.\.\.)$")


def _literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Return printed string literals: docstrings and bare strings are skipped."""
    joined_parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    joined_parts |= {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant | ast.JoinedStr)
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in joined_parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            found.append((node.lineno, text))
    return found


def _argv(command: str) -> list[str]:
    """Split a printed command into argv, stopping where the command line ends."""
    argv: list[str] = []
    for token in command.replace(",", " ").split():
        if token in ("--", "|", "&&", ";") or token.startswith("#"):
            break
        argv.append(token.rstrip(".;)"))
        if token.endswith(":"):  # "apm lifecycle: edit ..." -- prose follows
            argv[-1] = token.rstrip(":")
            break
    while argv and _PLACEHOLDER.match(argv[-1]):
        argv.pop()
    return [token for token in argv if token and not _PLACEHOLDER.match(token)]


def _alternatives(argv: list[str]) -> list[list[str]]:
    """Expand 'a|b' words and '--x/--y' flags into one argv per alternative."""
    expanded: list[list[str]] = [[]]
    for token in argv:
        if "|" in token:
            choices = token.split("|")
        elif token.startswith("--") and "/--" in token:
            choices = token.split("/")
        else:
            choices = [token]
        expanded = [[*prefix, choice] for prefix in expanded for choice in choices]
    return expanded


def _command_lines() -> list[tuple[str, int, str, bool]]:
    top_level = {path[0] for path, _command in iter_commands() if len(path) == 1}
    lines: list[tuple[str, int, str, bool]] = []
    for file in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        rel = file.relative_to(_SRC.parents[1]).as_posix()
        for lineno, text in _literals(tree):
            quoted = {match.start(1) for match in _QUOTED.finditer(text)}
            if "was removed" in text or "removed in" in text:
                continue  # a removal stub names the command it replaces
            for match in _QUOTED.finditer(text):
                lines.append((rel, lineno, match.group(1), True))
            for match in _BARE.finditer(text):
                if match.start(1) in quoted:
                    continue
                if match.group(1).split()[0] in top_level:
                    lines.append((rel, lineno, match.group(1), False))
    return lines


def test_every_apm_command_literal_resolves() -> None:
    broken: list[str] = []
    for rel, lineno, command, quoted in _command_lines():
        argv = _argv(command)
        if not argv:
            continue
        resolutions = [resolve(alternative) for alternative in _alternatives(argv)]
        failed = [resolution for resolution in resolutions if not resolution.ok]
        if not failed:
            continue
        resolution = failed[0]
        if resolution.unknown_command and not quoted:
            continue  # unquoted 'apm <cmd>' followed by prose
        problem = (
            f"unknown command {resolution.unknown_command!r}"
            if resolution.unknown_command
            else f"unknown flag(s) {', '.join(resolution.unknown_options)}"
        )
        broken.append(f"{rel}:{lineno}: 'apm {command}' -- {problem}")
    assert not broken, "apm command literals that do not resolve:\n" + "\n".join(broken)
