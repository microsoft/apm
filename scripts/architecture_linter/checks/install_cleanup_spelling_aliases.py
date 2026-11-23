"""Closed-world guard: cleanup reads path spellings through one alias index (#4295, #4815).

On a case- or normalization-insensitive filesystem two claim spellings can
name one file. ``utils/paths.SpellingAliasIndex`` alone decides that:
``DeploymentEvidence._equivalent_keys`` asks ``SpellingAliasIndex.aliases``
for every recorded spelling of a claim, so consent and the deletion verdict
pool the hashes and owners of one file. ``integration/cleanup.py`` never
re-derives spelling equivalence with its own filesystem probes.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_CLEANUP_SPELLING_ALIASES = "install-deployment-cleanup-spelling-aliases"
_CLEANUP = "src/apm_cli/integration/cleanup.py"
_OWNER_FUNCTION = "_equivalent_keys"
# Probes that decide whether two spellings name one file outside the index.
_BYPASSES = frozenset({"samefile", "normcase", "casefold", "physical_alias", "stored_spelling"})


def check_cleanup_spelling_aliases(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep cleanup's spelling equivalence behind ``SpellingAliasIndex.aliases``."""
    rule_id = GUARD_CLEANUP_SPELLING_ALIASES
    facts, failures = _facts_for(provider, _CLEANUP, rule_id)
    if failures:
        return tuple(failures)
    findings: list[Violation] = []
    owner = [item for item in facts.definitions if item.name == _OWNER_FUNCTION]
    if len(owner) != 1 or not any(
        call.qualname.rsplit(".", 1)[-1] == "aliases"
        and owner[0].line <= call.line <= owner[0].end_line
        for call in facts.calls
    ):
        findings.append(
            violation(
                rule_id,
                _CLEANUP,
                f"{_OWNER_FUNCTION} must read spellings through SpellingAliasIndex.aliases",
            )
        )
    findings.extend(
        violation(
            rule_id,
            _CLEANUP,
            f"{call.scope or '<module>'} decides spelling equivalence outside "
            "SpellingAliasIndex.aliases",
            line=call.line,
            column=call.column,
        )
        for call in facts.calls
        if call.qualname.rsplit(".", 1)[-1] in _BYPASSES
    )
    return tuple(findings)
