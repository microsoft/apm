"""Base-integrator, target-file-contraction, and provenance-state analyzers.

Ports three owner guards recorded in
``.apm/architecture/owners/install-deployment.json``:
``install-deployment-base-integrator``,
``install-deployment-target-file-contraction``, and
``install-deployment-provenance-state``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _all_names,
    _awk_body,
    _body_has,
    _facts_for,
    _line_findings,
    _name_calls_in,
    _present,
    _present_re,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

_GUARD_PROVENANCE = "install-deployment-provenance-state"


_GUARD_TARGET_CONTRACTION = "install-deployment-target-file-contraction"


_GUARD_BASE_INTEGRATOR = "install-deployment-base-integrator"


def _body_has_re(body: Sequence[str], pattern: re.Pattern[str]) -> bool:
    """Return whether any captured body line matches `pattern`."""
    return any(pattern.search(line) is not None for line in body)


def _loaded_names(index: TreeIndex, node: ast.AST) -> set[str]:
    """Return every Name id loaded within `node` (Load context)."""
    return {
        item.id
        for item in index.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


_BASE_INTEGRATOR_OWNER = "src/apm_cli/integration/base_integrator.py"


_BASE_INTEGRATOR_METHODS = (
    "check_collision",
    "sync_remove_files",
    "cleanup_empty_parents",
    "validate_deploy_path",
)


def check_base_integrator(provider: FactsProvider) -> tuple[Violation, ...]:
    """File-level deploy / sync / cleanup must stay owned by BaseIntegrator."""
    rule_id = _GUARD_BASE_INTEGRATOR
    owner, owner_fail = _facts_for(provider, _BASE_INTEGRATOR_OWNER, rule_id)
    if owner_fail:
        return tuple(owner_fail)

    missing: list[str] = []
    if not _present_re(owner, re.compile(r"^class BaseIntegrator(?:\([^)]*\))?:")):
        missing.append("class BaseIntegrator")
    for method in _BASE_INTEGRATOR_METHODS:
        if not _present_re(owner, re.compile(r"^    def " + re.escape(method) + r"\(")):
            missing.append(method)
    if missing:
        return (
            _summary(
                rule_id,
                _BASE_INTEGRATOR_OWNER,
                "File-level deploy/sync/cleanup must stay owned by BaseIntegrator; missing: "
                + ", ".join(missing),
            ),
        )
    return ()


def _defines_function(facts: object, name: str) -> bool:
    """Return whether the file defines a (async) function named `name`."""
    return any(
        definition.name == name and definition.kind in ("function", "async_function")
        for definition in getattr(facts, "definitions", ())
    )


_CONTRACTION_MANIFEST = "src/apm_cli/install/manifest_reconcile.py"


_CONTRACTION_LOCKFILE = "src/apm_cli/install/phases/lockfile.py"


_CONTRACTION_POST_LOCAL = "src/apm_cli/install/phases/post_deps_local.py"


_CONTRACTION_UNINSTALL = "src/apm_cli/commands/uninstall/cli.py"


_MANIFEST_OWNER_FN = "reconcile_target_deployed_files"


_LIFECYCLE_ROUTER_FN = "_reconcile_target_deployed_files"


_CLEANUP_OWNER_FN = "reconcile_deployed_block"


def _contraction_ownership_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Port check_target_instruction_contraction_owner over standard call facts."""
    manifest, manifest_fail = _facts_for(provider, _CONTRACTION_MANIFEST, rule_id)
    lockfile, lockfile_fail = _facts_for(provider, _CONTRACTION_LOCKFILE, rule_id)
    post_local, post_fail = _facts_for(provider, _CONTRACTION_POST_LOCAL, rule_id)
    uninstall, uninstall_fail = _facts_for(provider, _CONTRACTION_UNINSTALL, rule_id)
    failures = list(manifest_fail) + list(lockfile_fail) + list(post_fail) + list(uninstall_fail)
    if failures:
        return failures

    messages: list[tuple[str, str]] = []
    if not _defines_function(manifest, _MANIFEST_OWNER_FN):
        messages.append(
            (
                _CONTRACTION_MANIFEST,
                "target-file contraction owner is missing from manifest_reconcile.py",
            )
        )
    if _MANIFEST_OWNER_FN not in _name_calls_in(manifest, "reconcile_deployed_state"):
        messages.append(
            (
                _CONTRACTION_MANIFEST,
                "reconcile_deployed_state must delegate target files to manifest_reconcile",
            )
        )
    if _CLEANUP_OWNER_FN not in _name_calls_in(manifest, _MANIFEST_OWNER_FN):
        messages.append(
            (
                _CONTRACTION_MANIFEST,
                "target-file contraction owner must delegate deletion through reconcile_deployed_block",
            )
        )
    if "remove_stale_deployed_files" not in _name_calls_in(manifest, _CLEANUP_OWNER_FN):
        messages.append(
            (
                _CONTRACTION_MANIFEST,
                "target-file deletion must stay routed through reconcile_deployed_block",
            )
        )
    if "remove_stale_deployed_files" in _name_calls_in(lockfile, _LIFECYCLE_ROUTER_FN):
        messages.append(
            (_CONTRACTION_LOCKFILE, "LockfileBuilder must not delete target files directly")
        )
    if _MANIFEST_OWNER_FN not in _name_calls_in(lockfile, _LIFECYCLE_ROUTER_FN):
        messages.append(
            (
                _CONTRACTION_LOCKFILE,
                "LockfileBuilder must route target contraction through manifest_reconcile",
            )
        )
    if "remove_stale_deployed_files" in _name_calls_in(post_local, "run"):
        messages.append(
            (_CONTRACTION_POST_LOCAL, "post-deps local must not delete target files directly")
        )
    if _CLEANUP_OWNER_FN not in _name_calls_in(post_local, "run"):
        messages.append(
            (
                _CONTRACTION_POST_LOCAL,
                "post-deps local must route target contraction through reconcile_deployed_block",
            )
        )
    if _MANIFEST_OWNER_FN not in _name_calls_in(uninstall, "uninstall"):
        messages.append(
            (
                _CONTRACTION_UNINSTALL,
                "uninstall must route removed target files through manifest_reconcile",
            )
        )
    return [_summary(rule_id, path, message) for path, message in messages]


