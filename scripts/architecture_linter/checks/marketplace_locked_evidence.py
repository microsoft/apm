"""Keep marketplace declaration association and ref evidence in their owners."""

import ast

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-locked-evidence"
ENVELOPE_RULE_ID = "marketplace-integrations-json-envelope"
PROJECTION_RULE_ID = "marketplace-integrations-json-projection"
_ENVELOPE_CONSUMERS = (
    "src/apm_cli/commands/marketplace/__init__.py",
    "src/apm_cli/commands/view.py",
)
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


def check_json_envelope(provider: FactsProvider) -> tuple[Violation, ...]:
    """Reject duplicate literal envelopes in the marketplace and view commands."""
    findings: list[Violation] = []
    for path in _ENVELOPE_CONSUMERS:
        facts, failures = checked_facts(provider, path, ENVELOPE_RULE_ID, require_python=True)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        for node in facts.tree_index.nodes:
            if not isinstance(node, ast.Dict):
                continue
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
            if "schema_version" in keys and ("data" in keys or "error" in keys):
                findings.append(
                    violation(
                        ENVELOPE_RULE_ID,
                        path,
                        "Marketplace and view envelopes must use core/json_envelope.py",
                        line=node.lineno,
                    )
                )
    return tuple(findings)


def check_json_projection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep JSON string/path sanitization in the shared projection owner."""
    findings: list[Violation] = []
    for path in _ENVELOPE_CONSUMERS:
        facts, failures = checked_facts(provider, path, PROJECTION_RULE_ID, require_python=True)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        for node in facts.tree_index.nodes:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("safe_"):
                continue
            findings.append(
                violation(
                    PROJECTION_RULE_ID,
                    path,
                    "Marketplace and view safe_* projection belongs in core/json_envelope.py",
                    line=node.lineno,
                )
            )
        for call in facts.calls:
            if call.qualname.rsplit(".", 1)[-1] in {
                "urlsplit",
                "urlunsplit",
                "urlparse",
                "urlunparse",
            }:
                # Registration's URL parser is unrelated to JSON projection.
                if path.endswith("marketplace/__init__.py") and call.scope in {
                    "_default_alias_from_remote_url",
                    "_default_alias_from_url",
                }:
                    continue
                findings.append(
                    violation(
                        PROJECTION_RULE_ID,
                        path,
                        "Route JSON URL projection through safe_source/safe_text",
                        line=call.line,
                    )
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
    Rule(
        id=ENVELOPE_RULE_ID,
        group="marketplace_integrations",
        guard_ids=(ENVELOPE_RULE_ID,),
        description="Marketplace and view JSON envelopes use their shared owner.",
        check=check_json_envelope,
    ),
    Rule(
        id=PROJECTION_RULE_ID,
        group="marketplace_integrations",
        guard_ids=(PROJECTION_RULE_ID,),
        description="Marketplace and view safe string/path projection uses its shared owner.",
        check=check_json_projection,
    ),
)
