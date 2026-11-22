"""Guard: uninstall refuses before its first write (#4185, #4162).

``commands/uninstall/cli.py::uninstall`` runs ``_preflight_uninstall_writes``
before it removes anything. The preflight refuses out-of-project paths before
any change (``BaseIntegrator.validate_deploy_path``) and resolves the
survivors' executables trust, so a refusal leaves apm.yml, the lockfile and
deployed files untouched; the survivor rebuild reuses that trust.

The deployment-evidence refusal has one owner,
``commands/uninstall/selection_shrink.py::uninstall_evidence_refusals``: it
names the files an uninstall would delete (including the files a shrinking
survivor selection retires) and asks ``DeploymentEvidence.refusals``. Uninstall
and ``apm audit --ci``'s allow-list advice (#4570) both route through it, and
no other module under ``src/apm_cli`` calls ``refusals`` itself.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.contracts_test_shared import _python_paths
from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_UNINSTALL_PREFLIGHT = "install-deployment-uninstall-preflight"
_CLI = "src/apm_cli/commands/uninstall/cli.py"
_ENGINE = "src/apm_cli/commands/uninstall/engine.py"
_SHRINK = "src/apm_cli/commands/uninstall/selection_shrink.py"
_REMEDY = "src/apm_cli/policy/allowlist_remedy.py"
_REFUSAL_OWNER = "uninstall_evidence_refusals"
# DeploymentEvidence.refusals is defined in cleanup.py and asked only by the owner.
_REFUSAL_CALLERS = frozenset({"src/apm_cli/integration/cleanup.py", _SHRINK})
_ROUTES = (
    (_SHRINK, _REFUSAL_OWNER, "uninstall_deletion_candidates"),
    (_SHRINK, "uninstall_deletion_candidates", "shrink_path_split"),
    (_CLI, "_refuse_uninstall_without_evidence", _REFUSAL_OWNER),
    (_REMEDY, "_deletions_have_evidence", _REFUSAL_OWNER),
    (_REMEDY, "_deletions_have_evidence", "project_shrunk_selections"),
    (_REMEDY, "_uninstall_policy_resolves", "resolve_project_exec_allow_map"),
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
    findings.extend(_refusals_outside_owner(provider, rule_id))
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


def _refusals_outside_owner(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Closed world: only the refusal owner asks ``DeploymentEvidence.refusals``."""
    findings: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        if path in _REFUSAL_CALLERS:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        findings.extend(
            violation(
                rule_id,
                path,
                f"deployment-evidence refusals must come from {_SHRINK}::{_REFUSAL_OWNER}",
                line=call.line,
            )
            for call in facts.calls
            if call.qualname.endswith(".refusals")
        )
    return findings
