"""Guard: a re-anchor predicts a shared path's writer only through install's owners (#5048).

``commands/uninstall/local_reanchor_writers.py`` decides which package a fresh
install would record for a path the re-anchor re-ranks. Every input comes
from an existing authority, never a parallel copy:

* the order is install's ``_select_dependency_winners`` (depth, unique key),
  never replay's repo_url order;
* the providers come from a full, live audit replay (``_render_locked`` with
  ``records=False``), never a narrower scan;
* the shared-path rule is the one ``SkillIntegrator`` declares.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_REANCHOR_WRITER_PREDICTION = "install-deployment-reanchor-writer-prediction"
_OWNER = "src/apm_cli/commands/uninstall/local_reanchor_writers.py"
_REQUIRED = (
    ("_select_dependency_winners(", "order writers with install's _select_dependency_winners"),
    ("_render_locked(", "render providers through the audit replay owner"),
    ("records=False", "replay every package live"),
    ("SkillIntegrator.SHARED_PATH_RULE", "take the shared-path rule from the skill owner"),
)
_FORBIDDEN = (
    ("repo_url", "never order writers by repo_url (replay order)"),
    ("get_all_dependencies(", "never order writers by the lock's replay order"),
)


def check_reanchor_writer_prediction(provider: FactsProvider) -> tuple[Violation, ...]:
    """The writer prediction delegates order, providers and rule to their owners."""
    rule_id = GUARD_REANCHOR_WRITER_PREDICTION
    facts, failures = _facts_for(provider, _OWNER, rule_id)
    if failures:
        return tuple(failures)
    lines = tuple(getattr(facts, "lines", ()))
    findings = [
        violation(rule_id, _OWNER, f"the writer prediction must {purpose}")
        for needle, purpose in _REQUIRED
        if not any(needle in line for line in lines)
    ]
    findings.extend(
        violation(rule_id, _OWNER, f"the writer prediction must {purpose}", line=index)
        for index, line in enumerate(lines, start=1)
        for needle, purpose in _FORBIDDEN
        if needle in line
    )
    return tuple(findings)
