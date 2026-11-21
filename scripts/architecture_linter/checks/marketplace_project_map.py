"""Route the root project ``marketplaces:`` map through its owner (#2903).

``marketplace/project_marketplaces.py`` owns the root project's map: it alone
binds the map for a command, resolves a declared name ahead of the registry
(through ``registry.get_marketplace_by_name``), refuses a transitive name the
root does not declare, and decides when a lock row recorded from another
origin re-resolves through the map. ``marketplace.allow`` judges locked and
freshly resolved catalog origins, so a lock cannot bypass it. The resolver refuses a dependency's map with the
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
    "declared_origin_conflict",
    "project_wins_locked_origin",
    "rebound_by_project_map",
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
    ("src/apm_cli/commands/update.py", "_reverified_lock", "bind_project_marketplaces"),
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
    # The file wins over a lock row from another origin; --frozen refuses it.
    ("src/apm_cli/drift.py", "_declared_origin_conflict", "declared_origin_conflict"),
    (
        "src/apm_cli/marketplace/catalog_origin.py",
        "require_locked_catalog_origin",
        "project_wins_locked_origin",
    ),
    (
        "src/apm_cli/marketplace/catalog_origin.py",
        "reverify_locked_provenance",
        "rebound_by_project_map",
    ),
    # marketplace.allow judges locked and fresh catalog origins, not only apm.yml.
    ("src/apm_cli/install/phases/policy_gate.py", "run", "marketplace_resolution_origins"),
    ("src/apm_cli/policy/policy_checks.py", "run_policy_checks", "marketplace_resolution_origins"),
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
