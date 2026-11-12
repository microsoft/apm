"""Keep Codex agent conversion on the shared install-time link resolver (#2966).

The production Codex branch of ``AgentIntegrator.integrate_agents_for_target``
must hand ``_write_codex_agent`` the ``BaseIntegrator.resolve_links`` owner and
report the writer's resolved-link count. The writer must call that owner with
its own source and target and serialize the resolved body. Direct static
callers of ``_write_codex_agent`` (tests, spec conformance) stay exempt.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_CODEX_AGENT_LINKS = "install-deployment-codex-agent-link-resolution"
_CONSUMER = "src/apm_cli/integration/agent_integrator.py"
_INTEGRATE = "AgentIntegrator.integrate_agents_for_target"
_WRITER = "AgentIntegrator._write_codex_agent"
_HOOK = "resolve_links"
_OWNER_CALL = f"self.{_HOOK}"
_WRITER_CALL = f"{_HOOK}(body, source, target)"


def _assign_targets(index, node: ast.AST) -> list[str] | None:
    parent = index.parent(node)
    if not isinstance(parent, ast.Assign):
        return None
    return [ast.unparse(target) for target in parent.targets]


def check_codex_agent_link_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    """Codex agent bodies are rewritten by BaseIntegrator.resolve_links."""
    rule = GUARD_CODEX_AGENT_LINKS
    _facts, failures = _facts_for(provider, _CONSUMER, rule)
    if failures:
        return tuple(failures)
    index = provider.tree_index(_CONSUMER)
    integrate = index.function(_INTEGRATE) if index is not None else None
    writer = index.function(_WRITER) if index is not None else None
    if index is None or integrate is None or writer is None:
        return (
            _summary(
                rule,
                _CONSUMER,
                f"{_INTEGRATE} and {_WRITER} must exist for Codex link resolution",
            ),
        )

    findings: list[Violation] = []
    writer_calls = [
        node
        for node in index.walk(integrate)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._write_codex_agent"
    ]
    if not writer_calls:
        findings.append(
            _summary(rule, _CONSUMER, f"{_INTEGRATE} must deploy Codex agents via {_WRITER}")
        )
    for call in writer_calls:
        hook = next((item.value for item in call.keywords if item.arg == _HOOK), None)
        if hook is None or ast.unparse(hook) != _OWNER_CALL:
            findings.append(
                _summary(
                    rule,
                    _CONSUMER,
                    f"Codex agent deployment must pass {_HOOK}={_OWNER_CALL} to _write_codex_agent",
                )
            )
        if _assign_targets(index, call) != ["links_resolved"]:
            findings.append(
                _summary(
                    rule,
                    _CONSUMER,
                    "Codex agent deployment must report the writer's resolved-link count "
                    "as links_resolved",
                )
            )

    writer_nodes = index.walk(writer)
    hook_calls = [
        node
        for node in writer_nodes
        if isinstance(node, ast.Call) and ast.unparse(node.func) == _HOOK
    ]
    dump_lines = [
        node.lineno
        for node in writer_nodes
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".dumps")
    ]
    applied = [
        node
        for node in hook_calls
        if ast.unparse(node) == _WRITER_CALL
        and _assign_targets(index, node) == ["(body, links_resolved)"]
    ]
    if not applied or not dump_lines or min(node.lineno for node in applied) > min(dump_lines):
        findings.append(
            _summary(
                rule,
                _CONSUMER,
                f"{_WRITER} must assign body, links_resolved = {_WRITER_CALL} "
                "before TOML serialization",
            )
        )
    return tuple(findings)
