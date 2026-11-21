"""Agent deployment renders first, then adopts through one owner (#3722).

``BaseIntegrator._check_adopt_or_skip(..., expected_content=...)`` owns the
adopt-or-skip decision. Every agent writer (Codex TOML, Kiro filtered
markdown, verbatim copy with rewritten links) renders the exact text first
and hands it to ``AgentIntegrator._deploy_rendered_agent``, which asks the
owner and then writes that same string. A raw-source comparison
(``is_content_identical_to_source``), an inline ``read_bytes()`` compare, or a
bare ``check_collision`` in ``agent_integrator.py`` re-derives the decision
against the wrong bytes and reports APM's own earlier output as skipped.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_AGENT_RENDER_ADOPT = "install-deployment-agent-render-then-adopt"
_CONSUMER = "src/apm_cli/integration/agent_integrator.py"
_DEPLOY = "AgentIntegrator._deploy_rendered_agent"
_OWNER_METHOD = "_check_adopt_or_skip"
# Adoption/collision primitives that bypass the owner when called directly.
_BYPASS_CALLS = frozenset(
    {
        "is_content_identical_to_source",
        "is_content_identical_to_text",
        "try_adopt_identical",
        "check_collision",
        "read_bytes",
        "_read_bytes_no_follow",
    }
)
# Adoption-free writers kept only for direct (test / spec) callers.
_UNGUARDED_WRITERS = frozenset({"copy_agent", "_write_codex_agent"})
_WRITE_ALLOWED_IN = frozenset(
    {_DEPLOY, "AgentIntegrator.copy_agent", "AgentIntegrator._write_codex_agent"}
)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _enclosing(index, node: ast.AST) -> str:
    anchor = index.definition_anchor(node)
    while anchor is not None and not isinstance(anchor, ast.FunctionDef | ast.AsyncFunctionDef):
        anchor = index.definition_anchor(anchor)
    return index.function_qualname(anchor) if anchor is not None else "<module>"


def check_agent_render_then_adopt(provider: FactsProvider) -> tuple[Violation, ...]:
    """Agent writers adopt only via _check_adopt_or_skip on the rendered text."""
    rule = GUARD_AGENT_RENDER_ADOPT
    _facts, failures = _facts_for(provider, _CONSUMER, rule)
    if failures:
        return tuple(failures)
    index = provider.tree_index(_CONSUMER)
    deploy = index.function(_DEPLOY) if index is not None else None
    if index is None or deploy is None:
        return (_summary(rule, _CONSUMER, f"{_DEPLOY} must own agent adopt-then-write"),)

    findings: list[Violation] = []

    def _flag(node: ast.AST, message: str) -> None:
        findings.append(
            violation(rule, _CONSUMER, message, line=node.lineno, column=node.col_offset + 1)
        )

    owner_calls: list[ast.Call] = []
    for node in index.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        where = _enclosing(index, node)
        if name in _BYPASS_CALLS:
            _flag(
                node,
                f"agent adoption must route through {_OWNER_METHOD}(expected_content=...); "
                f"direct {name}() re-derives the decision",
            )
        elif name in _UNGUARDED_WRITERS:
            _flag(
                node,
                f"{name}() writes without adoption; deploy agents via {_DEPLOY}",
            )
        elif name == "write_text_lf" and where not in _WRITE_ALLOWED_IN:
            _flag(node, f"agent writes must go through {_DEPLOY}")
        elif name == _OWNER_METHOD:
            if where != _DEPLOY:
                _flag(node, f"{_OWNER_METHOD} for agents is called only from {_DEPLOY}")
            owner_calls.append(node)

    deploy_calls = [node for node in index.walk(deploy) if isinstance(node, ast.Call)]
    adopt = [node for node in deploy_calls if _call_name(node) == _OWNER_METHOD]
    expected = (
        next((kw.value for kw in adopt[0].keywords if kw.arg == "expected_content"), None)
        if len(adopt) == 1
        else None
    )
    writes = [
        node
        for node in deploy_calls
        if _call_name(node) == "write_text_lf"
        and [ast.unparse(arg) for arg in node.args] == ["target_path", "rendered"]
    ]
    if not (
        isinstance(expected, ast.Name)
        and expected.id == "rendered"
        and len(writes) == 1
        and writes[0].lineno > adopt[0].lineno
    ):
        findings.append(
            _summary(
                rule,
                _CONSUMER,
                f"{_DEPLOY} must call {_OWNER_METHOD}(..., expected_content=rendered) "
                "and then write_text_lf(target_path, rendered)",
            )
        )
    if not owner_calls:
        findings.append(_summary(rule, _CONSUMER, f"agent deployment must call {_OWNER_METHOD}"))
    return tuple(findings)


GUARD_COMMAND_RENDER_ADOPT = "install-deployment-command-render-then-adopt"
_COMMAND_CONSUMER = "src/apm_cli/integration/command_integrator.py"
_COMMAND_RENDER = "CommandIntegrator._rendered_command"
_COMMAND_RENDER_STEPS = frozenset(
    {"_transform_prompt_to_command", "resolve_links", "_gemini_command_text"}
)


def check_command_render_then_adopt(provider: FactsProvider) -> tuple[Violation, ...]:
    """Command adoption compares the rendered command, never the raw prompt.

    Commands are format-transformed, so every ``_check_adopt_or_skip`` call in
    ``command_integrator.py`` passes ``expected_content`` rendered by
    ``CommandIntegrator._rendered_command``, and no raw comparison runs there.
    An install then adopts command files an earlier run deployed but did not
    record.
    """
    rule = GUARD_COMMAND_RENDER_ADOPT
    _facts, failures = _facts_for(provider, _COMMAND_CONSUMER, rule)
    if failures:
        return tuple(failures)
    index = provider.tree_index(_COMMAND_CONSUMER)
    render = index.function(_COMMAND_RENDER) if index is not None else None
    if index is None or render is None:
        return (_summary(rule, _COMMAND_CONSUMER, f"{_COMMAND_RENDER} must render commands"),)

    findings: list[Violation] = []
    owner_calls = 0
    for node in index.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _BYPASS_CALLS - {"read_bytes"}:
            findings.append(
                violation(
                    rule,
                    _COMMAND_CONSUMER,
                    f"command adoption must route through {_OWNER_METHOD}; "
                    f"direct {name}() compares the wrong bytes",
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
        elif name == _OWNER_METHOD:
            owner_calls += 1
            expected = next(
                (kw.value for kw in node.keywords if kw.arg == "expected_content"), None
            )
            if not (isinstance(expected, ast.Call) and _call_name(expected) == "_rendered_command"):
                findings.append(
                    violation(
                        rule,
                        _COMMAND_CONSUMER,
                        f"{_OWNER_METHOD} for commands must pass "
                        "expected_content=self._rendered_command(...)",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
    steps = {_call_name(node) for node in index.walk(render) if isinstance(node, ast.Call)}
    if not steps >= _COMMAND_RENDER_STEPS:
        findings.append(
            _summary(
                rule,
                _COMMAND_CONSUMER,
                f"{_COMMAND_RENDER} must render through {', '.join(sorted(_COMMAND_RENDER_STEPS))}",
            )
        )
    if not owner_calls:
        findings.append(
            _summary(rule, _COMMAND_CONSUMER, f"command deployment must call {_OWNER_METHOD}")
        )
    return tuple(findings)
