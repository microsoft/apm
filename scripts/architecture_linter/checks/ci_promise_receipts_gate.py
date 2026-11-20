"""The promise-obligation receipts gate stays a required PR check (#4122).

``.github/workflows/ci.yml`` (job ``promise-obligation-receipts``) is the only
place that merges every lifecycle shard's oracle receipts and runs
``tests/quality/test_promise_obligations.py`` with every witness required.
``merge-gate.yml`` must list its check for both pull_request and merge_group,
and ``tests/quality/test_ci_topology.py`` must keep the behavioral guard that
pins the job's shape.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "ci-validation-topology-promise-receipts-gate"
_CI = ".github/workflows/ci.yml"
_MERGE_GATE = ".github/workflows/merge-gate.yml"
_TOPOLOGY = "tests/quality/test_ci_topology.py"
_CHECK = "Promise Obligation Receipts (Linux)"
_CI_FRAGMENTS = (
    "  promise-obligation-receipts:",
    f"    name: {_CHECK}",
    "        name: lifecycle-receipts-${{ env.LIFECYCLE_SHARD }}",
    "        pattern: lifecycle-receipts-*",
    '        APM_LIFECYCLE_RECEIPTS_REQUIRED: "1"',
    "      run: uv run --extra dev python -m pytest -p no:cacheprovider -q "
    "tests/quality/test_promise_obligations.py",
)
_TOPOLOGY_GUARDS = (
    "def _assert_promise_receipts_gate(",
    "def test_promise_receipts_gate_is_required(",
    "def test_promise_receipts_gate_drift_fails(",
)
_EXPECTED_CHECKS = re.compile(
    r"EXPECTED_CHECKS: \$\{\{ github\.event_name == 'merge_group' && '([^']*)' \|\| '([^']*)' \}\}"
)


def _missing_lines(provider: FactsProvider, path: str, fragments: tuple[str, ...]) -> list[str]:
    lines = provider.file_facts(path).lines
    return [
        fragment for fragment in fragments if not any(line.startswith(fragment) for line in lines)
    ]


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    for path in (_CI, _MERGE_GATE, _TOPOLOGY):
        findings.extend(checked_facts(provider, path, RULE_ID)[1])
    if findings:
        return tuple(findings)

    findings.extend(
        violation(RULE_ID, _CI, f"the receipts gate job must keep: {fragment.strip()}")
        for fragment in _missing_lines(provider, _CI, _CI_FRAGMENTS)
    )
    findings.extend(
        violation(RULE_ID, _TOPOLOGY, f"the receipts gate guard must keep: {fragment}")
        for fragment in _missing_lines(provider, _TOPOLOGY, _TOPOLOGY_GUARDS)
    )
    matches = [
        (number, match)
        for number, line in enumerate(provider.file_facts(_MERGE_GATE).lines, start=1)
        if (match := _EXPECTED_CHECKS.search(line))
    ]
    if len(matches) != 1:
        findings.append(violation(RULE_ID, _MERGE_GATE, "expected one EXPECTED_CHECKS expression"))
        return tuple(findings)
    number, match = matches[0]
    for event, checks in (("merge_group", match[1]), ("pull_request", match[2])):
        if checks.split(",").count(_CHECK) != 1:
            findings.append(
                violation(
                    RULE_ID,
                    _MERGE_GATE,
                    f"{_CHECK!r} must be a required {event} check in EXPECTED_CHECKS",
                    line=number,
                )
            )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "The promise-obligation receipts gate stays a required PR and merge-queue check.",
        _check,
    ),
)
