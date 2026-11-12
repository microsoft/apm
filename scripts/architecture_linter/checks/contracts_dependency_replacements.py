"""Guard original dependency ownership and root-owned source substitution."""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_dependency_policy import _unique_definitions
from scripts.architecture_linter.checks.contracts_test_shared import _summary
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Rule, Violation

RULE = "contracts-tooling-source-replacements"
OWNER = "src/apm_cli/models/dependency/replacement.py"
RESOLVER = "src/apm_cli/deps/apm_resolver.py"
PLAN_INPUTS = "src/apm_cli/install/helpers/plan_inputs.py"
RESOLUTION_REPORT = "src/apm_cli/install/presentation/resolution.py"


def _binding_order(provider: FactsProvider) -> list[Violation]:
    """Require root, dev and transitive assignments before queue deduplication."""
    facts, failures = checked_facts(provider, RESOLVER, RULE, require_python=True)
    findings = list(failures)
    if facts is None or facts.tree_index is None:
        return list(findings)
    function = facts.tree_index.function("APMDependencyResolver.build_dependency_tree")
    if function is None:
        return [*findings, _summary(RULE, RESOLVER, "Missing canonical dependency-tree builder")]
    loops = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id in {"dep_ref", "sub_dep"}
    ]
    invalid = len(loops) != 3
    for loop in loops:
        bindings = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == loop.target.id
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "self.replacement_index.bind"
        ]
        lookups = [
            node.lineno
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == f"{loop.target.id}.get_resolution_key"
        ]
        invalid |= len(bindings) != 1 or (
            bool(lookups) and bool(bindings) and bindings[0].lineno >= min(lookups)
        )
    if invalid:
        findings.append(
            _summary(RULE, RESOLVER, "Bind every original requirement before queue deduplication")
        )
    return list(findings)