def _reconciler_reconcile_call(node: ast.AST) -> bool:
    """Return whether `node` is ``DeploymentReconciler(...).reconcile(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reconcile"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "DeploymentReconciler"
    )


def _records_values_call(node: ast.AST) -> bool:
    """Return whether `node` is ``<x>.records.values()``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "records"
    )


def _locator_field(node: ast.AST, field: str) -> bool:
    """Return whether `node` is ``<x>.locator.<field>``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == field
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "locator"
    )


def _shared_contraction_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Port check_shared_target_contraction_owner over the shared tree index."""
    _facts, fail = _facts_for(provider, _CONTRACTION_MANIFEST, rule_id)
    if fail:
        return list(fail)
    index = provider.tree_index(_CONTRACTION_MANIFEST)
    if index is None or index.root is None:
        return []
    findings: list[Violation] = []
    if not any(_reconciler_reconcile_call(node) for node in index.walk(index.root)):
        findings.append(
            _summary(
                rule_id,
                _CONTRACTION_MANIFEST,
                "shared target contraction must delegate to DeploymentReconciler.reconcile",
            )
        )
    for node in index.functions():
        subtree = list(index.walk(node))
        if (
            any(_records_values_call(child) for child in subtree)
            and any(_locator_field(child, "target") for child in subtree)
            and any(_locator_field(child, "value") for child in subtree)
        ):
            findings.append(
                violation(
                    rule_id,
                    _CONTRACTION_MANIFEST,
                    "generic deployment row supersession belongs to DeploymentReconciler",
                    line=getattr(node, "lineno", 1),
                )
            )
    return findings


