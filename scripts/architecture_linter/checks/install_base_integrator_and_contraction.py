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
    _lines,
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
    "copy_directory_files",
    "walk_source_tree",
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
    native_path = "src/apm_cli/integration/skill_integrator.py"
    native, native_fail = _facts_for(provider, native_path, rule_id)
    if native_fail:
        return tuple(native_fail)
    definition = next(
        (item for item in native.definitions if item.name == "_integrate_native_skill"), None
    )
    if definition is None or not any(
        call.qualname == "self.check_collision"
        and definition.line <= call.line <= definition.end_line
        for call in native.calls
    ):
        return (
            _summary(
                rule_id,
                native_path,
                "Native root skills must route collision protection through BaseIntegrator.check_collision",
            ),
        )
    failures: list[Violation] = []
    if (
        not _present(owner, "onerror=fail")
        or not _present(owner, "self.walk_source_tree(source)")
        or sum("on_member_skipped(target_file)" in line for line in owner.lines) != 3
        or not _present(native, "on_member_skipped=self._skill_member_skipped")
        or not _present(native, "or plan.skill_discovery_complete is not True")
        or not _present(native, "plan.skill_source_roots <= self._skill_completed_roots")
        or not _present(native, "plan.skill_source_files <= self._skill_completed_files")
        or not _present(owner, "if not destination_existed:")
        or not _present(owner, "self.cleanup_empty_parents([target_file], destination.parent)")
        or not _present(
            native, "written_hashes = {entry(path): compute_file_hash(path) for path in copied}"
        )
    ):
        failures.append(
            _summary(
                rule_id,
                native_path,
                "Skill completion must consume explicit source discovery and canonical copy outcomes",
            )
        )
    replacement_functions = {
        "copy_skill_to_target",
        "_promote_sub_skills",
        "_integrate_native_skill",
    }
    for definition in native.definitions:
        if definition.name not in replacement_functions:
            continue
        calls = {
            call.qualname
            for call in native.calls
            if definition.line <= call.line <= definition.end_line
        }
        if any(call.endswith((".rmtree", ".copytree")) for call in calls):
            failures.append(
                _summary(
                    rule_id,
                    native_path,
                    "Skill replacement must preserve sibling assets through file-level copying",
                )
            )
    if "self.copy_directory_files" not in {call.qualname for call in native.calls}:
        failures.append(
            _summary(
                rule_id, native_path, "Skill materialization must route through BaseIntegrator"
            )
        )
    if not _present(
        native, "rel_path = rewriter._skill_path_entry(target, materialization_root)"
    ) or not _present(native, "return self._skill_path_entry(path, project_root)"):
        failures.append(
            _summary(
                rule_id,
                native_path,
                "Skill collisions and copied members must share canonical deployed-path identity",
            )
        )
    services_path = "src/apm_cli/install/services.py"
    services, services_fail = _facts_for(provider, services_path, rule_id)
    failures.extend(services_fail)
    if not services_fail and (
        not _present(services, "for path in skill_result.deployed_files")
        or any("skill_bundle_file_entries" in call.qualname for call in services.calls)
    ):
        failures.append(
            _summary(
                rule_id,
                services_path,
                "Skill ownership must consume successful source members, not destination inventory",
            )
        )
    failures.extend(_skill_completion_receipt_messages(provider, rule_id))
    return tuple(failures)


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
    index = provider.tree_index(_CONTRACTION_LOCKFILE)
    attach = index.function("LockfileBuilder._attach_deployed_files") if index else None
    reconciliation_calls = (
        [
            node
            for node in index.walk(attach)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _CLEANUP_OWNER_FN
        ]
        if attach is not None
        else []
    )
    if len(reconciliation_calls) != 1 or not any(
        keyword.arg == "active_targets" and ast.unparse(keyword.value) == "self.ctx.targets"
        for keyword in reconciliation_calls[0].keywords
    ):
        messages.append(
            (
                _CONTRACTION_LOCKFILE,
                "lock persistence must use the install target scope, not skill receipt targets",
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


# Local rows rebuilt without their reconciled locators fall back to the
# unattributed target, which no later install can prove stale (#3179).
_LOCAL_PERSISTENCE_SITES = (
    (_CONTRACTION_POST_LOCAL, "run"),
    (_CONTRACTION_LOCKFILE, "LockfileBuilder._preserve_existing_local_state"),
)


def _carries_provenance(index: TreeIndex, function: ast.AST) -> bool:
    """Return whether every ``replace_legacy_owner`` call in *function* passes provenance and targets."""
    calls = [
        node
        for node in index.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace_legacy_owner"
    ]

    def _empty(value: ast.expr) -> bool:
        return (
            (isinstance(value, ast.Constant) and value.value is None)
            or (isinstance(value, (ast.Tuple, ast.List, ast.Set)) and not value.elts)
            or (isinstance(value, ast.Dict) and not value.keys)
            or (
                isinstance(value, ast.Call)
                and not value.args
                and all(_empty(keyword.value) for keyword in value.keywords)
            )
        )

    def _passes(call: ast.Call, name: str) -> bool:
        return any(keyword.arg == name and not _empty(keyword.value) for keyword in call.keywords)

    return bool(calls) and all(
        _passes(call, "provenance") and _passes(call, "current_targets") for call in calls
    )


def _local_persistence_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Local deployment rows must persist with the locators reconciliation chose."""
    findings: list[Violation] = []
    for path, qualname in _LOCAL_PERSISTENCE_SITES:
        _facts, fail = _facts_for(provider, path, rule_id)
        if fail:
            findings.extend(fail)
            continue
        index = provider.tree_index(path)
        function = index.function(qualname) if index is not None else None
        if function is None or not _carries_provenance(index, function):
            findings.append(
                _summary(
                    rule_id,
                    path,
                    "local deployment rows must persist through replace_legacy_owner(provenance=..., current_targets=...)",
                )
            )
    return findings


def _local_preservation_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Require explicit generic governance and preserve unprocessed target claims."""
    index = provider.tree_index(_CONTRACTION_MANIFEST)
    function = index.function("union_preserving") if index is not None else None
    if function is None:
        return []
    findings: list[Violation] = []
    assignments = [
        node
        for node in index.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "sweep" for target in node.targets)
    ]
    if len(assignments) != 1 or not (
        isinstance(assignments[0].value, ast.Attribute)
        and isinstance(assignments[0].value.value, ast.Name)
        and assignments[0].value.value.id == "intent"
        and assignments[0].value.attr == "generic_governed_values"
    ):
        findings.append(
            _summary(
                rule_id,
                _CONTRACTION_MANIFEST,
                "generic cleanup must use explicit caller governance, not infer historical ownership",
            )
        )
    claims = [
        keyword.value
        for node in index.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replace"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "intent"
        for keyword in node.keywords
        if keyword.arg == "unprocessed_claims"
    ]
    if len(claims) != 1 or "unprocessed" not in _loaded_names(index, claims[0]):
        findings.append(
            _summary(
                rule_id, _CONTRACTION_MANIFEST, "reconciliation must pass unprocessed target claims"
            )
        )
    state_path = "src/apm_cli/core/deployment_state.py"
    state, failures = _facts_for(provider, state_path, rule_id)
    findings.extend(failures)
    if not failures and not _present(
        state, "and record.locator.value not in intent.unprocessed_claims"
    ):
        findings.append(
            _summary(rule_id, state_path, "stale-target cleanup must preserve unprocessed claims")
        )
    return findings


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


def _absent_dependency_claim_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Keep absent-claim decisions canonical and separate from disk deletion."""
    state_path = "src/apm_cli/core/deployment_state.py"
    state, failures = _facts_for(provider, state_path, rule_id)
    findings = list(failures)
    if not failures:
        for required in (
            "obsolete_absent = self._obsolete_absent_values(",
            "or prior.locator.value in obsolete_absent",
            "record.owners != (completed.owner,)",
            "or not completed.targets <= intent.active_targets",
            "- intent.unprocessed_claims",
            "proven_skill_values = self._proven_skill_values(",
            "return frozenset(candidates & proven_skill_values)",
            "from apm_cli.security.primitive_discovery import primitive_surfaces",
            "completed.skill_values & completed.current_values & successful_by_value.keys()",
            'if surface.kind != "skills"',
            "roots.intersection(candidate_path.parents)",
            "proof.locator.target in completed.targets",
        ):
            if not _present(state, required):
                findings.append(
                    _summary(
                        rule_id,
                        state_path,
                        "absent dependency claims require the canonical owner-scoped completion guard",
                    )
                )
                break

    _, failures = _facts_for(provider, _CONTRACTION_MANIFEST, rule_id)
    findings.extend(failures)
    if failures:
        return findings
    index = provider.tree_index(_CONTRACTION_MANIFEST)
    block = index.function("reconcile_deployed_block") if index is not None else None
    if block is None:
        return findings
    union = index.function("union_preserving")
    scope_comparisons = {
        (node.left.attr, node.comparators[0].id)
        for node in (index.walk(union) if union is not None else ())
        if isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.NotEq)
        and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "intent"
        and isinstance(node.comparators[0], ast.Name)
    }
    if (
        not {
            ("active_targets", "active_names"),
            ("declared_targets", "declared_names"),
        }
        <= scope_comparisons
    ):
        findings.append(
            _summary(
                rule_id,
                _CONTRACTION_MANIFEST,
                "reconciliation intent must agree with the supplied target profiles",
            )
        )
    excludes_absent = any(
        isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "dropped"
        and isinstance(node.op, ast.Sub)
        and isinstance(node.value, ast.Name)
        and node.value.id == "verified_absent"
        for node in index.walk(block)
    )
    probe_path = "src/apm_cli/install/helpers/absence_observation.py"
    _, failures = _facts_for(provider, probe_path, rule_id)
    findings.extend(failures)
    if failures:
        return findings
    probe_index = provider.tree_index(probe_path)
    probe = (
        probe_index.function("verified_absent_dependency_values")
        if probe_index is not None
        else None
    )
    probe_calls = (
        {_call_name(node) for node in probe_index.walk(probe) if isinstance(node, ast.Call)}
        if probe is not None
        else set()
    )
    routed = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "apm_cli.install.helpers.absence_observation"
        and any(
            alias.name == "verified_absent_dependency_values" and alias.asname is None
            for alias in node.names
        )
        for node in index.walk(block)
    ) and any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "verified_absent"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "verified_absent_dependency_values"
        for node in index.walk(block)
    )
    if not routed:
        findings.append(
            _summary(
                rule_id, _CONTRACTION_MANIFEST, "absence observations must use the read-only helper"
            )
        )
    if not excludes_absent or not {"validate_deploy_path", "lstat"} <= probe_calls:
        findings.append(
            _summary(
                rule_id,
                _CONTRACTION_MANIFEST,
                "safe absence observations may retire metadata but must not authorize disk deletion",
            )
        )
    if probe_calls.intersection({"unlink", "rmtree", "remove_stale_deployed_files"}):
        findings.append(_summary(rule_id, probe_path, "absence probing must remain read-only"))
    return findings


def _records_values_call(node: ast.AST) -> bool:
    """Return whether `node` is ``<x>.records.values()``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "records"
    )


