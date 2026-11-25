"""CI validation topology guards.

* ``contracts-tooling-ci-validation-topology`` (#4115) -- every Windows
  unit-suite run goes through the CI selection owner.
* ``contracts-tooling-ci-merge-time-topology`` -- ci-integration.yml runs only
  on merge_group, workflow_dispatch and `full-integration`-labeled PRs (never
  pull_request_target or push) and shards through the build-release
  planner, merge-gate.yml branches EXPECTED_CHECKS on merge_group, and
  tests/quality/test_ci_topology.py keeps the closed-world assertions that
  every merge_group check has exactly one merge_group producer.
* ``contracts-tooling-ci-merge-queue-freshness`` -- a scheduled workflow fails
  when main moves but no merge_group run completed in the last 3 days.

merge-gate.yml's merge_group EXPECTED_CHECKS is the merge-time authority (#4116).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-ci-validation-topology"
OWNER = "scripts/ci_selections.py"
OWNER_CALL = "python -m scripts.ci_selections windows-unit"
# The required PR gate and the main-push lane must both run the owner.
CALLERS = (".github/workflows/build-release.yml", ".github/workflows/ci.yml")
WORKFLOWS = ".github/workflows/"
# The Windows unit seed is owner-private: a workflow naming it is sharding
# its own copy of the Windows unit selection.
_PRIVATE_SEED = "ci-durations/windows-unit.json"
_UNIT_ROOT = re.compile(r"(?<![\w/.-])tests/unit(?![\w/.-])")


def check_ci_validation_topology(provider: FactsProvider) -> tuple[Violation, ...]:
    """One owner defines the Windows unit selection; PR and push lanes call it."""
    findings: list[Violation] = []
    facts, failures = checked_facts(provider, OWNER, RULE_ID)
    findings.extend(failures)
    if facts is not None and facts.read_error is None:
        text = "\n".join(facts.lines)
        if any(
            needle not in text
            for needle in ('"windows-unit": (', '"tests/unit",', '"tests/test_console.py",')
        ):
            findings.append(
                violation(RULE_ID, OWNER, "Owner must define the windows-unit selection")
            )
    for path in CALLERS:
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if (
            facts is not None
            and facts.read_error is None
            and OWNER_CALL not in "\n".join(facts.lines)
        ):
            findings.append(
                violation(RULE_ID, path, f"Windows units must run through `{OWNER_CALL}`")
            )
    for path in provider.inventory:
        if not path.startswith(WORKFLOWS) or not path.endswith((".yml", ".yaml")):
            continue
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if facts is None or facts.read_error is not None:
            continue
        for line_number, line in enumerate(facts.lines, 1):
            pwsh_unit_run = "pytest" in line and "$env:" in line and _UNIT_ROOT.search(line)
            if _PRIVATE_SEED in line or pwsh_unit_run:
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        f"Windows unit selection is owned by {OWNER}; call `{OWNER_CALL}`",
                        line=line_number,
                    )
                )
    return tuple(findings)


TOPOLOGY_RULE_ID = "contracts-tooling-ci-merge-time-topology"
FRESHNESS_RULE_ID = "contracts-tooling-ci-merge-queue-freshness"

_INTEGRATION = ".github/workflows/ci-integration.yml"
_MERGE_GATE = ".github/workflows/merge-gate.yml"
_TOPOLOGY_GUARD = "tests/quality/test_ci_topology.py"
_FRESHNESS_WORKFLOW = ".github/workflows/merge-queue-freshness.yml"
_FRESHNESS_SCRIPT = "scripts/merge_queue_freshness.py"

_SECRET_REACHING_TRIGGER = re.compile(r"^  (pull_request_target|push):", re.MULTILINE)
_LABEL_GATE = (
    "github.event_name != 'pull_request' || "
    "contains(github.event.pull_request.labels.*.name, 'full-integration')"
)

_TOPOLOGY_NEEDLES: dict[str, tuple[str, ...]] = {
    _INTEGRATION: (
        "\n  merge_group:\n",
        "\n  workflow_dispatch:\n",
        "\n  pull_request:\n",
        _LABEL_GATE,
        "-p scripts.ci_sharding",
        "shard: ${{ fromJSON(needs.integration-plan.outputs.shards) }}",
        "--shard-count ${{ needs.integration-plan.outputs.count }}",
    ),
    _MERGE_GATE: ("EXPECTED_CHECKS: ${{ github.event_name == 'merge_group' && '",),
    _TOPOLOGY_GUARD: (
        "def _assert_merge_group_checks_are_produced(",
        "    assert not missing, (",
        "    assert not ambiguous, ",
        "    assert not weaker, ",
        "def _assert_merge_time_integration_authority(",
        "def _assert_global_coverage_floor_gates_merge(",
        "def test_merge_group_expected_checks_are_produced_by_merge_group_jobs(",
    ),
}

_FRESHNESS_NEEDLES: dict[str, tuple[str, ...]] = {
    _FRESHNESS_WORKFLOW: (
        "  schedule:\n",
        "run: python3 scripts/merge_queue_freshness.py",
        "WINDOW_DAYS: '3'",
        "BRANCH: main",
    ),
    _FRESHNESS_SCRIPT: (
        'INTEGRATION_WORKFLOW = "ci-integration.yml"',
        'MERGE_GROUP_EVENT = "merge_group"',
        "last merge_group run",
        "return 0 if verdict.fresh else 1",
    ),
}


def _missing_needles(
    provider: FactsProvider, rule_id: str, needles: dict[str, tuple[str, ...]], message: str
) -> list[Violation]:
    findings: list[Violation] = []
    for path, required in needles.items():
        facts, failures = checked_facts(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        text = source_text(facts) + "\n"
        missing = [needle.strip() for needle in required if needle not in text]
        if missing:
            findings.append(violation(rule_id, path, f"{message}: missing {missing}"))
    return findings


def check_merge_time_topology(provider: FactsProvider) -> tuple[Violation, ...]:
    """The merge-time gate waits only for checks merge_group really produces."""
    findings = _missing_needles(
        provider,
        TOPOLOGY_RULE_ID,
        _TOPOLOGY_NEEDLES,
        "Merge-time gate topology must stay owned by merge_group",
    )
    facts, failures = checked_facts(provider, _INTEGRATION, TOPOLOGY_RULE_ID)
    if not failures:
        text = source_text(facts)
        for match in _SECRET_REACHING_TRIGGER.finditer(text):
            findings.append(
                violation(
                    TOPOLOGY_RULE_ID,
                    _INTEGRATION,
                    f"ci-integration.yml holds secrets and must not trigger on {match.group(1)}",
                    line=text.count("\n", 0, match.start()) + 1,
                )
            )
    return tuple(findings)


def check_merge_queue_freshness(provider: FactsProvider) -> tuple[Violation, ...]:
    """A schedule fails when main moves without any merge_group run."""
    findings = _missing_needles(
        provider,
        FRESHNESS_RULE_ID,
        _FRESHNESS_NEEDLES,
        "Merge-queue freshness alarm must fail when merges bypass the queue",
    )
    facts, failures = checked_facts(provider, _FRESHNESS_WORKFLOW, FRESHNESS_RULE_ID)
    if not failures and "continue-on-error" in source_text(facts):
        findings.append(
            violation(
                FRESHNESS_RULE_ID,
                _FRESHNESS_WORKFLOW,
                "Merge-queue freshness alarm must not be suppressed with continue-on-error",
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Windows unit selection has one owner that the PR gate and main push both run.",
        check_ci_validation_topology,
    ),
    Rule(
        TOPOLOGY_RULE_ID,
        "contracts_tests",
        (TOPOLOGY_RULE_ID,),
        "merge_group EXPECTED_CHECKS is produced by merge_group jobs; merge-time "
        "integration shards through the build-release planner.",
        check_merge_time_topology,
    ),
    Rule(
        FRESHNESS_RULE_ID,
        "contracts_tests",
        (FRESHNESS_RULE_ID,),
        "A scheduled alarm fails when main moves without a merge_group run.",
        check_merge_queue_freshness,
    ),
)
