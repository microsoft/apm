"""Shared, side-effect-free helpers for marketplace/integration analyzers.

Used by two or more of the cohesive check-family modules under this
catalog; splitting them out avoids duplicating the same lexical/AST
primitives in every family module.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.lexical_shared import (
    count_contracts,
    count_regex,
    forbid_scan,
    load_required,
    paths_under,
    require_literals,
    require_regexes,
    source_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import DefinitionFact, FileFacts

GROUP = "marketplace_integrations"

_count_checks = count_contracts
_count_re = count_regex
_forbid_scan = forbid_scan
_load = load_required
_paths_under = paths_under
_require_res = require_regexes
_require_subs = require_literals
_src_python = source_python_paths


_PY: tuple[str, ...] = (".py",)


def _definition(facts: FileFacts, name: str) -> DefinitionFact | None:
    for definition in reversed(facts.definitions):
        if definition.name == name:
            return definition
    return None


def _def_body_text(facts: FileFacts, name: str) -> str:
    """Return the joined source of the last recorded ``name`` definition."""
    definition = _definition(facts, name)
    if definition is None:
        return ""
    return "\n".join(facts.lines[definition.line - 1 : definition.end_line])


def _ast_definition_body(facts: FileFacts, definition: ast.AST) -> str:
    """Return source text for one AST definition."""
    line = _definition_line(definition)
    end_line = max(getattr(definition, "end_lineno", line) or line, line)
    return "\n".join(facts.lines[line - 1 : end_line])


def _count_calls_named(facts: FileFacts, name: str) -> int:
    """Count call expressions whose terminal callee name is ``name``."""
    total = 0
    for call in facts.calls:
        terminal = call.qualname.rsplit(".", 1)[-1]
        if terminal == name:
            total += 1
    return total


def _count_calls_qualified(facts: FileFacts, qualname: str) -> int:
    """Count calls to one exact AST-derived dotted callable name."""
    return sum(1 for call in facts.calls if call.qualname == qualname)


def _subdir_python(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    return _paths_under(provider, prefix, _PY)


_PLUGIN_PARSER = "src/apm_cli/deps/plugin_parser.py"


_PROJECTION = "src/apm_cli/agent_plugins/projection.py"


_VALIDATION = "src/apm_cli/models/validation.py"


def _definition_line(node: ast.AST | None) -> int:
    return max(getattr(node, "lineno", 1), 1)
