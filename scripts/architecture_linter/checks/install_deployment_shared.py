"""Shared, side-effect-free helpers for install/deployment analyzers.

Every helper here is used by two or more of the cohesive check-family
modules under this catalog; splitting them out avoids duplicating the same
lexical/AST primitives in every family module.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

GROUP = "install_deployment"


_SRC_PREFIX = "src/apm_cli/"


def _facts_for(provider: FactsProvider, path: str, rule_id: str):
    """Return ``(facts, failures)`` for one Python owner/consumer file."""
    return checked_facts(provider, path, rule_id, require_python=path.endswith(".py"))


def _lines(facts: object) -> tuple[str, ...]:
    """Return the cached lexical lines for a file."""
    return getattr(facts, "lines", ())


def _present(facts: object, needle: str) -> bool:
    """Return whether any single line contains `needle` (literal, grep -q)."""
    return any(needle in line for line in _lines(facts))


def _present_re(facts: object, pattern: re.Pattern[str]) -> bool:
    """Return whether any single line matches `pattern` (grep -Eq)."""
    return any(pattern.search(line) is not None for line in _lines(facts))


def _first_line_re(facts: object, pattern: re.Pattern[str]) -> int | None:
    """Return the 1-based line number of the first regex match, else None."""
    for number, line in enumerate(_lines(facts), start=1):
        if pattern.search(line) is not None:
            return number
    return None


def _awk_body(
    facts: object,
    start: re.Pattern[str],
    boundary: re.Pattern[str],
    keep: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    """Extract a function/class body like the shell's block-capture awk.

    Capture begins on the first line matching `start` (inclusive) and ends
    just before the next line matching `boundary` that does not also match
    `keep` -- exactly the ``/start/{flag=1} flag&&/boundary/&&!/keep/{exit}``
    idiom used throughout the legacy script. `keep` defaults to `start`, which
    covers every block whose negation repeats the opening signature.
    """
    keep_pattern = keep if keep is not None else start
    body: list[str] = []
    capturing = False
    for line in _lines(facts):
        if not capturing:
            if start.search(line) is not None:
                capturing = True
                body.append(line)
            continue
        if boundary.search(line) is not None and keep_pattern.search(line) is None:
            break
        body.append(line)
    return tuple(body)


def _body_has(body: Sequence[str], needle: str) -> bool:
    """Return whether any captured body line contains `needle`."""
    return any(needle in line for line in body)


def _python_paths(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    """Return every inventory Python path under `prefix`, in inventory order."""
    return tuple(
        path for path in provider.inventory if path.startswith(prefix) and path.endswith(".py")
    )


def _duplicate_definition_lines(
    provider: FactsProvider,
    *,
    rule_id: str,
    prefix: str,
    pattern: re.Pattern[str],
    owner: str,
    message: str,
    respect_exempt: bool,
) -> list[Violation]:
    """Flag every definition matching `pattern` outside the canonical `owner`.

    Mirrors ``grep -rEn PATTERN prefix | grep -v owner: | grep -v exempt``.
    """
    findings: list[Violation] = []
    for path in _python_paths(provider, prefix):
        if path == owner:
            continue
        facts = provider.file_facts(path)
        if getattr(facts, "read_error", None) is not None:
            continue
        for number, line in enumerate(_lines(facts), start=1):
            if respect_exempt and EXEMPT_MARKER in line:
                continue
            match = pattern.search(line)
            if match is not None:
                findings.append(
                    violation(rule_id, path, message, line=number, column=match.start() + 1)
                )
    return findings


def _all_names(index: TreeIndex, node: ast.AST) -> set[str]:
    """Return every Name id referenced within `node`, any context."""
    return {item.id for item in index.walk(node) if isinstance(item, ast.Name)}


def _literal_string(node: ast.AST) -> str | None:
    """Return the string value of a string constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _count_re(facts: object, pattern: re.Pattern[str]) -> int:
    """Return how many lines match `pattern` (grep -Ec)."""
    return sum(1 for line in _lines(facts) if pattern.search(line) is not None)


def _summary(rule_id: str, path: str, message: str) -> Violation:
    """Return a single owner-attributed summary violation."""
    return violation(rule_id, path, message, line=1)


_INSTALL_ADAPTER = "src/apm_cli/commands/install.py"


_UNINSTALL_ENGINE = "src/apm_cli/commands/uninstall/engine.py"


def _name_calls_in(facts: object, function_name: str) -> set[str]:
    """Return bare-name call ids lexically inside every function `function_name`.

    A call at line L belongs to function F when F's ``[line, end_line]`` range
    contains L -- which is exactly ``ast.walk(function_node)`` subtree
    membership, so this reproduces the legacy ``_function_calls`` map without a
    second traversal.
    """
    ranges = [
        (definition.line, definition.end_line)
        for definition in getattr(facts, "definitions", ())
        if definition.name == function_name and definition.kind in ("function", "async_function")
    ]
    if not ranges:
        return set()
    names: set[str] = set()
    for call in getattr(facts, "calls", ()):
        qualname = call.qualname
        if "." in qualname or not qualname.isidentifier():
            continue
        if any(low <= call.line <= high for low, high in ranges):
            names.add(qualname)
    return names


def _line_findings(
    facts: object,
    path: str,
    rule_id: str,
    pattern: re.Pattern[str],
    message: str,
    *,
    respect_exempt: bool,
) -> list[Violation]:
    """Report every matching line in one file (check_pattern semantics)."""
    findings: list[Violation] = []
    for number, line in enumerate(_lines(facts), start=1):
        if respect_exempt and EXEMPT_MARKER in line:
            continue
        match = pattern.search(line)
        if match is not None:
            findings.append(
                violation(rule_id, path, message, line=number, column=match.start() + 1)
            )
    return findings


def _rule(guard_id: str, description: str, check) -> Rule:
    """Build one owner rule whose id and single guard id are the guard id."""
    return Rule(
        id=guard_id,
        group=GROUP,
        guard_ids=(guard_id,),
        description=description,
        check=check,
    )