def check_target_file_contraction(provider: FactsProvider) -> tuple[Violation, ...]:
    """Target-scoped deployed-file contraction must stay owned by manifest_reconcile."""
    rule_id = _GUARD_TARGET_CONTRACTION
    findings = _contraction_ownership_messages(provider, rule_id)
    findings.extend(_shared_contraction_messages(provider, rule_id))
    return tuple(findings)


_DEPLOYMENT_LEDGER = "src/apm_cli/core/deployment_ledger.py"


_OWNER_ATTRIBUTES = frozenset({"owners", "active_owner", "deployment_ledger"})


_UNTRUSTED_NAME_PARTS = ("ghost", "invalid", "removed_record", "violation")


_REQUIRED_OWNER_CALLS = {
    "src/apm_cli/commands/prune.py": ("legacy_value", "reconcile_owner_references"),
    "src/apm_cli/commands/audit.py": ("owner_reference_violations",),
    "src/apm_cli/commands/uninstall/cli.py": ("cleanup_snapshot",),
    "src/apm_cli/policy/ci_checks.py": ("owner_reference_violations",),
}


_OWNED_STATE_FIELDS = frozenset(
    {
        "deployed_files",
        "deployed_file_hashes",
        "local_deployed_files",
        "local_deployed_file_hashes",
        "mcp_target_servers",
        "lsp_target_servers",
    }
)


_STATE_MUTATORS = frozenset({"append", "remove", "pop", "extend", "clear", "update", "insert"})


_STATE_ALLOWED = frozenset(
    {
        "src/apm_cli/core/deployment_state.py",
        _DEPLOYMENT_LEDGER,
        "src/apm_cli/deps/lockfile.py",
    }
)


def _contains_dependency_claim(index: TreeIndex, node: ast.AST) -> bool:
    """Return whether an expression reads canonical dependency cleanup claims."""
    return any(
        isinstance(item, ast.Attribute) and item.attr in {"deployed_files", "deployed_file_hashes"}
        for item in index.walk(node)
    )


def _trusted_claim_names(index: TreeIndex) -> set[str]:
    """Trace names derived from dependency deployed-file claims (fixed point)."""
    if index.root is None:
        return set()
    trusted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in index.walk(index.root):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if _contains_dependency_claim(index, value) or (
                    _loaded_names(index, value) & trusted
                ):
                    before = len(trusted)
                    for target in targets:
                        trusted.update(_all_names(index, target))
                    changed = changed or len(trusted) != before
            elif isinstance(node, ast.For):
                if _loaded_names(index, node.iter) & trusted:
                    before = len(trusted)
                    trusted.update(_all_names(index, node.target))
                    changed = changed or len(trusted) != before
    return trusted


def _iterates_owners(index: TreeIndex, node: ast.AST) -> bool:
    """Return whether an iterable expression walks a deployment owners field."""
    return any(
        isinstance(item, ast.Attribute) and item.attr == "owners" for item in index.walk(node)
    )


