"""Keep marketplace declaration association and ref evidence in their owners."""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-locked-evidence"
_ROUTES = (
    ("src/apm_cli/policy/ci_checks.py", "_check_ref_consistency", "lock.get_dependency"),
    ("src/apm_cli/policy/ci_checks.py", "_check_ref_consistency", "marketplace_ref_mismatch"),
    ("src/apm_cli/policy/ci_checks.py", "_check_ref_consistency", "dep_ref.get_display_name"),
    ("src/apm_cli/policy/ci_checks.py", "_check_no_orphans", "lock.get_dependency"),
    ("src/apm_cli/install/plan.py", "lockfile_satisfies_manifest", "lockfile.get_dependency"),
    ("src/apm_cli/install/plan.py", "lockfile_satisfies_manifest", "dep.get_display_name"),
    ("src/apm_cli/drift.py", "detect_ref_change", "marketplace_ref_mismatch"),
    ("src/apm_cli/drift.py", "marketplace_ref_mismatch", "satisfies_range"),
    ("src/apm_cli/drift.py", "marketplace_ref_mismatch", "is_full_revision_pin"),
    ("src/apm_cli/marketplace/resolver.py", "resolve_marketplace_plugin", "build_tag_regex"),
)


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require actual scoped AST calls, not imports, comments, or dead strings."""
    findings: list[Violation] = []
    for path, function, callee in _ROUTES:
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        definitions = [
            definition for definition in facts.definitions if definition.name == function
        ]
        if len(definitions) != 1 or not any(
            call.qualname == callee and definitions[0].line <= call.line <= definitions[0].end_line
            for call in facts.calls
        ):
            findings.append(
                violation(RULE_ID, path, f"{function} must call canonical owner {callee}")
            )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="marketplace_integrations",
        guard_ids=(RULE_ID, "marketplace-integrations-locked-association"),
        description="Marketplace audit and replay consume canonical locked association and ref evidence.",
        check=_check,
    ),
)
