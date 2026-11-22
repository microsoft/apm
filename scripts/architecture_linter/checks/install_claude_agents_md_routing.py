"""Keep Claude AGENTS.md instruction routing on one owner (#3939).

``compilation/claude_agents_md.py`` alone decides which project instructions
Claude Code loads from ``AGENTS.md`` (``routes_to_agents_md``) and whether a
hand-authored root file blocks that route (``claude_agents_md_blocker``).
Install must filter ``.claude/rules/`` through both, compile must drop
instructions ``.claude/rules/`` already delivers through the owner, and the
generated ``CLAUDE.md`` must be the ``@AGENTS.md`` import, so install and
compile cannot drift apart about where an instruction lives.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _facts_for,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_CLAUDE_AGENTS_MD_ROUTING = "install-deployment-claude-agents-md-routing"
_OWNER = "src/apm_cli/compilation/claude_agents_md.py"
_OWNER_FUNCTIONS = ("routes_to_agents_md", "claude_agents_md_blocker", "directory_scope")
_CONTRACTS = (
    (
        "src/apm_cli/integration/instruction_integrator.py",
        "InstructionIntegrator.integrate_instructions_for_target",
        "self._claude_rule_sources",
    ),
    (
        "src/apm_cli/integration/instruction_integrator.py",
        "InstructionIntegrator._claude_rule_sources",
        "routes_to_agents_md",
    ),
    (
        "src/apm_cli/integration/instruction_integrator.py",
        "InstructionIntegrator._claude_rule_sources",
        "claude_agents_md_blocker",
    ),
    (
        "src/apm_cli/compilation/agents_compiler.py",
        "AgentsCompiler._compile_agents_md",
        "without_claude_rule_instructions",
    ),
    (
        "src/apm_cli/compilation/agents_compiler.py",
        "AgentsCompiler._compile_agents_md",
        "without_directory_instructions",
    ),
    (
        "src/apm_cli/compilation/agents_compiler.py",
        "AgentsCompiler._compile_distributed",
        "pin_directory_placements",
    ),
    (
        "src/apm_cli/compilation/agents_compiler.py",
        "AgentsCompiler._compile_claude_md",
        "claude_formatter.format_agents_md_imports",
    ),
)
_ROUTING_CONSUMER = "src/apm_cli/integration/instruction_integrator.py"
_ROUTING_FUNCTION = "InstructionIntegrator._claude_rule_sources"


def _calls(index, function: ast.AST) -> set[str]:
    return {ast.unparse(node.func) for node in index.walk(function) if isinstance(node, ast.Call)}


def _routing_filter_is_owner_verdict(index, function: ast.AST) -> bool:
    """The routed set keeps exactly the sources the owner's verdict selects.

    Any widening (``routes_to_agents_md(...) or True``) or narrowing in the
    consumer changes the comprehension's single filter and fails this check.
    """
    for node in index.walk(function):
        if not isinstance(node, ast.Assign) or [ast.unparse(t) for t in node.targets] != ["routed"]:
            continue
        value = node.value
        if not isinstance(value, ast.SetComp) or len(value.generators) != 1:
            return False
        conditions = value.generators[0].ifs
        return (
            len(conditions) == 1
            and isinstance(conditions[0], ast.Call)
            and ast.unparse(conditions[0].func) == "routes_to_agents_md"
            and len(conditions[0].args) == 2
            and ast.unparse(conditions[0].args[1]) == "live_root"
        )
    return False


def check_claude_agents_md_routing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Claude instruction routing is decided once and consumed on both sides."""
    rule = GUARD_CLAUDE_AGENTS_MD_ROUTING
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        facts, failures = _facts_for(provider, path, rule)
        if failures:
            findings.extend(failures)
            continue
        names = [
            definition.name
            for definition in getattr(facts, "definitions", ())
            if definition.kind in ("function", "async_function")
            and definition.name in _OWNER_FUNCTIONS
        ]
        expected = len(_OWNER_FUNCTIONS) if path == _OWNER else 0
        if len(names) != expected or len(set(names)) != len(names):
            findings.append(
                _summary(
                    rule,
                    path,
                    "Claude AGENTS.md routing must be defined once in "
                    f"{_OWNER} ({', '.join(_OWNER_FUNCTIONS)})",
                )
            )
    for path, qualname, call in _CONTRACTS:
        _facts, failures = _facts_for(provider, path, rule)
        if failures:
            findings.extend(failures)
            continue
        index = provider.tree_index(path)
        function = index.function(qualname) if index is not None else None
        if function is None or call not in _calls(index, function):
            findings.append(
                _summary(rule, path, f"{qualname} must route Claude instructions via {call}()")
            )
    index = provider.tree_index(_ROUTING_CONSUMER)
    function = index.function(_ROUTING_FUNCTION) if index is not None else None
    if function is None or not _routing_filter_is_owner_verdict(index, function):
        findings.append(
            _summary(
                rule,
                _ROUTING_CONSUMER,
                f"{_ROUTING_FUNCTION} must route exactly the sources "
                "routes_to_agents_md(applyTo, live_root) selects",
            )
        )
    return tuple(findings)