def _call_name(node: ast.Call) -> str:
    """Return the terminal callable name for one call expression."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _deployment_owner_findings(provider: FactsProvider, path: str, rule_id: str) -> list[Violation]:
    """Port check_deployment_owner_boundaries.analyze_source for one file."""
    _facts, fail = _facts_for(provider, path, rule_id)
    if fail:
        return list(fail)
    index = provider.tree_index(path)
    if index is None or index.root is None:
        return []
    trusted = _trusted_claim_names(index)
    findings: list[Violation] = []
    calls_seen: set[str] = set()

    def report(node: ast.AST, message: str) -> None:
        findings.append(violation(rule_id, path, message, line=getattr(node, "lineno", 1)))

    for node in index.walk(index.root):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            calls_seen.add(name)
            if name == "DeploymentRecord":
                report(node, "deployment consumers must not construct canonical records")
            if name in {"set", "frozenset"} and any(
                isinstance(item, ast.Attribute) and item.attr == "dependencies"
                for argument in node.args
                for item in index.walk(argument)
            ):
                report(node, "valid deployment owners must come from DeploymentLedgerCodec")
            if name == "remove_stale_deployed_files" and node.args:
                cleanup_expression = node.args[0]
                names = _loaded_names(index, cleanup_expression)
                if not names.intersection(trusted):
                    report(node, "deployed-file cleanup must derive from dependency claims")
                reads_ledger_locator = any(
                    isinstance(item, ast.Attribute) and item.attr == "locator"
                    for item in index.walk(cleanup_expression)
                )
                if reads_ledger_locator or any(
                    part in loaded.lower() for loaded in names for part in _UNTRUSTED_NAME_PARTS
                ):
                    report(node, "ledger violations must not authorize physical deletion")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for item in index.walk(target):
                    if isinstance(item, ast.Attribute) and item.attr in _OWNER_ATTRIBUTES:
                        report(item, f"direct {item.attr} mutation bypasses DeploymentLedgerCodec")
        elif isinstance(node, (ast.For, ast.comprehension)):
            if _iterates_owners(index, node.iter):
                report(node, "owner filtering must delegate to DeploymentReconciler")

    for call in sorted(set(_REQUIRED_OWNER_CALLS.get(path, ())) - calls_seen):
        findings.append(
            violation(rule_id, path, f"required canonical call {call} is missing", line=1)
        )
    return findings


def _owned_state_attr(node: ast.AST) -> bool:
    """Return whether one AST node references codec-owned deployment state."""
    return isinstance(node, ast.Attribute) and node.attr in _OWNED_STATE_FIELDS


def _state_mutation_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Port check_deployment_state_mutations across the ``src/apm_cli`` tree."""
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path in _STATE_ALLOWED:
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        lines: set[int] = set()
        for node in index.walk(index.root):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if _owned_state_attr(target) or (
                        isinstance(target, ast.Subscript) and _owned_state_attr(target.value)
                    ):
                        lines.add(node.lineno)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _STATE_MUTATORS
                and _owned_state_attr(node.func.value)
            ):
                lines.add(node.lineno)
        for line_number in sorted(lines):
            findings.append(
                violation(
                    rule_id,
                    path,
                    "deployment compatibility state must mutate only through canonical owners",
                    line=line_number,
                )
            )
    return findings


_CLEANUP_OWNER = "src/apm_cli/install/phases/cleanup.py"


_CLAIM_COLLECTION_NAME = "package_deployed_files"


_CLAIM_AGGREGATION_METHODS = frozenset({"add", "update", "union"})


def _cleanup_owner_call(node: ast.AST) -> bool:
    """Return whether `node` is ``DeploymentReconciler.current_claimed_paths(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "DeploymentReconciler"
        and node.func.attr == "current_claimed_paths"
    )


def _claim_collection_call(node: ast.AST) -> bool:
    """Return whether `node` is ``package_deployed_files.items()/.values()``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == _CLAIM_COLLECTION_NAME
        and node.func.attr in {"items", "values"}
    )


def _loop_aggregates_claims(index: TreeIndex, node: ast.For) -> bool:
    """Return whether a for-loop aggregates package_deployed_files locally."""
    if not _claim_collection_call(node.iter):
        return False
    if (
        isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Attribute)
        and (node.iter.func.attr == "values")
    ):
        return True
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in _CLAIM_AGGREGATION_METHODS
        for statement in node.body
        for child in index.walk(statement)
    )


