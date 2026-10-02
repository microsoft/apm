"""Keep deployed audit discovery and content applicability on canonical owners."""

from __future__ import annotations

from collections.abc import Iterable

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "audit-primitive-discovery"
_SCANNER = "src/apm_cli/security/file_scanner.py"
_DISCOVERY = "src/apm_cli/security/primitive_discovery.py"
_REQUIRED = {
    _SCANNER: {
        "_scan_claimed_files": {"primitive_surfaces", "_scan_primitive"},
        "_scan_deployed_trees": {"primitive_surfaces", "iter_surface_files", "_scan_primitive"},
        "_content_entries": {"inspect_native_hooks"},
        "_scan_primitive": {"safe_surface_path"},
        "scan_project_result": {"_scan_claimed_files", "_scan_deployed_trees"},
    },
    _DISCOVERY: {
        "_mapping_surface": {"skills_deploy_path"},
        "primitive_surfaces": {"native_hook_config", "_mapping_surface", "root_context_filename"},
        "safe_surface_path": {"ensure_path_within", "has_symlink_component"},
    },
    "src/apm_cli/integration/skill_integrator.py": {
        "_target_skills_root": {"skills_deploy_path"},
    },
    "src/apm_cli/integration/hook_native_formats.py": {
        "inspect_native_hooks": {"hook_handlers"},
    },
    "src/apm_cli/commands/audit.py": {"_audit_content_scan": {"scan_project_result"}},
    "src/apm_cli/policy/ci_checks.py": {"_check_content_integrity": {"scan_project_result"}},
    "src/apm_cli/security/audit_report.py": {
        "findings_to_json": {"finding_to_json"},
        "findings_to_sarif": {"finding_to_sarif"},
    },
    "src/apm_cli/policy/models.py": {
        "to_json": {"finding_to_json"},
        "to_sarif": {"finding_to_sarif"},
    },
}


def check_audit_discovery(provider: FactsProvider) -> Iterable[Violation]:
    """Require the shared discovery/applicability route in both audit consumers."""
    for path, functions in _REQUIRED.items():
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        yield from failures
        for function, required in functions.items():
            calls = {
                call.qualname.rsplit(".", 1)[-1]
                for call in facts.calls
                if call.scope.rsplit(".", 1)[-1] == function
            }
            for name in sorted(required - calls):
                yield violation(RULE_ID, path, f"{function} must route through canonical {name}.")
        if path == _SCANNER:
            for call in facts.calls:
                if call.scope.rsplit(".", 1)[-1] in {
                    "_scan_deployed_trees",
                    "_scan_claimed_files",
                    "scan_project_result",
                } and call.qualname.rsplit(".", 1)[-1] in {"scan_files", "scan_file", "walk"}:
                    yield violation(
                        RULE_ID,
                        path,
                        "Automatic audit must not bypass primitive applicability with a raw file/tree scan.",
                        line=call.line,
                    )
        if path in {_SCANNER, _DISCOVERY}:
            for literal in facts.literals:
                if literal.value_repr.strip("'\"").startswith(
                    (".claude/", ".codex/", ".github/", ".cursor/")
                ):
                    yield violation(
                        RULE_ID,
                        path,
                        "Audit target paths must derive from target/hook registries.",
                        line=literal.line,
                    )


RULES = (
    Rule(
        id=RULE_ID,
        group="mutation_writes",
        guard_ids=(RULE_ID, "audit-finding-serialization"),
        description="Deployed primitive discovery and applicable checks share registry-derived owners.",
        check=check_audit_discovery,
    ),
)
