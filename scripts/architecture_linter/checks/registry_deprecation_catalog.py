"""Deprecation warning text is spelled only in the deprecation catalog (#4019).

``src/apm_cli/core/deprecations.py`` owns every deprecation's warning
template. Call sites read the words through ``deprecation_message(key)``; a
string literal elsewhere in ``src/apm_cli`` that repeats a catalog template's
literal text is a second authority for the same words and is rejected.

Templates are read statically from the catalog's ``Deprecation(template=...)``
calls (an f-string template contributes its literal parts) and split at their
``{field}`` placeholders, so an f-string copy of a template is caught piece by
piece. Fragments shorter than
``_MIN_FRAGMENT_LENGTH`` are punctuation glue and are not matched.
"""

from __future__ import annotations

import ast
import string

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Violation

DEPRECATION_CATALOG = "src/apm_cli/core/deprecations.py"
_SCANNED_PREFIX = "src/apm_cli/"
_ENTRY_CALL = "Deprecation"
_MIN_FRAGMENT_LENGTH = 16


def _keyword_text(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if (
            keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _template_parts(call: ast.Call) -> tuple[str, ...]:
    """Return a template's literal text; an f-string contributes its literal parts."""
    for keyword in call.keywords:
        if keyword.arg != "template":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return (value.value,)
        if isinstance(value, ast.JoinedStr):
            return tuple(
                part.value
                for part in value.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
    return ()


def _template_fragments(nodes) -> dict[str, str]:
    """Map each matchable literal fragment of a catalog template to its key."""
    fragments: dict[str, str] = {}
    for node in nodes:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _ENTRY_CALL
        ):
            continue
        key = _keyword_text(node, "key")
        if key is None:
            continue
        for template in _template_parts(node):
            for literal, _field, _spec, _conversion in string.Formatter().parse(template):
                # Spacing around a placeholder is not part of the words.
                literal = literal.strip()
                if len(literal) >= _MIN_FRAGMENT_LENGTH:
                    fragments[literal] = key
    return fragments


def check_deprecation_catalog_text(
    provider: FactsProvider,
    rule_id: str,
) -> tuple[Violation, ...]:
    """Reject deprecation template text spelled outside the catalog."""
    findings: list[Violation] = []
    catalog, failures = checked_facts(provider, DEPRECATION_CATALOG, rule_id, require_python=True)
    findings.extend(failures)
    if catalog.tree_index is None:
        return tuple(findings)
    fragments = _template_fragments(catalog.tree_index.nodes)
    if not fragments:
        findings.append(
            violation(
                rule_id,
                DEPRECATION_CATALOG,
                f"no {_ENTRY_CALL}(key=..., template=...) entries found; the catalog "
                "must spell every deprecation's warning text",
                line=1,
            )
        )
        return tuple(findings)
    shortest = min(len(fragment) for fragment in fragments)

    for path in provider.inventory:
        if (
            path == DEPRECATION_CATALOG
            or not path.endswith(".py")
            or not path.startswith(_SCANNED_PREFIX)
        ):
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if facts.tree_index is None:
            continue
        for node in facts.tree_index.nodes:
            if not (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) >= shortest
            ):
                continue
            key = next(
                (owner for fragment, owner in fragments.items() if fragment in node.value),
                None,
            )
            if key is None:
                continue
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"deprecation text duplicates {DEPRECATION_CATALOG}; "
                    f"use deprecation_message({key!r})",
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
    return tuple(findings)


__all__ = ["DEPRECATION_CATALOG", "check_deprecation_catalog_text"]
