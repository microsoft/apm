"""Route the root project ``marketplaces:`` map through its owner (#2903).

``marketplace/project_marketplaces.py`` owns the root project's map: it alone
binds the map for a command, resolves a declared name ahead of the registry
(through ``registry.get_marketplace_by_name``) and refuses a transitive name
the root does not declare. The resolver refuses a dependency's map with the
declaration owner's message. Project-origin sources never leave memory, so
``ORIGIN_PROJECT`` is read only where it is defined, cache-keyed and created.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

RULE_ID = "marketplace-integrations-project-map"
_OWNER_PATH = "src/apm_cli/marketplace/project_marketplaces.py"
_OWNER_FUNCTIONS = (
    "project_marketplace_scope",
    "bind_project_marketplaces",
    "project_marketplace",
    "require_root_declared_marketplace",
)
_ORIGIN_READERS = frozenset(
    {
        "src/apm_cli/marketplace/models.py",
        "src/apm_cli/marketplace/client.py",
        _OWNER_PATH,
    }
)
_ORIGIN_TOKEN = re.compile(r"\bORIGIN_PROJECT\b")
# (path, function, owner call) that must exist.
_REQUIRED_CALLS = (
    ("src/apm_cli/marketplace/registry.py", "get_marketplace_by_name", "project_marketplace"),
    ("src/apm_cli/install/service.py", "run", "bind_project_marketplaces"),
    (
        "src/apm_cli/commands/install.py",
        "_validate_and_add_packages_to_apm_yml",
        "bind_project_marketplaces",
    ),
    ("src/apm_cli/commands/outdated.py", "outdated", "bind_project_marketplaces"),
    (
        "src/apm_cli/deps/apm_resolver.py",
        "_resolve_marketplace_dep",
        "require_root_declared_marketplace",
    ),
    (
        "src/apm_cli/deps/apm_resolver.py",
        "_activate_validated_package",
        "dependency_marketplaces_error",
    ),
)


def _is_call_to(qualname: str, name: str) -> bool:
    return qualname == name or qualname.endswith(f".{name}")


def _calls_within(facts: FileFacts, function: str, target: str) -> bool:
    for definition in facts.definitions:
        if definition.kind == "class" or definition.name != function:
            continue
        if any(
            _is_call_to(call.qualname, target)
            and definition.line <= call.line <= definition.end_line
            for call in facts.calls
        ):
            return True
    return False


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    owner, failures = checked_facts(provider, _OWNER_PATH, RULE_ID, require_python=True)
    findings.extend(failures)
    if not failures:
        defined = {definition.name for definition in owner.definitions}
        findings.extend(
            violation(RULE_ID, _OWNER_PATH, f"must define {name}")
            for name in _OWNER_FUNCTIONS
            if name not in defined
        )

    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py") or path == _OWNER_PATH:
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if path not in _ORIGIN_READERS:
            findings.extend(
                violation(
                    RULE_ID,
                    path,
                    "ORIGIN_PROJECT sources are created only by project_marketplaces (#2903)",
                    line=index,
                )
                for index, line in enumerate(facts.lines, start=1)
                if _ORIGIN_TOKEN.search(line) and not line.lstrip().startswith("#")
            )
        findings.extend(
            violation(
                RULE_ID,
                path,
                "bind the root marketplaces: map with bind_project_marketplaces",
                line=call.line,
            )
            for call in facts.calls
            if _is_call_to(call.qualname, "project_marketplace_scope")
        )

    for path, function, target in _REQUIRED_CALLS:
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
        elif not _calls_within(facts, function, target):
            findings.append(violation(RULE_ID, path, f"{function} must call {target} (#2903)"))
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="marketplace_integrations",
        guard_ids=(RULE_ID,),
        description=(
            "The root project marketplaces: map is bound, resolved ahead of the registry "
            "and enforced for transitive and dependency declarations only through "
            "marketplace/project_marketplaces.py."
        ),
        check=_check,
    ),
)
