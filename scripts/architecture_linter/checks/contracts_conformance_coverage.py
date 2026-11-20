"""Conformance coverage counts only behavioral tests.

``tests/spec_conformance/gen_statement.py`` aggregates a requirement's status
from the tests ``tests/spec_conformance/_test_kind.py`` classifies as
behavioral; a requirement with only text/shape tests is ``text-only``. This
guard pins that aggregation in the generator and checks the committed
CONFORMANCE.json: every requirement it reports ``active`` names at least one
behavioral test, and every behavioral test is one of its linked tests.
"""

from __future__ import annotations

import json

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tests-conformance-behavioral-coverage"
_GENERATOR = "tests/spec_conformance/gen_statement.py"
_STATEMENT = "CONFORMANCE.json"
_AGGREGATION = 'behavioral = [r for r in rows if r.get("kind") == BEHAVIORAL]'


def _statement_findings(provider: FactsProvider) -> list[Violation]:
    text, error = provider.source_cache.read(_STATEMENT)
    if text is None:
        return [violation(RULE_ID, _STATEMENT, f"cannot read conformance statement: {error}")]
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return [violation(RULE_ID, _STATEMENT, f"malformed JSON: {exc.msg}")]
    findings: list[Violation] = []
    for entry in document.get("requirements", []):
        req_id = entry.get("id", "<unknown>")
        behavioral = entry.get("behavioral_tests")
        if not isinstance(behavioral, list):
            findings.append(violation(RULE_ID, _STATEMENT, f"{req_id} lacks behavioral_tests"))
            continue
        if not set(behavioral) <= set(entry.get("tests", [])):
            findings.append(
                violation(
                    RULE_ID, _STATEMENT, f"{req_id} names a behavioral test it is not bound to"
                )
            )
        if entry.get("status") == "active" and not behavioral:
            findings.append(
                violation(
                    RULE_ID,
                    _STATEMENT,
                    f"{req_id} is reported active with only text/shape tests; "
                    "report it text-only or bind a behavioral test",
                )
            )
    return findings


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Only behavioral tests count as conformance coverage."""
    facts, failures = checked_facts(provider, _GENERATOR, RULE_ID, require_python=True)
    findings = list(failures)
    if not failures and not any(line.strip() == _AGGREGATION for line in facts.lines):
        findings.append(
            violation(
                RULE_ID,
                _GENERATOR,
                "status aggregation must count only behavioral tests: " + _AGGREGATION,
            )
        )
    findings.extend(_statement_findings(provider))
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Conformance status counts only behavioral tests.",
        _check,
    ),
)
