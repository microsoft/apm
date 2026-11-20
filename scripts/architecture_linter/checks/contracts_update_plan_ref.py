"""Guard the update plan's ref comparison for ref-less dependencies (RA-007).

``install/plan.build_update_plan`` compares the ref the lockfile RECORDS, not
the branch name the resolver reports. A dependency declared without a ref is
recorded with an absent ref, so the plan must project the fresh ref through
``_recorded_ref`` before comparing; otherwise every update reports a change.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_test_shared import _summary
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Rule, Violation

RULE = "contracts-tooling-update-plan-recorded-ref"
OWNER = "src/apm_cli/install/plan.py"


def _assigns_projection(function: ast.AST) -> bool:
    """Whether ``new_ref = _recorded_ref(dep, old, new_ref)`` is present."""
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "new_ref" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_recorded_ref"
        for node in ast.walk(function)
    )


def check_update_plan_recorded_ref(provider: FactsProvider) -> tuple[Violation, ...]:
    """The update plan compares the recorded ref, never the resolver's branch name."""
    facts, failures = checked_facts(provider, OWNER, RULE, require_python=True)
    findings = list(failures)
    if facts is None or facts.tree_index is None:
        return tuple(findings)
    plan = facts.tree_index.function("build_update_plan")
    if plan is None or not _assigns_projection(plan):
        findings.append(_summary(RULE, OWNER, "build_update_plan must compare _recorded_ref"))
    projection = facts.tree_index.function("_recorded_ref")
    if projection is None or "old.resolved_ref is None" not in ast.unparse(projection):
        findings.append(_summary(RULE, OWNER, "_recorded_ref must keep an absent locked ref"))
    return tuple(findings)


RULES = (
    Rule(
        id=RULE,
        group="contracts_tests",
        guard_ids=(RULE,),
        description="Update plans compare the ref the lockfile records.",
        check=check_update_plan_recorded_ref,
    ),
)