def _cleanup_claim_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Port check_cleanup_claim_owner over the shared tree index."""
    _facts, fail = _facts_for(provider, _CLEANUP_OWNER, rule_id)
    if fail:
        return list(fail)
    index = provider.tree_index(_CLEANUP_OWNER)
    if index is None or index.root is None:
        return []
    findings: list[Violation] = []
    owner_calls = [node for node in index.walk(index.root) if _cleanup_owner_call(node)]
    if len(owner_calls) != 2:
        findings.append(
            _summary(
                rule_id,
                _CLEANUP_OWNER,
                "cleanup must delegate both current-claim decisions to "
                "DeploymentReconciler.current_claimed_paths",
            )
        )
    for node in index.walk(index.root):
        if isinstance(node, ast.For) and _loop_aggregates_claims(index, node):
            findings.append(
                violation(
                    rule_id,
                    _CLEANUP_OWNER,
                    "cleanup must not aggregate package_deployed_files in a local loop",
                    line=node.lineno,
                )
            )
        if isinstance(node, (ast.SetComp, ast.ListComp, ast.GeneratorExp)) and any(
            isinstance(child, ast.Name) and child.id == _CLAIM_COLLECTION_NAME
            for child in index.walk(node)
        ):
            findings.append(
                violation(
                    rule_id,
                    _CLEANUP_OWNER,
                    "cleanup must not aggregate package_deployed_files in a local comprehension",
                    line=node.lineno,
                )
            )
    return findings


def _legacy_scope_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Legacy user deployment scope must route through DeploymentLedgerCodec."""
    ledger, ledger_fail = _facts_for(provider, _DEPLOYMENT_LEDGER, rule_id)
    manifest, manifest_fail = _facts_for(provider, _CONTRACTION_MANIFEST, rule_id)
    targets, targets_fail = _facts_for(provider, "src/apm_cli/integration/targets.py", rule_id)
    failures = list(ledger_fail) + list(manifest_fail) + list(targets_fail)
    if failures:
        return failures
    if (
        not _present_re(ledger, re.compile(r"^_LEGACY_USER_TARGET_PREFIXES = \{"))
        or not _present(ledger, '".copilot/": "copilot"')
        or not _present_re(ledger, re.compile(r"^    def legacy_scope\("))
        or not _present(manifest, "scope=DeploymentLedgerCodec.legacy_scope(path)")
        or not _present(
            targets, "if targets is None and user_scope and t.user_root_dir is not None:"
        )
    ):
        return [
            _summary(
                rule_id,
                _DEPLOYMENT_LEDGER,
                "Legacy user deployment scope must route through DeploymentLedgerCodec",
            )
        ]
    return []


def _local_bundle_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Local-bundle replay provenance must route through DeploymentLedgerCodec."""
    handler, handler_fail = _facts_for(
        provider, "src/apm_cli/install/local_bundle_handler.py", rule_id
    )
    drift, drift_fail = _facts_for(provider, "src/apm_cli/install/drift.py", rule_id)
    if handler_fail or drift_fail:
        return list(handler_fail) + list(drift_fail)
    marker = re.compile(
        r"_LOCAL_BUNDLE_OWNER"
        r"|active_owner.*[\"']local-bundle[\"']"
        r"|[\"']local-bundle[\"'].*active_owner"
        r"|owners.*[\"']local-bundle[\"']"
    )
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _DEPLOYMENT_LEDGER:
            continue
        facts = provider.file_facts(path)
        if getattr(facts, "read_error", None) is not None:
            continue
        findings.extend(
            _line_findings(
                facts,
                path,
                rule_id,
                marker,
                "Local-bundle replay provenance must route through DeploymentLedgerCodec",
                respect_exempt=True,
            )
        )
    if not _present(handler, "DeploymentLedgerCodec.record_local_bundle_files") or not _present(
        drift, "DeploymentLedgerCodec.local_bundle_paths"
    ):
        findings.append(
            _summary(
                rule_id,
                _DEPLOYMENT_LEDGER,
                "Local-bundle replay provenance must route through DeploymentLedgerCodec",
            )
        )
    return findings


def _drift_membership_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Drift deployment membership must route through DeploymentLedgerCodec."""
    drift, drift_fail = _facts_for(provider, "src/apm_cli/install/drift.py", rule_id)
    if drift_fail:
        return list(drift_fail)
    tracked = _awk_body(drift, re.compile(r"^def _collect_tracked_files\("), re.compile(r"^def "))
    hashed = _awk_body(drift, re.compile(r"^def _collect_hashed_files\("), re.compile(r"^def "))
    forbidden = re.compile(r"lockfile\.dependencies|local_deployed_files|deployed_file_hashes")
    if (
        not _body_has(tracked, "DeploymentLedgerCodec.legacy_deployed_file_claims")
        or not _body_has(hashed, "DeploymentLedgerCodec.legacy_deployed_file_hash_paths")
        or _body_has_re(tracked, forbidden)
        or _body_has_re(hashed, forbidden)
    ):
        return [
            _summary(
                rule_id,
                "src/apm_cli/install/drift.py",
                "Drift deployment membership must route through DeploymentLedgerCodec",
            )
        ]
    return []


