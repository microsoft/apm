"""Guard: one owner ranks the parent a re-parented local row records (#5161).

``_ranked_parent`` in ``commands/uninstall/local_reanchor.py`` holds the rule
``reparent_rows`` applies: the nearest declaring parent wins, ties to the
lowest key as install ranks them. Uninstall's ``plan_local_reanchor`` and
prune's ``_reparent_survivors`` both go through ``reparent_rows``, and no
second nearest-parent ranking exists in the commands.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_LOCAL_REPARENT_RULE = "install-deployment-local-reparent-rule"
_OWNER = "src/apm_cli/commands/uninstall/local_reanchor.py"
_CALLERS = (
    (_OWNER, "plan_local_reanchor"),
    ("src/apm_cli/commands/prune.py", "_reparent_survivors"),
)
_SCOPES = ("src/apm_cli/commands/",)
_RANKING = re.compile(r"\bmin\(\s*nearest\b")


def _span(facts, name: str) -> tuple[int, int] | None:
    found = [item for item in facts.definitions if item.name == name]
    return (found[0].line, found[0].end_line) if len(found) == 1 else None


def check_local_reparent_rule(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every local re-parent ranks parents through reparent_rows alone."""
    rule_id = GUARD_LOCAL_REPARENT_RULE
    findings: list[Violation] = []
    owner, failures = _facts_for(provider, _OWNER, rule_id)
    if failures:
        return tuple(failures)
    owner_span = _span(owner, "_ranked_parent")
    if owner_span is None or _span(owner, "reparent_rows") is None:
        return (
            violation(
                rule_id, _OWNER, "local_reanchor.py must define _ranked_parent and reparent_rows"
            ),
        )
    for path, caller in _CALLERS:
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        span = None if failures else _span(facts, caller)
        if span is None or not any(
            call.qualname.rsplit(".", 1)[-1] == "reparent_rows" and span[0] <= call.line <= span[1]
            for call in facts.calls
        ):
            findings.append(
                violation(rule_id, path, f"{caller} must re-parent through reparent_rows")
            )
    for scope in _SCOPES:
        for path in _python_paths(provider, scope):
            facts, failures = _facts_for(provider, path, rule_id)
            findings.extend(failures)
            if failures:
                continue
            for number, line in enumerate(getattr(facts, "lines", ()), start=1):
                inside = path == _OWNER and owner_span[0] <= number <= owner_span[1]
                if _RANKING.search(line) and not inside:
                    findings.append(
                        violation(
                            rule_id,
                            path,
                            "a nearest-parent ranking outside _ranked_parent",
                            line=number,
                        )
                    )
    return tuple(findings)