def check_source_replacements(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep source selection in its typed owner and consumers on clean projections."""
    findings = _unique_definitions(
        provider,
        RULE,
        OWNER,
        ("ReplacementIndex", "parse_replace_block", "project_effective_reference"),
    )
    for path, name in (
        (PLAN_INPUTS, "build_resolved_update_plan"),
        (RESOLUTION_REPORT, "report_resolved_dependencies"),
        ("src/apm_cli/install/plan.py", "lockfile_satisfies_replacements"),
    ):
        findings.extend(_unique_definitions(provider, RULE, path, (name,)))
    required = {
        OWNER: (
            "return (self.host, self.port, self.repo_url, self.subpath)",
            "self._by_key.get(ReplacementSelector.from_reference(dep).key)",
            "if not self._rules or dep.replacement is not None:",
            "binding.record(dep, parent, is_dev)",
        ),
        RESOLVER: (
            "build_replacement_index(root_package.replace_rules or [])",
            "if sub_dep.replacement is not None or queue_key not in queued_keys:",
        ),
        "src/apm_cli/models/dependency/reference.py": (
            "return self.replacement.original.get_unique_key()",
        ),
        "src/apm_cli/deps/immutable_requirements.py": (
            "self._resolver.resolve_git_reference(dep.source_reference)",
        ),
        "src/apm_cli/deps/lockfile.py": (
            "isinstance(self.replacement.rule.source, LocalReplacementSource)",
        ),
        "src/apm_cli/install/pipeline.py": (
            "dep = dep.source_reference",
            "local_cleanup_pending=(\n            not lockfile_only\n"
            "            and local_rows_need_reconciling(",
            "from apm_cli.install.helpers.plan_inputs import build_resolved_update_plan",
            "plan = build_resolved_update_plan(ctx, _early_lockfile)",
            "apm_package=apm_package,\n        logger=logger,\n    ):",
        ),
        "src/apm_cli/commands/install.py": (
            "or not lockfile_satisfies_replacements(_existing_lock, apm_package.replace_rules)[0]",
        ),
        "src/apm_cli/install/helpers/no_work.py": (
            "builder.save_replacement_intent()",
            "apm_dir, apm_package=apm_package, logger=logger, lockfile_only=lockfile_only",
        ),
        "src/apm_cli/install/phases/lockfile.py": (
            "lockfile_satisfies_replacements(self.ctx.existing_lockfile, rules)",
            "lockfile = copy.deepcopy(existing) if existing is not None else LockFile()",
            "self._write_if_changed(lockfile, get_lockfile_path(self.ctx.apm_dir), LockFile)",
        ),
        "src/apm_cli/install/phases/resolve.py": (
            "from apm_cli.install.presentation.resolution import report_resolved_dependencies",
            "report_resolved_dependencies(ctx, resolver, deps_to_install, dependency_graph.dependency_tree)",
        ),
        PLAN_INPUTS: (
            "from apm_cli.install.plan import build_update_plan",
            "from apm_cli.utils.content_hash import compute_package_hash",
            "dependency.replacement is not None and dependency.is_local",
            "resolved_content_hashes=ctx.package_hashes",
        ),
        RESOLUTION_REPORT: (
            "resolver.replacement_index.unused_rules",
            "ctx.logger.diagnostics.warn(",
            "ctx.logger.verbose_detail(",
            "dependency.ownership_reference.to_display_reference()",
            "dependency.source_reference.to_display_reference()",
        ),
        "src/apm_cli/install/helpers/ref_reuse.py": (
            "locked.matches_replacement(dep_ref)",
            "dep_ref = dep_ref.source_reference",
        ),
        "src/apm_cli/install/helpers/ref_seed.py": ("seed(selected.source_reference, ref, sha)",),
        "src/apm_cli/install/native_plugin_admission.py": ("if lock_dep.is_local_source:",),
    }
    for path, needles in required.items():
        facts, failures = checked_facts(provider, path, RULE, require_python=True)
        findings.extend(failures)
        text = "\n".join(facts.lines) if facts is not None else ""
        if facts is not None and any(needle not in text for needle in needles):
            findings.append(
                _summary(
                    RULE,
                    path,
                    "Preserve original ownership and canonical selected-source projection",
                )
            )
        if facts is not None and facts.tree_index is not None:
            if path == "src/apm_cli/install/pipeline.py":
                forbidden = {"build_update_plan", "compute_package_hash"}
                duplicated = any(
                    definition.name in forbidden for definition in facts.definitions
                ) or any(call.qualname.rsplit(".", 1)[-1] in forbidden for call in facts.calls)
            elif path == "src/apm_cli/install/phases/resolve.py":
                duplicated = "unused_rules" in text or "Unused replacement rule" in text
            else:
                duplicated = False
            if duplicated:
                findings.append(
                    _summary(
                        RULE, path, "Route replacement plan inputs and reports through helpers"
                    )
                )
    path = "src/apm_cli/models/dependency/materialization.py"
    facts, failures = checked_facts(provider, path, RULE, require_python=True)
    findings.extend(failures)
    if facts is not None and facts.tree_index is not None:
        function = facts.tree_index.function("build_materialization_path")
        if function is None or "dependency = dependency.ownership_reference" not in ast.unparse(
            function
        ):
            findings.append(_summary(RULE, path, "Materialization must use original ownership"))
    findings.extend(_binding_order(provider))
    path = "src/apm_cli/models/dependency/reference.py"
    facts, failures = checked_facts(provider, path, RULE, require_python=True)
    findings.extend(failures)
    if facts is not None and facts.tree_index is not None:
        function = facts.tree_index.function("DependencyReference.get_canonical_dependency_string")
        if function is None or any(
            isinstance(node, ast.Attribute) and node.attr in {"ownership_reference", "replacement"}
            for node in ast.walk(function)
        ):
            findings.append(_summary(RULE, path, "Policy matching must use selected source fields"))
    return tuple(findings)


RULES = (
    Rule(
        id=RULE,
        group="contracts_tests",
        guard_ids=(RULE,),
        description="Root source bindings preserve exact identity and selected-source boundaries.",
        check=check_source_replacements,
    ),
)
