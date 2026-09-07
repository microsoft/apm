"""Policy casing and generated-bytecode lifecycle owner guards."""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.contracts_test_shared import _python_paths, _summary
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Rule, Violation

POLICY_RULE = "contracts-tooling-policy-identity"
BYTECODE_RULE = "contracts-tooling-python-artifact-membership"
IDENTITY = "src/apm_cli/models/dependency/identity.py"
MATCHER = "src/apm_cli/policy/matcher.py"
CHECKS = "src/apm_cli/policy/policy_checks.py"
BYTECODE = "src/apm_cli/security/gate.py"


def _unique_definitions(
    provider: FactsProvider, rule_id: str, owner: str, names: tuple[str, ...]
) -> list[Violation]:
    """Reject relocated or duplicated owner definitions using shared facts."""
    findings: list[Violation] = []
    definitions: dict[str, list[str]] = {name: [] for name in names}
    for path in _python_paths(provider, "src/apm_cli/"):
        facts, failures = checked_facts(provider, path, rule_id)
        findings.extend(failures)
        for definition in facts.definitions:
            if definition.name in definitions:
                definitions[definition.name].append(path)
    for name, paths in definitions.items():
        if paths != [owner]:
            findings.append(_summary(rule_id, owner, f"{name} must be defined only in {owner}"))
    return findings


def check_policy_identity(provider: FactsProvider) -> tuple[Violation, ...]:
    """Policy evaluation and index keys must share the dependency identity owner."""
    findings = _unique_definitions(
        provider, POLICY_RULE, IDENTITY, ("normalize_package_policy_identity",)
    )
    required = {
        IDENTITY: ("classify_package_identity_case", "case_insensitive_identity_prefix_segments"),
        MATCHER: (
            "normalize_package_policy_identity(",
            "dependency.case_insensitive_identity_prefix_segments",
        ),
        CHECKS: (
            "check_dependency_allowed(dep, policy)",
            "DependencyPolicyIndex.from_dependencies(deps)",
            "index.find_name(pkg_name)",
        ),
    }
    parallel_case = re.compile(
        r"\.lower\(\)|\.casefold\(\)|\.translate\(|maketrans\(|re\.(I|IGNORECASE)\b|is_github_hostname"
    )
    for path, needles in required.items():
        facts, failures = checked_facts(provider, path, POLICY_RULE)
        findings.extend(failures)
        text = "\n".join(facts.lines)
        if path == CHECKS:
            text = text.split("def _check_transitive_depth(", 1)[0]
        if any(needle not in text for needle in needles) or (
            path != IDENTITY and parallel_case.search(text)
        ):
            findings.append(
                _summary(
                    POLICY_RULE,
                    path,
                    "Dependency policy casing must route through models/dependency/identity.py",
                )
            )
        if path == MATCHER and facts.tree_index is not None:
            for method in ("from_dependencies", "find_name"):
                function = facts.tree_index.function(f"DependencyPolicyIndex.{method}")
                nodes = facts.tree_index.own_scope(function) if function is not None else ()
                if not any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "normalize_package_policy_identity"
                    and any(
                        keyword.arg == "case_insensitive_prefix_segments"
                        and isinstance(keyword.value, ast.Name)
                        and keyword.value.id == "prefix"
                        for keyword in node.keywords
                    )
                    for node in nodes
                ):
                    findings.append(
                        _summary(
                            POLICY_RULE,
                            path,
                            f"DependencyPolicyIndex.{method} must normalize through the identity owner",
                        )
                    )
    return tuple(findings)


def check_python_artifact_membership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Copy, lock inventory, and cleanup must use the shared bytecode predicates."""
    findings = _unique_definitions(
        provider,
        BYTECODE_RULE,
        BYTECODE,
        ("is_generated_python_artifact", "is_python_bytecode_cache_path"),
    )
    consumers = {
        BYTECODE: "is_generated_python_artifact(Path(c))",
        "src/apm_cli/install/deployed_paths.py": "is_generated_python_artifact(relative)",
        "src/apm_cli/integration/cleanup.py": "is_python_bytecode_cache_path(relative_child)",
    }
    for path, call in consumers.items():
        facts, failures = checked_facts(provider, path, BYTECODE_RULE)
        findings.extend(failures)
        if not any(call in line for line in facts.lines):
            findings.append(
                _summary(
                    BYTECODE_RULE,
                    path,
                    "Python bytecode copy, inventory, and cleanup must share security/gate.py",
                )
            )
    return tuple(findings)


RULES = (
    Rule(
        POLICY_RULE,
        "contracts_tests",
        (POLICY_RULE,),
        "Dependency policy casing and cached exact names use one identity authority.",
        check_policy_identity,
    ),
    Rule(
        BYTECODE_RULE,
        "contracts_tests",
        (BYTECODE_RULE,),
        "Python bytecode membership is shared across the deployment lifecycle.",
        check_python_artifact_membership,
    ),
)
