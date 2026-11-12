"""Keep issue-scope evidence separate from automated permission."""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-governance-evidence"
OWNER = "scripts/governance/authority.cjs"
CONSUMER = "scripts/governance/eligibility.cjs"
RUNNER = "scripts/governance/run.cjs"


def check_governance_authority(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one parser/roster reader and neutral-only downstream publication."""
    findings: list[Violation] = []
    required = {
        OWNER: (
            "function readPolicy(",
            "function parseRecord(",
            "function isResponsible(",
            "authorizes_implementation: false",
        ),
        CONSUMER: ("require('./authority.cjs')", "readPolicy(", "evaluateIssue("),
        RUNNER: ("require('./eligibility.cjs')", "evaluatePull(", "conclusion: 'neutral'"),
    }
    for path, needles in required.items():
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if facts is None:
            continue
        text = "\n".join(facts.lines)
        if any(needle not in text for needle in needles):
            findings.append(
                violation(RULE_ID, path, "Governance evidence must use its canonical owner", line=1)
            )
    for path in provider.inventory:
        if not path.startswith("scripts/governance/") or not path.endswith(".cjs"):
            continue
        facts, failures = checked_facts(provider, path, RULE_ID)
        findings.extend(failures)
        if facts is None:
            continue
        for line_number, line in enumerate(facts.lines, 1):
            split_parser = path != OWNER and re.search(
                r"function (readPolicy|parseRecord|isResponsible|evaluateIssue)\(", line
            )
            if (
                split_parser
                or re.search(r"authorizes_implementation:\s*true", line)
                or re.search(r"conclusion:\s*['\"]success['\"]", line)
            ):
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        "No parallel approval parser or automated permission",
                        line=line_number,
                    )
                )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Human scope evidence has one owner and can only produce advisory reports.",
        check_governance_authority,
    ),
)
