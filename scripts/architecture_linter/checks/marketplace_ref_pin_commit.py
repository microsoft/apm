"""Record marketplace ref pins only when an install transaction commits.

``marketplace/version_pins.py`` owns the trust-on-first-use pin file.
Resolution may only check a pin and stage a new one (``enforce_ref_pin``),
and a contradicting ref is refused rather than re-recorded (#4400). The
install transaction opens one ``RefPinStage`` per attempt: only
``InstallTransaction.commit`` commits it, and rollback and dry-run discard it.
No other module writes the pin file, so a failed, previewed or refused
install never moves the trusted baseline (#4194, #4212).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _count_checks,
    _def_body_text,
    _forbid_scan,
    _load,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-ref-pin-commit"
_OWNER = "src/apm_cli/marketplace/version_pins.py"
_TRANSACTION = "src/apm_cli/install/transaction.py"
_RESOLVER = "src/apm_cli/marketplace/resolver.py"
_WRITERS = re.compile(r"\b(?:record_ref_pin|save_ref_pins|stage_ref_pin)\(")
_STAGE_OPEN = re.compile(r"\bopen_ref_pin_stage\(")
_COMMIT_CALL = "self._ref_pins.commit()"
_DISCARD_CALL = "self._ref_pins.discard()"


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inv,
            RULE_ID,
            _OWNER,
            (
                ("re", r"^def enforce_ref_pin\(", 1, "eq"),
                ("re", r"^class RefPinStage\b", 1, "eq"),
                ("re", r"^def open_ref_pin_stage\(", 1, "eq"),
                ("sub", "raise RefPinMismatchError(", 1, "eq"),
            ),
            "the ref-pin owner checks, stages, refuses and commits pins",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            RULE_ID,
            _RESOLVER,
            (("sub", "enforce_ref_pin(", 1, "eq"),),
            "resolution only checks and stages ref pins",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _WRITERS,
            "only marketplace/version_pins.py writes the ref-pin file",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            RULE_ID,
            _src_python(provider, exclude={_OWNER, _TRANSACTION}),
            _STAGE_OPEN,
            "only the install transaction opens a ref-pin stage",
            exempt=False,
        )
    )
    facts, failures = _load(provider, inv, RULE_ID, _TRANSACTION, parse=True)
    if failures:
        findings.extend(failures)
        return tuple(findings)
    commit_body = _def_body_text(facts, "commit")
    rollback_body = _def_body_text(facts, "rollback")
    complete_body = _def_body_text(facts, "complete")
    total = sum(1 for line in facts.lines if _COMMIT_CALL in line)
    problems = []
    if total != 1 or commit_body.count(_COMMIT_CALL) != 1:
        problems.append("the ref-pin stage must be committed once, in InstallTransaction.commit")
    if _DISCARD_CALL not in rollback_body:
        problems.append("rollback must discard the ref-pin stage")
    if _DISCARD_CALL not in complete_body:
        problems.append("a dry-run completion must discard the ref-pin stage")
    if problems:
        findings.append(violation(RULE_ID, _TRANSACTION, "; ".join(problems)))
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(RULE_ID,),
        description="Marketplace ref pins are recorded only when an install commits.",
        check=_check,
    ),
)

__all__ = ["RULES"]
