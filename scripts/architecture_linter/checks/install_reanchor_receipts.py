"""Guard: a re-anchored local owner keeps its receipts' hash history (#4889).

``core/deployment_reanchor.py`` moves a kept local dependency's receipts from
its old lock key to the new one. The bytes on disk and their writer are
unchanged, so the earlier hashes edit detection relies on must carry over:
the rename never forgets history, unlike a new binding (#4428).
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_REANCHOR_RECEIPT_HISTORY = "install-deployment-reanchor-receipt-history"
_OWNER = "src/apm_cli/core/deployment_reanchor.py"


def check_reanchor_receipt_history(provider: FactsProvider) -> tuple[Violation, ...]:
    """The re-anchor owner renames receipts in place and never forgets their history."""
    rule_id = GUARD_REANCHOR_RECEIPT_HISTORY
    facts, failures = _facts_for(provider, _OWNER, rule_id)
    if failures:
        return tuple(failures)
    lines = tuple(getattr(facts, "lines", ()))
    findings = [
        violation(rule_id, _OWNER, "a re-anchor must carry receipt hash history", line=index)
        for index, line in enumerate(lines, start=1)
        if "_history.forget" in line
    ]
    if not any("def rename_owners(" in line for line in lines):
        findings.append(violation(rule_id, _OWNER, "the receipt owner rename is missing"))
    return tuple(findings)
