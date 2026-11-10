"""Keep legacy lockfile parent placement behind its canonical owner (#3510).

``deps tree`` may add installed-manifest evidence; ``deps why`` must stay on
lock data alone (OpenAPM req-rs-005).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _duplicate_definition_lines,
    _facts_for,
    _lines,
    _present_re,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import Violation

GUARD_LOCK_PARENT_PLACEMENT = "install-deployment-lock-parent-placement"
_OWNER = "src/apm_cli/deps/lock_parent_placement.py"
_TREE = "src/apm_cli/commands/deps/cli.py"
_WHY_CLI = "src/apm_cli/commands/deps/why.py"
_WHY_WALKER = "src/apm_cli/deps/why_walker.py"
_PARENT_DERIVATION = re.compile(r"\.depth - 1\b|\bparents_by_(?:unique_key|repo_url)\b")


def check_lock_parent_placement(provider: FactsProvider) -> tuple[Violation, ...]:
    """deps tree and deps why place legacy lock entries through one owner."""
    rule = GUARD_LOCK_PARENT_PLACEMENT
    facts = {}
    findings: list[Violation] = []
    for path in (_OWNER, _TREE, _WHY_CLI, _WHY_WALKER):
        facts[path], failures = _facts_for(provider, path, rule)
        findings.extend(failures)
    if findings:
        return tuple(findings)

    for name in ("place_legacy_lock_parents", "installed_manifest_children"):
        if not _present_re(facts[_OWNER], re.compile(rf"^def {name}\(")):
            findings.append(_summary(rule, _OWNER, f"lock_parent_placement.py must own {name}"))
        findings.extend(
            _duplicate_definition_lines(
                provider,
                rule_id=rule,
                prefix=_SRC_PREFIX,
                pattern=re.compile(rf"^[ \t]*def {name}\("),
                owner=_OWNER,
                message="Legacy lockfile parent placement must use its canonical owner",
                respect_exempt=False,
            )
        )

    required_calls = (
        (_TREE, re.compile(r"\bplace_legacy_lock_parents\(")),
        (_TREE, re.compile(r"\binstalled_manifest_children\(")),
        (_WHY_WALKER, re.compile(r"\bplace_legacy_lock_parents\(")),
    )
    for path, pattern in required_calls:
        if not _present_re(facts[path], pattern):
            findings.append(
                _summary(
                    rule,
                    path,
                    "deps tree/why must route parent placement through lock_parent_placement.py",
                )
            )

    for path in (_WHY_CLI, _WHY_WALKER):
        for number, line in enumerate(_lines(facts[path]), start=1):
            column = line.find("installed_manifest_children")
            if column >= 0 and EXEMPT_MARKER not in line:
                findings.append(
                    violation(
                        rule,
                        path,
                        "deps why must walk the lockfile alone (req-rs-005), not installed manifests",
                        line=number,
                        column=column + 1,
                    )
                )

    for path in (_TREE, _WHY_CLI, _WHY_WALKER):
        for number, line in enumerate(_lines(facts[path]), start=1):
            if EXEMPT_MARKER in line:
                continue
            match = _PARENT_DERIVATION.search(line)
            if match is not None:
                findings.append(
                    violation(
                        rule,
                        path,
                        "Do not re-derive lockfile parents by depth outside lock_parent_placement.py",
                        line=number,
                        column=match.start() + 1,
                    )
                )
    return tuple(findings)
