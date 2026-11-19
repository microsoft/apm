"""Regression coverage for policy casing and bytecode owner guard ports."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.contracts_dependency_policy import (
    BYTECODE_RULE,
    POLICY_RULE,
)
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]


def test_policy_and_bytecode_owner_guards_are_registered_and_clean() -> None:
    """The main registry retains both guards formerly embedded in the shell."""
    rules = {rule.id: rule for rule in registered_rules()}
    assert rules[POLICY_RULE].guard_ids == (POLICY_RULE,)
    assert rules[BYTECODE_RULE].guard_ids == (BYTECODE_RULE,)
    report = run_selected_rules(ROOT, (POLICY_RULE, BYTECODE_RULE))
    assert not report.violations
    assert not report.failures


def test_dependency_policy_case_guard_rejects_parallel_lowercase() -> None:
    """The sharded guard retains the PR's policy-local casefold mutation trap."""
    path = "src/apm_cli/policy/matcher.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = (
        source + "\n\ndef _parallel_policy_casefold(value: str) -> str:\n    return value.lower()\n"
    )
    report = run_selected_rules(ROOT, (POLICY_RULE,), source_overrides={path: mutated})
    assert any(
        finding.rule_id == POLICY_RULE and "Dependency policy casing" in finding.message
        for finding in report.violations
    )


def test_policy_index_guard_rejects_source_prefix_bypass() -> None:
    """Both cache construction and lookup must retain the source-owned prefix."""
    path = "src/apm_cli/policy/matcher.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "case_insensitive_prefix_segments=prefix\n",
        "case_insensitive_prefix_segments=0\n",
    )
    assert mutated != source
    report = run_selected_rules(ROOT, (POLICY_RULE,), source_overrides={path: mutated})
    methods = {finding.message for finding in report.violations if finding.rule_id == POLICY_RULE}
    assert methods == {
        "DependencyPolicyIndex.from_dependencies must normalize through the identity owner",
        "DependencyPolicyIndex._first must normalize through the identity owner",
    }


@pytest.mark.parametrize(
    ("original", "mutation", "message"),
    [
        (
            "            subjects.append(_unresolved_required_subject(index, pkg_name))\n"
            "        elif match.lock_key not in lock_keys:",
            "            continue\n        elif match.lock_key not in lock_keys:",
            "_check_required_packages_deployed must not skip a require entry",
        ),
        (
            "        elif match.lock_key not in lock_keys:",
            "        elif match.name not in lock_keys:",
            "_check_required_packages_deployed must address lock rows by "
            "DependencyPolicyIndex.find(...).lock_key",
        ),
    ],
    ids=["skip-unresolved-entry", "host-blind-lock-lookup"],
)
def test_required_package_guard_rejects_skip_and_host_blind_lock_lookup(
    original: str, mutation: str, message: str
) -> None:
    """#3993: required-package presence never skips and never reads the lock host-blind."""
    path = "src/apm_cli/policy/policy_checks.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(original, mutation, 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (POLICY_RULE,), source_overrides={path: mutated})
    assert message in {f.message for f in report.violations if f.rule_id == POLICY_RULE}


@pytest.mark.parametrize(
    ("path", "guard"),
    [
        ("src/apm_cli/security/gate.py", "is_generated_python_artifact(Path(c))"),
        ("src/apm_cli/install/deployed_paths.py", "is_generated_python_artifact(relative)"),
        ("src/apm_cli/integration/cleanup.py", "is_python_bytecode_cache_path(relative_child)"),
    ],
)
def test_generated_python_artifact_membership_has_one_authority(path: str, guard: str) -> None:
    """Every copy/inventory/cleanup delegate is enforced by the registered guard."""
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(guard, "False", 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (BYTECODE_RULE,), source_overrides={path: mutated})
    assert any(
        finding.rule_id == BYTECODE_RULE and "must share security/gate.py" in finding.message
        for finding in report.violations
    )