def _scanner_membership_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Hidden-Unicode membership must route through DeploymentLedgerCodec."""
    scanner_path = "src/apm_cli/security/file_scanner.py"
    scanner, scanner_fail = _facts_for(provider, scanner_path, rule_id)
    if scanner_fail:
        return list(scanner_fail)
    body = _awk_body(scanner, re.compile(r"^def scan_lockfile_packages\("), re.compile(r"^def "))
    if not _body_has(body, "DeploymentLedgerCodec.legacy_deployed_file_claims") or _body_has_re(
        body, re.compile(r"lock\.dependencies|dep\.deployed_files")
    ):
        return [
            _summary(
                rule_id,
                scanner_path,
                "Hidden-Unicode membership must route through DeploymentLedgerCodec",
            )
        ]
    return []


def _membership_owner_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Legacy deployed-file membership projection belongs to DeploymentLedgerCodec."""
    ledger, ledger_fail = _facts_for(provider, _DEPLOYMENT_LEDGER, rule_id)
    if ledger_fail:
        return list(ledger_fail)
    body = _awk_body(
        ledger,
        re.compile(r"^    def legacy_deployed_file_claims\("),
        re.compile(r"^    def "),
        keep=re.compile(r"legacy_deployed_file_claims"),
    )
    if (
        not _body_has(body, "dependency.deployed_files")
        or not _body_has(body, "lockfile.local_deployed_files")
        or _body_has(body, "from_lockfile")
    ):
        return [
            _summary(
                rule_id,
                _DEPLOYMENT_LEDGER,
                "Legacy deployed-file membership projection belongs to DeploymentLedgerCodec",
            )
        ]
    return []


def _claim_handoff_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Deployment claim handoff belongs to DeploymentReconciler."""
    lockfile_path = "src/apm_cli/install/phases/lockfile.py"
    lockfile, lockfile_fail = _facts_for(provider, lockfile_path, rule_id)
    if lockfile_fail:
        return list(lockfile_fail)
    findings = _line_findings(
        lockfile,
        lockfile_path,
        rule_id,
        re.compile(
            r"def reconcile_cross_package_deployed_files|all_current_deployed|other_current"
        ),
        "Deployment claim handoff belongs to DeploymentReconciler",
        respect_exempt=True,
    )
    if not _present(lockfile, "DeploymentReconciler.reconcile_package_claims"):
        findings.append(
            _summary(
                rule_id,
                lockfile_path,
                "LockfileBuilder must consume DeploymentReconciler package claims",
            )
        )
    return findings


def check_provenance_state(provider: FactsProvider) -> tuple[Violation, ...]:
    """Deployment provenance / state must stay owned by DeploymentLedgerCodec."""
    rule_id = _GUARD_PROVENANCE
    findings: list[Violation] = []
    for path in _REQUIRED_OWNER_CALLS:
        findings.extend(_deployment_owner_findings(provider, path, rule_id))
    findings.extend(_legacy_scope_findings(provider, rule_id))
    findings.extend(_state_mutation_findings(provider, rule_id))
    findings.extend(_local_bundle_findings(provider, rule_id))
    findings.extend(_drift_membership_findings(provider, rule_id))
    findings.extend(_scanner_membership_findings(provider, rule_id))
    findings.extend(_membership_owner_findings(provider, rule_id))
    findings.extend(_cleanup_claim_findings(provider, rule_id))
    findings.extend(_claim_handoff_findings(provider, rule_id))
    return tuple(findings)
