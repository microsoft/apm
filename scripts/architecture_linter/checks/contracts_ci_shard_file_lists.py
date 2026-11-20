"""Sharded CI jobs run explicit per-shard test file lists (#4120, PERF-H8).

``scripts/ci_sharding.py`` (``shard_test_files``) owns each shard's file
list; ``scripts/ci_selections.py`` routes its sharded selections through it.
A sharded pytest command that collects whole roots and deselects (the node
level ``--shard-index`` plugin, pytest-split ``--splits``) is a second
selection authority. The drift guard in ``tests/quality/test_ci_topology.py``
compares every job's file lists with a full collect on the same SHA; this
rule keeps that comparison wired.
"""

from __future__ import annotations

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-ci-shard-file-lists"
OWNER = "scripts/ci_sharding.py"
DRIFT_GUARD = "tests/quality/test_ci_topology.py"
WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/build-release.yml",
    ".github/workflows/ci-integration.yml",
)
EMITTER = "scripts.ci_sharding files"
REQUIRED: dict[str, tuple[str, ...]] = {
    OWNER: (
        "def shard_test_files(",
        "plan = shard_test_files(",
        "def _write_shard_files(",
    ),
    "scripts/ci_selections.py": ("shard_test_files(",),
    DRIFT_GUARD: (
        "def test_shard_file_lists_match_full_collection(",
        "def test_windows_unit_shard_file_lists_match_full_collection(",
        '"--shard-inventory-out",',
        "ci_sharding.shard_test_files(",
        "assert not dropped, (",
        "assert not duplicated,",
    ),
}
# A shard selector belongs to the nearest command line above it.
_COMMAND_MARKERS = ("pytest", EMITTER, "scripts.ci_selections")
_LOOKBACK = 8


def _owning_command(lines: list[str], index: int) -> str | None:
    for line in reversed(lines[max(0, index - _LOOKBACK) : index + 1]):
        if any(marker in line for marker in _COMMAND_MARKERS):
            return line
    return None


def _workflow_findings(path: str, lines: list[str]) -> list[Violation]:
    findings: list[Violation] = []
    if not any(EMITTER in line for line in lines):
        findings.append(violation(RULE_ID, path, f"Sharded jobs must run `{EMITTER}`"))
    for number, line in enumerate(lines, 1):
        if "--splits " in line:
            findings.append(
                violation(RULE_ID, path, "pytest-split collects every root", line=number)
            )
        if "--shard-index" not in line:
            continue
        command = _owning_command(lines, number - 1)
        if (
            command is not None
            and EMITTER not in command
            and "scripts.ci_selections" not in command
        ):
            findings.append(
                violation(
                    RULE_ID,
                    path,
                    f"A sharded pytest run must pass its `{EMITTER}` list, not whole roots",
                    line=number,
                )
            )
    return findings


def check_ci_shard_file_lists(provider: FactsProvider) -> tuple[Violation, ...]:
    """Owner, routed selections, drift guard and workflows stay wired to file lists."""
    findings: list[Violation] = []
    for path, needles in REQUIRED.items():
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if facts is None or facts.read_error is not None:
            continue
        text = "\n".join(facts.lines)
        missing = [needle for needle in needles if needle not in text]
        if missing:
            findings.append(
                violation(RULE_ID, path, f"Shard file-list wiring lost: {', '.join(missing)}")
            )
    for path in WORKFLOWS:
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if facts is not None and facts.read_error is None:
            findings.extend(_workflow_findings(path, list(facts.lines)))
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Sharded CI jobs run one owner's explicit per-shard file lists, checked by a drift guard.",
        check_ci_shard_file_lists,
    ),
)
