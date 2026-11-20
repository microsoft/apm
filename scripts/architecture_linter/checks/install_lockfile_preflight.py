"""Guard: every mutating install refuses an unwritable lockfile up front (#4168).

``install/helpers/plan_inputs.py::confirm_update_plan`` runs before any
download, deploy or cleanup. For every install that will write the lockfile
(not ``--frozen``, ``--dry-run`` or lockfile-only) it calls the lockfile
owner's ``refuse_lockfile_before_mutation``, so a symlinked or newer-generator
lockfile stops the command while nothing has changed. The refusal must not
depend on update intent alone.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_INSTALL_LOCKFILE_PREFLIGHT = "install-deployment-install-lockfile-preflight"
_PATH = "src/apm_cli/install/helpers/plan_inputs.py"
_GATE = "confirm_update_plan"
_OWNER_CALL = "refuse_lockfile_before_mutation"


def check_install_lockfile_preflight(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep the up-front lockfile refusal unconditional for mutating installs."""
    rule_id = GUARD_INSTALL_LOCKFILE_PREFLIGHT
    facts, failures = _facts_for(provider, _PATH, rule_id)
    if failures:
        return tuple(failures)
    found = [item for item in facts.definitions if item.name == _GATE]
    if len(found) != 1:
        return (violation(rule_id, _PATH, f"{_GATE} not found"),)
    gate = found[0]
    findings: list[Violation] = []
    if not any(
        call.qualname.rsplit(".", 1)[-1] == _OWNER_CALL and gate.line <= call.line <= gate.end_line
        for call in facts.calls
    ):
        findings.append(violation(rule_id, _PATH, f"{_GATE} must call {_OWNER_CALL}"))
    body = facts.lines[gate.line - 1 : gate.end_line]
    findings.extend(
        violation(
            rule_id,
            _PATH,
            f"{_GATE} must refuse an unwritable lockfile for every mutating install, "
            "not only updates",
            line=gate.line + offset,
        )
        for offset, line in enumerate(body)
        if "update_refs" in line
    )
    return tuple(findings)
