"""Guard: uninstall refuses before its first write (#4185, #4162).

``commands/uninstall/cli.py::uninstall`` runs ``_preflight_uninstall_writes``
before it removes anything. The preflight validates every recorded path the
command removes through ``BaseIntegrator.validate_deploy_path`` and resolves
the survivors' executables trust, so a refusal leaves apm.yml, the lockfile
and deployed files untouched; the survivor rebuild reuses that trust.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_UNINSTALL_PREFLIGHT = "install-deployment-uninstall-preflight"
_CLI = "src/apm_cli/commands/uninstall/cli.py"
_ENGINE = "src/apm_cli/commands/uninstall/engine.py"
_ROUTES = (
    (_CLI, "_preflight_uninstall_writes", "_refuse_unvalidated_paths"),
    (_CLI, "_preflight_uninstall_writes", "resolve_project_exec_allow_map"),
    (_CLI, "_refuse_unvalidated_paths", "validate_deploy_path"),
    (_ENGINE, "_sync_integrations_after_uninstall", "resolve_project_exec_allow_map"),
)
# The first writes uninstall performs; the preflight must precede them all.
_FIRST_WRITES = frozenset({"remove_codex_plugins", "_remove_packages_from_disk"})


def _definition(facts, name: str):
    found = [item for item in facts.definitions if item.name == name]
    return found[0] if len(found) == 1 else None


def _call_lines(facts, definition, callee: str) -> list[int]:
    return [
        call.line
        for call in facts.calls
        if call.qualname.rsplit(".", 1)[-1] == callee
        and definition.line <= call.line <= definition.end_line
    ]


def check_uninstall_preflight(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep every uninstall refusal ahead of the command's first write."""
    rule_id = GUARD_UNINSTALL_PREFLIGHT
    findings: list[Violation] = []
    for path, name, callee in _ROUTES:
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        definition = None if failures else _definition(facts, name)
        if definition is None or not _call_lines(facts, definition, callee):
            findings.append(violation(rule_id, path, f"{name} must call {callee}"))
    facts, failures = _facts_for(provider, _CLI, rule_id)
    findings.extend(failures)
    command = None if failures else _definition(facts, "uninstall")
    if command is None:
        return (*findings, violation(rule_id, _CLI, "uninstall command not found"))
    preflight = _call_lines(facts, command, "_preflight_uninstall_writes")
    writes = [line for callee in _FIRST_WRITES for line in _call_lines(facts, command, callee)]
    if not preflight or not writes or min(preflight) > min(writes):
        findings.append(
            violation(
                rule_id,
                _CLI,
                "uninstall must run _preflight_uninstall_writes before its first write",
            )
        )
    return tuple(findings)
