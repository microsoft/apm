"""Stable source anchors for rule evidence (#4696).

Evidence cites a SYMBOL, never a line number: ``path::qualname`` for Python
(a function, class or method with its decorators, or a module-level
assignment) and ``path::heading`` for Markdown (the section up to the next
heading of the same or higher level). An empty symbol is the whole file. A
line number moves whenever the cited file changes, so two unrelated PRs that
refresh it conflict in a merge train; a symbol moves only when the code it
names does.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A file name, a colon and a line number (or range): a citation that moves with every edit.
LINE_CITATION = re.compile(r"\b[\w./-]+\.(?:py|md|mdx|json|ya?ml|toml):\d+")


def _python_symbols(text: str) -> dict[str, tuple[int, int]]:
    spans: dict[str, tuple[int, int]] = {}

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                spans.setdefault(name, (start, node.end_lineno or node.lineno))
                walk(node.body, name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        end = node.end_lineno or node.lineno
                        spans.setdefault(prefix + target.id, (node.lineno, end))

    walk(ast.parse(text).body, "")
    return spans


def _markdown_symbols(lines: list[str]) -> dict[str, tuple[int, int]]:
    heads: list[tuple[int, int, str]] = []
    fence = ""
    for number, line in enumerate(lines, 1):
        marker = _FENCE.match(line)
        if marker and (not fence or marker.group(1).startswith(fence)):
            # A fence closes only on the same character, at least as long.
            fence = "" if fence else marker.group(1)
        elif not fence and (match := _HEADING.match(line)):
            heads.append((number, len(match.group(1)), match.group(2)))
    spans: dict[str, tuple[int, int]] = {}
    for index, (start, level, title) in enumerate(heads):
        end = next((n - 1 for n, lvl, _ in heads[index + 1 :] if lvl <= level), len(lines))
        spans.setdefault(title, (start, end))
    return spans


@cache
def symbols(path: str, root: Path = ROOT) -> dict[str, tuple[int, int]]:
    """Every anchorable symbol of ``path`` -> its 1-based inclusive line span."""
    text = (root / path).read_text(encoding="utf-8")
    lines = text.splitlines()
    found = _python_symbols(text) if path.endswith(".py") else _markdown_symbols(lines)
    return {"": (1, len(lines)), **found}
