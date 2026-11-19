"""Keep marketplace declaration association, ref and catalog-origin evidence in their owners."""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-locked-evidence"
ORIGIN_RULE_ID = "marketplace-integrations-catalog-origin"
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
    ("src/apm_cli/drift.py", "locked_marketplace_row", "lockfile.get_dependency"),
    ("src/apm_cli/drift.py", "locked_marketplace_row", "marketplace_ref_mismatch"),
    ("src/apm_cli/drift.py", "locked_marketplace_replay", "locked_marketplace_row"),
    ("src/apm_cli/drift.py", "locked_marketplace_replay", "locked_dep.to_edge_reference"),
    ("src/apm_cli/drift.py", "locked_marketplace_replay", "_owns_locked_servers"),
    ("src/apm_cli/drift.py", "_owns_locked_servers", "package_lsp_owner"),
    ("src/apm_cli/drift.py", "_owns_locked_servers", "mcp_package_owner"),
    ("src/apm_cli/install/dry_run_plan.py", "_resolved_key", "locked_marketplace_replay"),
    ("src/apm_cli/install/lsp/integration.py", "_dependency_provenance", "package_lsp_owner"),
    (
        "src/apm_cli/integration/mcp_config_view.py",
        "_collect_locked_dependencies",
        "mcp_package_owner",
    ),
    ("src/apm_cli/integration/mcp_config_view.py", "_collect_unlocked_compat", "mcp_package_owner"),
    ("src/apm_cli/deps/reachability.py", "_locked_marketplace_form", "locked.to_edge_reference"),
    ("src/apm_cli/deps/apm_resolver.py", "_resolve_marketplace_dep", "locked_marketplace_replay"),
    ("src/apm_cli/deps/apm_resolver.py", "_resolve_marketplace_dep", "frozen_marketplace_refusal"),
    ("src/apm_cli/marketplace/resolver.py", "resolve_marketplace_plugin", "build_tag_regex"),
)
# Lock-first replay (#3911): the replay decision precedes any catalog call.
_ORDERED = (
    (
        "src/apm_cli/deps/apm_resolver.py",
        "_resolve_marketplace_dep",
        "locked_marketplace_replay",
        "resolve_marketplace_plugin",
    ),
    (
        "src/apm_cli/deps/apm_resolver.py",
        "_resolve_marketplace_dep",
        "frozen_marketplace_refusal",
        "resolve_marketplace_plugin",
    ),
)


_ORIGIN_ROUTES = (
    ("src/apm_cli/marketplace/resolver.py", "resolve_marketplace_plugin", "marketplace_origin"),
    ("src/apm_cli/marketplace/client.py", "_parse_catalog", "catalog_digest"),
    (
        "src/apm_cli/deps/apm_resolver.py",
        "_resolve_marketplace_dep",
        "require_locked_catalog_origin",
    ),
    (
        "src/apm_cli/marketplace/catalog_origin.py",
        "require_locked_catalog_origin",
        "same_marketplace_origin",
    ),
    (
        "src/apm_cli/marketplace/catalog_origin.py",
        "require_locked_catalog_origin",
        "lockfile.marketplace_rows",
    ),
    (
        "src/apm_cli/marketplace/catalog_origin.py",
        "registrations_sharing_origin",
        "same_marketplace_catalog",
    ),
    ("src/apm_cli/commands/marketplace/__init__.py", "add", "_warn_if_duplicate_origin"),
    (
        "src/apm_cli/commands/marketplace/__init__.py",
        "_warn_if_duplicate_origin",
        "registrations_sharing_origin",
    ),
    ("src/apm_cli/marketplace/reconcile.py", "_apply", "registrations_sharing_origin"),
)
_ORIGIN_BEFORE_CATALOG = (
    "src/apm_cli/deps/apm_resolver.py",
    "_resolve_marketplace_dep",
    "require_locked_catalog_origin",
    "resolve_marketplace_plugin",
)


def _call_lines(provider: FactsProvider, path: str, function: str, callee: str, rule_id: str):
    """Return (failures, lines of *callee* calls inside the single *function* definition)."""
    facts, failures = checked_facts(provider, path, rule_id, require_python=True)
    if failures:
        return failures, ()
    definitions = [definition for definition in facts.definitions if definition.name == function]
    if len(definitions) != 1:
        return (), ()
    start, end = definitions[0].line, definitions[0].end_line
    return (), tuple(
        call.line for call in facts.calls if call.qualname == callee and start <= call.line <= end
    )


def _check_routes(
    provider: FactsProvider, rule_id: str, routes: tuple[tuple[str, str, str], ...]
) -> list[Violation]:
    findings: list[Violation] = []
    for path, function, callee in routes:
        failures, lines = _call_lines(provider, path, function, callee, rule_id)
        if failures:
            findings.extend(failures)
        elif not lines:
            findings.append(
                violation(rule_id, path, f"{function} must call canonical owner {callee}")
            )
    return findings


def _check_origin(provider: FactsProvider) -> tuple[Violation, ...]:
    """Lock origin evidence and alias checks route through the catalog-origin owners."""
    findings = _check_routes(provider, ORIGIN_RULE_ID, _ORIGIN_ROUTES)
    path, function, guard, catalog = _ORIGIN_BEFORE_CATALOG
    _failures, guard_lines = _call_lines(provider, path, function, guard, ORIGIN_RULE_ID)
    _failures, catalog_lines = _call_lines(provider, path, function, catalog, ORIGIN_RULE_ID)
    if guard_lines and catalog_lines and min(guard_lines) > min(catalog_lines):
        findings.append(
            violation(
                ORIGIN_RULE_ID,
                path,
                f"{function} must call {guard} before {catalog} reaches the catalog",
            )
        )
    return tuple(findings)


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
    for path, function, first, then in _ORDERED:
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        definitions = [
            definition for definition in facts.definitions if definition.name == function
        ]
        lines = {
            callee: [
                call.line
                for call in facts.calls
                if call.qualname == callee
                and len(definitions) == 1
                and definitions[0].line <= call.line <= definitions[0].end_line
            ]
            for callee in (first, then)
        }
        if not lines[first] or (lines[then] and min(lines[then]) < min(lines[first])):
            findings.append(violation(RULE_ID, path, f"{function} must call {first} before {then}"))
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="marketplace_integrations",
        guard_ids=(RULE_ID, "marketplace-integrations-locked-association"),
        description="Marketplace audit and replay consume canonical locked association and ref evidence.",
        check=_check,
    ),
    Rule(
        id=ORIGIN_RULE_ID,
        group="marketplace_integrations",
        guard_ids=(ORIGIN_RULE_ID,),
        description=(
            "Marketplace lock evidence records the catalog origin, and alias resolution "
            "and registration compare that origin, never the alias (#3912)."
        ),
        check=_check_origin,
    ),
)
