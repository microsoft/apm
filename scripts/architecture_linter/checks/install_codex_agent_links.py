"""Keep Codex agent conversion on the shared install-time link resolver (#2966).

The production Codex branch of ``AgentIntegrator.integrate_agents_for_target``
must hand ``_write_codex_agent`` a ``resolve_body_links`` callback bound to
``BaseIntegrator.resolve_links`` with the real source and the generated target,
and the writer must apply it before TOML serialization. Direct static callers
of ``_write_codex_agent`` (tests, spec conformance) stay exempt.
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
_CALLBACK = "resolve_body_links"
_EXPECTED_BINDING = {"source": "source_file", "target": "target_path"}


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _is_bound_resolver(node: ast.expr | None) -> bool:
    """Return whether `node` is ``functools.partial(self.resolve_links, ...)``."""
    if not isinstance(node, ast.Call) or ast.unparse(node.func) not in {
        "functools.partial",
        "partial",
    }:
        return False
    if not node.args or ast.unparse(node.args[0]) != "self.resolve_links":
        return False
    for name, expected in _EXPECTED_BINDING.items():
        value = _keyword(node, name)
        if value is None or ast.unparse(value) != expected:
            return False
    return True


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
        if not _is_bound_resolver(_keyword(call, _CALLBACK)):
            findings.append(
                _summary(
                    rule,
                    _CONSUMER,
                    "Codex agent deployment must pass resolve_body_links="
                    "functools.partial(self.resolve_links, source=source_file, "
                    "target=target_path) to _write_codex_agent",
                )
            )
        parent = index.parent(call)
        if not isinstance(parent, ast.Assign) or [ast.unparse(t) for t in parent.targets] != [
            "links_resolved"
        ]:
            findings.append(
                _summary(
                    rule,
                    _CONSUMER,
                    "Codex agent deployment must report the writer's resolved-link count "
                    "as links_resolved",
                )
            )

    writer_nodes = index.walk(writer)
    callback_lines = [
        node.lineno
        for node in writer_nodes
        if isinstance(node, ast.Call) and ast.unparse(node.func) == _CALLBACK
    ]
    dump_lines = [
        node.lineno
        for node in writer_nodes
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".dumps")
    ]
    if not callback_lines or not dump_lines or min(callback_lines) > min(dump_lines):
        findings.append(
            _summary(
                rule,
                _CONSUMER,
                f"{_WRITER} must apply resolve_body_links to the body before TOML serialization",
            )
        )
    return tuple(findings)
