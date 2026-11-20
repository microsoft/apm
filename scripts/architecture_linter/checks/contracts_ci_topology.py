"""Route every Windows unit-suite run through the CI selection owner (#4115)."""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
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


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Windows unit selection has one owner that the PR gate and main push both run.",
        check_ci_validation_topology,
    ),
)