def _skill_completion_receipt_messages(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Bind absence authority to the public discovery and materialization producers."""
    path = "src/apm_cli/install/services.py"
    index = provider.tree_index(path)
    function = index.function("integrate_package_primitives") if index is not None else None
    expected = {
        "owner": "package_name",
        "targets": "frozenset((target.name for target in projection_targets if target.supports('skills')))",
        "current_values": "frozenset(deployed)",
        "skill_values": (
            "frozenset((_deployed_path_entry(path, project_root, targets) "
            "for path in skill_result.deployed_files))"
        ),
    }
    for node in index.walk(function) if function is not None else ():
        if not isinstance(node, ast.If) or ast.unparse(node.test) != (
            "source_plan.skill_discovery_complete is True and skill_result.complete is True"
        ):
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and ast.unparse(statement.targets[0]) == "result['completed_reintegration']"
                and isinstance(statement.value, ast.Call)
                and _call_name(statement.value) == "CompletedReintegration"
                and {
                    keyword.arg: ast.unparse(keyword.value) for keyword in statement.value.keywords
                }
                == expected
            ):
                return []
    return [
        _summary(
            rule_id,
            path,
            "absent-claim receipts require positive public discovery/materialization, "
            "the package owner, processed skill targets and source-backed members",
        )
    ]


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
    findings.extend(_local_persistence_messages(provider, rule_id))
    findings.extend(_local_preservation_messages(provider, rule_id))
    findings.extend(_absent_dependency_claim_messages(provider, rule_id))
    return tuple(findings)


_DEPLOYMENT_LEDGER = "src/apm_cli/core/deployment_ledger.py"


_OWNER_ATTRIBUTES = frozenset({"owners", "active_owner", "deployment_ledger"})


_UNTRUSTED_NAME_PARTS = ("ghost", "invalid", "removed_record", "violation")


_REQUIRED_OWNER_CALLS = {
    "src/apm_cli/install/phases/lockfile.py": ("merge_dependencies",),
    "src/apm_cli/commands/prune.py": ("legacy_value", "reconcile_owner_references"),
    "src/apm_cli/commands/audit.py": ("owner_reference_violations",),
    "src/apm_cli/commands/uninstall/cli.py": ("cleanup_snapshot",),
    "src/apm_cli/policy/ci_checks.py": ("owner_reference_violations",),
}


_SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"


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


def _skill_lockfile_root_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """User-scope skill ownership must read provenance from the canonical lockfile root."""
    facts, fail = _facts_for(provider, _SKILL_INTEGRATOR, rule_id)
    if fail:
        return list(fail)

    route_message = "user-scope skill ownership must route lockfile provenance through get_apm_dir(InstallScope.USER)"
    forward_message = "skill ownership consumers must forward the canonical lockfile_root through every ownership-map integration path"
    required_import = "from apm_cli.core.scope import InstallScope, get_apm_dir"
    required_route = (
        "get_apm_dir(InstallScope.USER) if scope is InstallScope.USER else project_root"
    )
    integration_routes = {
        "_promote_sub_skills_standalone",
        "_integrate_native_skill",
        "_integrate_skill_bundle",
    }
    findings: list[Violation] = []
    if not _present(facts, required_import) or not _present(facts, required_route):
        findings.append(_summary(rule_id, _SKILL_INTEGRATOR, route_message))
    support_path = "src/apm_cli/integration/skill_support.py"
    support, support_failures = _facts_for(provider, support_path, rule_id)
    findings.extend(support_failures)
    if not _present(facts, "_build_ownership_maps = staticmethod(build_skill_ownership_maps)") or (
        not support_failures
        and not _present(support, "LockFile.read(get_lockfile_path(lockfile_root))")
    ):
        findings.append(
            _summary(
                rule_id,
                _SKILL_INTEGRATOR,
                "skill ownership maps must resolve deployment provenance from the selected lockfile_root",
            )
        )
    index = provider.tree_index(_SKILL_INTEGRATOR)
    function = (
        index.function("SkillIntegrator.integrate_package_skill") if index is not None else None
    )
    calls = (
        [
            node
            for node in index.own_scope(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in integration_routes
        ]
        if function is not None
        else []
    )
    if {call.func.attr for call in calls} != integration_routes or any(
        not any(
            keyword.arg == "lockfile_root"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "lockfile_root"
            for keyword in call.keywords
        )
        for call in calls
    ):
        findings.append(_summary(rule_id, _SKILL_INTEGRATOR, forward_message))
    snapshot_body = _awk_body(
        facts, re.compile(r"^    def ownership_snapshot\("), re.compile(r"^    def ")
    )
    accessor_body = _awk_body(
        facts, re.compile(r"^    def _ownership_maps\("), re.compile(r"^    def ")
    )
    if not re.search(
        r"finally:\s*\n\s+self\._ownership_snapshots = None",
        "\n".join(snapshot_body),
    ) or not all(
        _body_has(accessor_body, snippet)
        for snippet in (
            "if self._ownership_snapshots is None:",
            "return self._build_ownership_maps(lockfile_root)",
            "root = lockfile_root.resolve()",
            "self._build_ownership_maps(root)",
            "MappingProxyType(owned_by)",
            "MappingProxyType(native_owners)",
        )
    ):
        findings.append(
            _summary(
                rule_id,
                _SKILL_INTEGRATOR,
                "skill ownership snapshots must reuse the canonical builder immutably and expire on phase exit",
            )
        )
    if (
        sum("self._ownership_maps(lockfile_root or project_root)" in line for line in facts.lines)
        != 3
    ):
        findings.append(
            _summary(
                rule_id,
                _SKILL_INTEGRATOR,
                "all skill layouts must consume the phase-scoped ownership accessor",
            )
        )
    pipeline_path = "src/apm_cli/install/pipeline.py"
    pipeline, pipeline_failures = _facts_for(provider, pipeline_path, rule_id)
    findings.extend(pipeline_failures)
    if not pipeline_failures and (
        not re.search(
            r"with \(\s*contextlib\.nullcontext\(\)\s*if ctx\.lockfile_only\s*"
            r'else ctx\.integrators\["skill"\]\.ownership_snapshot\(\s*'
            r"replay_lockfile=ctx\.existing_lockfile if ctx\.frozen else None,"
            r"\s*materialization_session=session,\s*\)\s*\):\s*"
            r'_run_phase\("integrate", _integrate_phase, ctx\)',
            "\n".join(pipeline.lines),
        )
        or sum("_run_integration_phase(ctx)" in line for line in pipeline.lines) != 1
        or sum('_run_phase("integrate", _integrate_phase, ctx)' in line for line in pipeline.lines)
        != 1
    ):
        findings.append(
            _summary(
                rule_id,
                pipeline_path,
                "ownership snapshot lifetime must enclose only integration and bypass lockfile-only mode",
            )
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
    """Return whether cleanup delegates to the canonical survivor projection."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "surviving_deployed_paths"
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
    facts, fail = _facts_for(provider, _CLEANUP_OWNER, rule_id)
    manifest, manifest_fail = _facts_for(provider, _CONTRACTION_MANIFEST, rule_id)
    if fail or manifest_fail:
        return list(fail) + list(manifest_fail)
    index = provider.tree_index(_CLEANUP_OWNER)
    if index is None or index.root is None:
        return []
    findings: list[Violation] = []
    owner_calls = [node for node in index.walk(index.root) if _cleanup_owner_call(node)]
    survivor_body = _awk_body(
        manifest, re.compile(r"^def surviving_deployed_paths\("), re.compile(r"^def ")
    )
    if (
        len(owner_calls) != 1
        or not _present(facts, "if deployed_file not in surviving_files")
        or not _present(facts, "other_deployed = surviving_files - set(new_deployed)")
        or not all(
            _body_has(survivor_body, snippet)
            for snippet in (
                "DeploymentReconciler.reconcile_package_claims(",
                "DeploymentReconciler.current_claimed_paths(current_claims)",
                "files, _ = union_preserving(",
                "intent=DeploymentIntent(",
                "desired_owners=None,",
                "authoritative_targets=owner not in failed_owners,",
            )
        )
    ):
        findings.append(
            _summary(
                rule_id,
                _CLEANUP_OWNER,
                "cleanup must protect both deletion passes through surviving_deployed_paths "
                "and its canonical claim/target reconciliation",
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


def _materialization_evidence_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Keep local rendered-write authority behind the live install and canonical codec."""
    findings: list[Violation] = []
    requirements = {
        _DEPLOYMENT_LEDGER: (
            "class DeploymentMaterializationSession:",
            'data["installation"] != self._installation',
            "not self._matches_root_identity(data)",
            'return data["root_identity"] == [metadata.st_dev, metadata.st_ino]',
            'return data["root_identity"] == self._root_identity()',
            '"deployment-receipts-v2"',
            '"volume": darwin_volume_uuid(self.root)',
            "self.baseline(path, [target]) != record.content_hash",
            "compute_file_hash(path) != record.content_hash",
            "if snapshot[key] == self._legacy_snapshot.get(key):",
            "record.active_owner not in self._valid_owners",
            "self._bindings.get(key) != [record.active_owner, self._claim_signature(value)]",
            "value = deployed_path_entry(path, self.root, targets)",
            "deployed_path_entry(path, self.root, targets),",
            "self._written[key] = value",
            "for key, value in self._written.items():",
            "record.active_owner == owner",
            "record.locator.target != locator.target",
            "and self.baseline(path, targets) is not None",
            'DeploymentLedgerCodec.from_rows(data["records"])',
            "DeploymentLedgerCodec.rows(DeploymentLedger(records))",
            'atomic_write_text(ignore, "*\\n", new_file_mode=0o600, create_only=True)',
            'self.identity_path, installation + "\\n", new_file_mode=0o600, create_only=True',
            "create_only=not self._state_present",
        ),
        _SKILL_INTEGRATOR: (
            "on_member_written=written.append",
            "session.baseline(path, targets)",
            "session.requires_target_write(path, targets, owner)",
            "session.record({path: written_hashes[entry(path)] for path in written}",
            "materialization_session.flush()",
            "self._materialization_session = None",
            "removed_paths.append(target)",
            "removed_paths=removed_paths",
        ),
        _BASE_INTEGRATOR_OWNER: (
            "baseline_hash(target_file)",
            "(baseline_hash is not None and expected is None)",
            "on_member_written(target_file)",
            "and not (requires_write is not None and requires_write(target_file))",
        ),
        "src/apm_cli/install/pipeline.py": (
            "materialization_session=session",
            "if ctx.lockfile_only or ctx.dry_run",
            "ctx.project_root, ctx.scope, ctx.existing_lockfile",
            "materialization_session.bind_committed_claims(",
        ),
        "src/apm_cli/integration/cleanup.py": (
            "DeploymentMaterializationSession.retire_removed(",
            "[*result.deleted_targets, *absent_targets]",
            "if receipt_retirements is None:",
            "receipt_retirements.extend(retired)",
        ),
        "src/apm_cli/install/phases/cleanup.py": (
            "_run_cleanup(ctx, receipt_retirements)",
            "finally:",
            "DeploymentMaterializationSession.retire_removed(",
            "receipt_retirements=receipt_retirements",
        ),
        "src/apm_cli/integration/skill_support.py": ("removed_paths.append(skill_subdir)",),
        "src/apm_cli/commands/uninstall/engine.py": (
            "DeploymentMaterializationSession(project_root, _rebuild_scope, lockfile)",
            'with _integrators["skills"].ownership_snapshot(materialization_session=materialization_session):',
            "materialization_session=materialization_session,",
            "removed_paths=removed_skill_paths",
            "finally:",
            "project_root, removed_skill_paths, user_scope=user_scope",
        ),
        "src/apm_cli/commands/uninstall/cli.py": (
            "_persist_uninstall_lockfile(lockfile, lockfile_path, logger, integration_cleanup)",
            "else:\n        if integration_cleanup.materialization_session is not None:",
            "integration_cleanup.materialization_session.bind_committed_claims(lockfile)",
        ),
    }
    for path, snippets in requirements.items():
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        text = "\n".join(_lines(facts))
        if not failures and not all(snippet in text for snippet in snippets):
            findings.append(
                _summary(rule_id, path, "Local materialization evidence must use the guarded owner")
            )
    allowed = set(requirements)
    for path in _python_paths(provider, _SRC_PREFIX):
        facts = provider.file_facts(path)
        if path != _DEPLOYMENT_LEDGER:
            findings.extend(
                _line_findings(
                    facts,
                    path,
                    rule_id,
                    re.compile(r'["\'](?:deployment-receipts|\.apm-local)["\']'),
                    "Local materialization storage belongs only to the deployment owner",
                    respect_exempt=False,
                )
            )
        if path not in allowed:
            findings.extend(
                _line_findings(
                    facts,
                    path,
                    rule_id,
                    re.compile(r"\bDeploymentMaterializationSession\b|materialization_session="),
                    "Only live install or uninstall survivor writes may promote materialization evidence",
                    respect_exempt=False,
                )
            )
    return findings


def check_provenance_state(provider: FactsProvider) -> tuple[Violation, ...]:
    """Deployment provenance / state must stay owned by DeploymentLedgerCodec."""
    rule_id = _GUARD_PROVENANCE
    findings: list[Violation] = []
    for path in _REQUIRED_OWNER_CALLS:
        findings.extend(_deployment_owner_findings(provider, path, rule_id))
    findings.extend(_skill_lockfile_root_findings(provider, rule_id))
    findings.extend(_legacy_scope_findings(provider, rule_id))
    findings.extend(_state_mutation_findings(provider, rule_id))
    findings.extend(_local_bundle_findings(provider, rule_id))
    findings.extend(_drift_membership_findings(provider, rule_id))
    findings.extend(_scanner_membership_findings(provider, rule_id))
    findings.extend(_membership_owner_findings(provider, rule_id))
    findings.extend(_cleanup_claim_findings(provider, rule_id))
    findings.extend(_claim_handoff_findings(provider, rule_id))
    findings.extend(_materialization_evidence_findings(provider, rule_id))
    return tuple(findings)
