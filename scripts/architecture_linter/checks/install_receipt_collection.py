"""Closed-world guard: deployment receipts are collected only by their owner (#4176).

``core/deployment_ledger.py`` owns ``$APM_HOME/deployment-receipts``. Orphan
collection (``retire_receipts_of_missing_projects``) keys every candidate from the root
it records and keeps any receipt in doubt; install and prune run it. No
other product module names the receipts directory, so nothing else can
delete or rewrite a project's receipt.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_RECEIPT_COLLECTION = "install-deployment-receipt-collection"
_OWNER = "src/apm_cli/core/deployment_ledger.py"
_RECEIPTS_DIR = "deployment-receipts"
_ROUTES = (
    (_OWNER, "retire_receipts_of_missing_projects", "_receipt_key"),
    (
        "src/apm_cli/install/pipeline.py",
        "_run_integration_phase",
        "retire_receipts_of_missing_projects",
    ),
    ("src/apm_cli/commands/prune.py", "prune", "retire_receipts_of_missing_projects"),
)


def check_receipt_collection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Route receipt collection through the owner and keep the directory private."""
    rule_id = GUARD_RECEIPT_COLLECTION
    findings: list[Violation] = []
    for path, name, callee in _ROUTES:
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        found = [item for item in facts.definitions if item.name == name]
        if len(found) != 1 or not any(
            call.qualname.rsplit(".", 1)[-1] == callee
            and found[0].line <= call.line <= found[0].end_line
            for call in facts.calls
        ):
            findings.append(violation(rule_id, path, f"{name} must call {callee}"))
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        findings.extend(
            violation(
                rule_id,
                path,
                "only core/deployment_ledger.py may address the deployment-receipts directory",
                line=index,
            )
            for index, line in enumerate(getattr(facts, "lines", ()), start=1)
            if _RECEIPTS_DIR in line
        )
    return tuple(findings)
