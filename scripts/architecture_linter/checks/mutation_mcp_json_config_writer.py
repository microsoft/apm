"""MCP JSON config files are written by one owner that keeps the user's style.

Guard ``hooks-integrations-mcp-json-config-writer`` (#4526): every write of a
user-editable MCP JSON config (``.github/mcp.json``, ``.mcp.json``,
``~/.claude.json``, the IntelliJ MCP config) goes through
``utils/json_config.py::write_json_config``, which keeps the file's
indentation, final newline, line endings and literal non-ASCII text. No
module under ``adapters/client/`` or ``install/mcp/``, and not
``integration/mcp_integrator.py``, serializes JSON for a file itself: a
``json.dump`` call, or a ``json.dumps`` call with ``indent=``, is a second
writer.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.mutation_write_shared import (
    GROUP,
    _has_fixed,
    _python_paths,
    _read_required,
    _require,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "mutation_writes.mcp_json_config_writer"
GUARD_ID = "hooks-integrations-mcp-json-config-writer"

_OWNER = "src/apm_cli/utils/json_config.py"
_INTEGRATOR = "src/apm_cli/integration/mcp_integrator.py"
_SCANNED_TREES = ("src/apm_cli/adapters/client/", "src/apm_cli/install/mcp/")
_OWNER_CALL = re.compile(r"(?<![\w.])write_json_config\(")
_WRITERS = (
    "src/apm_cli/adapters/client/base.py",
    "src/apm_cli/adapters/client/claude.py",
    "src/apm_cli/adapters/client/intellij.py",
    "src/apm_cli/install/mcp/retired_destinations.py",
    _INTEGRATOR,
)


def _serializer_calls(tree: ast.Module) -> Iterable[ast.Call]:
    """Yield ``json.dump(...)`` and ``json.dumps(..., indent=...)`` calls."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not (isinstance(receiver, ast.Name) and receiver.id == "json"):
            continue
        if node.func.attr == "dump" or (
            node.func.attr == "dumps" and any(kw.arg == "indent" for kw in node.keywords)
        ):
            yield node


def _check_mcp_json_config_writer(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every MCP JSON config write goes through utils/json_config.py."""
    scanned = sorted(
        {*(path for tree in _SCANNED_TREES for path in _python_paths(provider, under=tree))}
        | {_INTEGRATOR}
    )
    facts_by_path, failures = _read_required(
        provider, RULE_ID, tuple(sorted({_OWNER, *_WRITERS, *scanned}))
    )
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    owner = facts_by_path[_OWNER]
    for needle in ("def write_json_config(", "normalize_line_endings=False"):
        findings.extend(
            _require(
                _has_fixed(owner, needle),
                RULE_ID,
                _OWNER,
                f"the MCP JSON config owner must keep the file's style; missing: {needle}",
            )
        )
    for path in _WRITERS:
        findings.extend(
            _require(
                _OWNER_CALL.search(source_text(facts_by_path[path])) is not None,
                RULE_ID,
                path,
                "MCP JSON config writes must go through utils/json_config.py::write_json_config",
            )
        )
    for path in scanned:
        facts = facts_by_path[path]
        try:
            tree = ast.parse(source_text(facts))
        except SyntaxError as exc:
            findings.append(violation(RULE_ID, path, f"unparsable source: {exc.msg}"))
            continue
        for call in _serializer_calls(tree):
            if EXEMPT_MARKER in facts.lines[call.lineno - 1]:
                continue
            findings.append(
                violation(
                    RULE_ID,
                    path,
                    "serialize MCP JSON config through utils/json_config.py::write_json_config, "
                    "which keeps the user's indentation and final newline",
                    line=call.lineno,
                    column=call.col_offset + 1,
                )
            )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(GUARD_ID,),
        description="MCP JSON config files are written only by utils/json_config.py.",
        check=_check_mcp_json_config_writer,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
