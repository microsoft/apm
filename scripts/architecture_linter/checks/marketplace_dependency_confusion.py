"""Route every marketplace install admission through the #1326 owner (#3913).

``marketplace/dependency_confusion.py`` (``admit_marketplace_resolution``) is
the only reader of the ``cross_repo_misconfig_risk`` sentinel. Any function
that calls ``resolve_marketplace_plugin`` must pass the resolution through that
owner after the call, unless it is a listed read-only consumer that never
installs the resolved package.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import DefinitionFact, FileFacts, Rule, Violation

RULE_ID = "marketplace-integrations-dependency-confusion"
_OWNER_PATH = "src/apm_cli/marketplace/dependency_confusion.py"
_OWNER_FUNCTION = "admit_marketplace_resolution"
_RESOLVE = "resolve_marketplace_plugin"
_SENTINEL_PRODUCER = "src/apm_cli/marketplace/resolver.py"
_SENTINEL_READ = re.compile(r"\.cross_repo_misconfig_risk\b")
# Read-only consumers: display a canonical, or map an installed entry back to
# its lock key for removal. Neither admits a package for download.
_READ_ONLY_CONSUMERS = frozenset(
    {
        ("src/apm_cli/commands/view.py", "_display_marketplace_plugin"),
        ("src/apm_cli/commands/uninstall/engine.py", "_resolve_marketplace_packages"),
    }
)
# Install paths that must exist and route through the owner.
_REQUIRED_ADMISSIONS = (
    ("src/apm_cli/commands/install.py", "_resolve_package_references"),
    ("src/apm_cli/deps/apm_resolver.py", "_resolve_marketplace_dep"),
)


def _is_call_to(qualname: str, name: str) -> bool:
    return qualname == name or qualname.endswith(f".{name}")


def _enclosing_function(facts: FileFacts, line: int) -> DefinitionFact | None:
    candidates = [
        definition
        for definition in facts.definitions
        if definition.kind != "class" and definition.line <= line <= definition.end_line
    ]
    return max(candidates, key=lambda definition: definition.line, default=None)


def _admits_after(facts: FileFacts, function: DefinitionFact, line: int) -> bool:
    return any(
        _is_call_to(call.qualname, _OWNER_FUNCTION) and line < call.line <= function.end_line
        for call in facts.calls
    )


def _sentinel_reads(path: str, facts: FileFacts) -> list[Violation]:
    findings: list[Violation] = []
    for index, line in enumerate(facts.lines, start=1):
        if line.lstrip().startswith("#") or not _SENTINEL_READ.search(line):
            continue
        findings.append(
            violation(
                RULE_ID,
                path,
                f"only {_OWNER_FUNCTION} may read cross_repo_misconfig_risk (#1326)",
                line=index,
            )
        )
    return findings


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    owner, failures = checked_facts(provider, _OWNER_PATH, RULE_ID, require_python=True)
    findings.extend(failures)
    if not failures and not any(
        definition.name == _OWNER_FUNCTION for definition in owner.definitions
    ):
        findings.append(violation(RULE_ID, _OWNER_PATH, f"must define {_OWNER_FUNCTION}"))

    admitted: set[tuple[str, str]] = set()
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py"):
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if path not in (_OWNER_PATH, _SENTINEL_PRODUCER):
            findings.extend(_sentinel_reads(path, facts))
        for call in facts.calls:
            if not _is_call_to(call.qualname, _RESOLVE):
                continue
            function = _enclosing_function(facts, call.line)
            name = function.name if function is not None else "<module>"
            if (path, name) in _READ_ONLY_CONSUMERS:
                continue
            if function is None or not _admits_after(facts, function, call.line):
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        f"{name} calls {_RESOLVE} but does not pass the resolution "
                        f"through {_OWNER_FUNCTION} (#1326)",
                        line=call.line,
                    )
                )
            else:
                admitted.add((path, name))

    for path, name in _REQUIRED_ADMISSIONS:
        if (path, name) not in admitted:
            findings.append(
                violation(RULE_ID, path, f"{name} must resolve and admit marketplace entries")
            )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="marketplace_integrations",
        guard_ids=(RULE_ID,),
        description=(
            "Every install path that resolves a marketplace entry refuses the #1326 "
            "dependency-confusion sentinel through admit_marketplace_resolution."
        ),
        check=_check,
    ),
)
