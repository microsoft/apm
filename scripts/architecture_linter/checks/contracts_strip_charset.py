"""Multi-character strip arguments are character sets, never prefixes or suffixes.

``str.strip/lstrip/rstrip`` treat their argument as a set of characters, so
``name.rstrip(".git")`` also eats a trailing ``t``, ``i`` or ``g`` (#3715) and
``path.lstrip("./")`` eats the dot of ``.hidden``. Ruff B005 only flags
literals with duplicate characters, so it misses both. Literal prefix/suffix
removal must use ``removeprefix``/``removesuffix``; a deliberate character set
of two or more characters must be listed in ``_CHARSET_ALLOWLIST``.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Violation

_SCANNED_PREFIXES = ("src/apm_cli/", "scripts/")
_STRIP_METHODS = frozenset({"strip", "lstrip", "rstrip"})
_CHARSET_ALLOWLIST: frozenset[str | bytes] = frozenset(
    {
        "[]",
        "[]'\" ",
        "\"'",
        "'\"",
        "\r\n",
        "\n\r",
        b"\x00\n",
        "vV",
        "<>=^~",
        ".-_",
        "-._",
        "._-",
    }
)


def _suspicious_strip_literal(node: ast.AST) -> str | bytes | None:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _STRIP_METHODS
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
    ):
        return None
    value = node.args[0].value
    if not isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    return None if value in _CHARSET_ALLOWLIST else value


def check_strip_charset_literals(
    provider: FactsProvider,
    rule_id: str,
) -> tuple[Violation, ...]:
    """Reject non-allowlisted multi-character strip literals in shipped code."""
    findings: list[Violation] = []
    for path in provider.inventory:
        if not path.endswith(".py") or not path.startswith(_SCANNED_PREFIXES):
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if facts is None or facts.tree_index is None:
            continue
        for node in facts.tree_index.nodes:
            literal = _suspicious_strip_literal(node)
            if literal is None:
                continue
            findings.append(
                violation(
                    rule_id,
                    path,
                    f".{node.func.attr}({literal!r}) strips a character set, not a "
                    "prefix/suffix; use removeprefix/removesuffix, or allowlist a "
                    "deliberate character set in contracts_strip_charset.py",
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
    return tuple(findings)


__all__ = ["check_strip_charset_literals"]
