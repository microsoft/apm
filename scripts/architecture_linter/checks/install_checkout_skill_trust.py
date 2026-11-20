"""Guard skill trust and kept-member retention across checkouts (#4229, #4230)."""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_TRUSTED_RECEIPT = "install-deployment-skill-adoption-trusted-receipt"
GUARD_KEPT_MEMBERS = "install-deployment-cleanup-retains-kept-members"
_ADOPTION = "src/apm_cli/integration/skill_adoption.py"
_CLEANUP = "src/apm_cli/integration/cleanup.py"


def _missing_fragments(
    provider: FactsProvider, rule_id: str, required: dict[str, tuple[str, ...]], message: str
) -> list[Violation]:
    failures: list[Violation] = []
    for path, fragments in required.items():
        facts, missing = _facts_for(provider, path, rule_id)
        failures.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            failures.append(_summary(rule_id, path, message))
    return failures


def check_skill_adoption_trusted_receipt(provider: FactsProvider) -> tuple[Violation, ...]:
    """Adoption bails only when every file holds its receipted bytes, never on presence."""
    failures = _missing_fragments(
        provider,
        GUARD_TRUSTED_RECEIPT,
        {
            _ADOPTION: (
                "if all_files_receipted(destination, session.receipt_held):",
                "return session.baseline(path, targets)",
                "if receipted is not None and compute_file_hash(target) == receipted:",
            ),
            "src/apm_cli/core/deployment_ledger.py": (
                "and not self._holds(path, record)",
                "return record is not None and self._holds(path, record)",
            ),
            "src/apm_cli/integration/skill_integrator.py": (
                "(lambda path: adopted.get(path) or session.baseline(path, targets))",
            ),
        },
        "Skill adoption bails only when every file still holds its receipted bytes",
    )
    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_TRUSTED_RECEIPT,
            prefix="src/apm_cli/",
            pattern=re.compile(r"\bhas_receipt\("),
            owner="",
            message="An untrusted receipt's presence never decides skill adoption",
            respect_exempt=False,
        )
    )
    return tuple(failures)


def _kept_gate_first(provider: FactsProvider) -> bool:
    index = provider.tree_index(_CLEANUP)
    function = index.function("remove_stale_deployed_files") if index is not None else None
    if function is None:
        return False
    for node in index.walk(function):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id == "_stale_list"
        ):
            first = node.body[0] if node.body else None
            test = first.test if isinstance(first, ast.If) else None
            return (
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "kept"
                and isinstance(test.func.value, ast.Name)
                and test.func.value.id == "evidence"
            )
    return False


def check_cleanup_retains_kept_members(provider: FactsProvider) -> tuple[Violation, ...]:
    """Members an install declared but left unwritten are never stale (#4230)."""
    failures = _missing_fragments(
        provider,
        GUARD_KEPT_MEMBERS,
        {
            "src/apm_cli/integration/skill_integrator.py": (
                "self._materialization_session.keep(path, self._skill_targets)",
            ),
            "src/apm_cli/install/phases/integrate.py": (
                "ctx.kept_deployed_values = _kept_skill_values(ctx)",
            ),
            "src/apm_cli/install/phases/cleanup.py": (
                'kept=getattr(ctx, "kept_deployed_values", frozenset()),',
            ),
        },
        "Skipped skill members reach the cleanup chokepoint as kept",
    )
    if not _kept_gate_first(provider):
        failures.append(
            _summary(
                GUARD_KEPT_MEMBERS,
                _CLEANUP,
                "remove_stale_deployed_files retains kept members before any deletion gate",
            )
        )
    return tuple(failures)
