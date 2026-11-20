"""Console stream contract: error lines on stderr, piped output never hard-wrapped.

``utils/console.py`` (owner ``console-construction``) decides which stream a
line goes to and whether it wraps. ``_rich_echo`` routes every error line (red,
or the ``error`` symbol) to stderr and soft-wraps whenever its console is not a
terminal. This guard pins both decisions in the owner and closes the world of
error-line emitters: a ``[x]`` line (literal or ``STATUS_SYMBOLS`` error/cross)
printed anywhere else in ``src/apm_cli`` must go through the owner or name
stderr explicitly, unless its enclosing function is on the shrink-only
allowlist below.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import GROUP, _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_CONSOLE_STREAMS = "registry_delegation.console_streams"
GUARD_CONSOLE_STREAMS = "registry-delegation-console-streams"
_CONSOLE = "src/apm_cli/utils/console.py"
_SRC = "src/apm_cli/"
_OWNER_DECISIONS = (
    'err = err or color == "red" or symbol == "error"',
    "soft_wrap = soft_wrap or not console.is_terminal",
)
_ERROR_SYMBOLS = frozenset({"error", "cross"})

# (path, enclosing function) -> reason. Only shrinks.
_ALLOWED_STDOUT_ERROR_ROWS: dict[tuple[str, str], str] = {
    ("src/apm_cli/integration/mcp_integrator_install.py", "_install_registry_group"): (
        "per-server row of the install progress tree; the failure is reported on stderr"
    ),
    ("src/apm_cli/integration/mcp_integrator_install.py", "_install_self_defined_deps"): (
        "per-server row of the install progress tree; the failure is reported on stderr"
    ),
}


def _names_error(node: ast.AST, index) -> bool:
    for item in index.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str) and "[x]" in item.value:
            return True
        if (
            isinstance(item, ast.Subscript)
            and isinstance(item.value, ast.Name)
            and item.value.id == "STATUS_SYMBOLS"
            and isinstance(item.slice, ast.Constant)
            and item.slice.value in _ERROR_SYMBOLS
        ):
            return True
    return False


def _keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.unparse(keyword.value)
    return None


def _routes_to_stderr(call: ast.Call, callee: str) -> bool:
    if _keyword(call, "err") == "True" or (_keyword(call, "file") or "").endswith("stderr"):
        return True
    if callee.endswith("_rich_error"):
        return True
    if callee.endswith("_rich_echo"):
        return _keyword(call, "color") == "'red'" or _keyword(call, "symbol") == "'error'"
    return False


def _is_output_call(callee: str) -> bool:
    return callee == "print" or callee.endswith(("echo", ".print", "_rich_error"))


def _enclosing_function(facts, line: int) -> str:
    best = ""
    best_start = 0
    for definition in facts.definitions:
        if definition.kind == "class":
            continue
        if definition.line <= line <= definition.end_line and definition.line >= best_start:
            best, best_start = definition.name, definition.line
    return best


def _owner_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    facts, failures = checked_facts(provider, _CONSOLE, rule_id, require_python=True)
    if failures:
        return list(failures)
    lines = tuple(line.strip() for line in facts.lines)
    return [
        violation(rule_id, _CONSOLE, f"_rich_echo must keep the stream decision: {decision}")
        for decision in _OWNER_DECISIONS
        if decision not in lines
    ]


def _emitter_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    findings: list[Violation] = []
    used: set[tuple[str, str]] = set()
    for path in _python_paths(provider, under=_SRC):
        if path == _CONSOLE:
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        index = facts.tree_index
        if index is None:
            continue
        for node in index.nodes:
            if not isinstance(node, ast.Call):
                continue
            callee = ast.unparse(node.func)
            if not _is_output_call(callee) or _routes_to_stderr(node, callee):
                continue
            if not any(_names_error(argument, index) for argument in node.args):
                continue
            key = (path, _enclosing_function(facts, node.lineno))
            if key in _ALLOWED_STDOUT_ERROR_ROWS:
                used.add(key)
                continue
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"error line printed by {callee} bypasses the console stream owner; "
                    "use _rich_error or _rich_echo(color='red') so it goes to stderr",
                    line=node.lineno,
                )
            )
    findings.extend(
        violation(rule_id, path, f"stale stdout error-row allowance for {function}")
        for path, function in sorted(set(_ALLOWED_STDOUT_ERROR_ROWS) - used)
    )
    return findings


def check_console_streams(provider: FactsProvider) -> Iterable[Violation]:
    """Error lines reach stderr through the console owner; piped output never wraps."""
    rule_id = RULE_CONSOLE_STREAMS
    return tuple(_owner_findings(provider, rule_id) + _emitter_findings(provider, rule_id))


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_CONSOLE_STREAMS,
        group=GROUP,
        guard_ids=(GUARD_CONSOLE_STREAMS,),
        description="Error lines go to stderr via utils/console.py; piped output never wraps.",
        check=check_console_streams,
    ),
)
