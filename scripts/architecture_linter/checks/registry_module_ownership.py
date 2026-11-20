"""Closed-world module ownership for ``src/apm_cli``.

Every Python module under ``src/apm_cli`` maps to exactly one canonical owner
in ``.apm/architecture/owners`` (the registry loader already rejects a module
claimed by two owners) or is listed in ``.apm/architecture/unowned-modules.json``.
That allowlist only shrinks: it must be sorted, unique and current (an entry
that is now owned or no longer exists is stale), and its length must equal
:data:`UNOWNED_MODULE_CEILING`. Assigning a module to an owner removes its
entry and lowers the ceiling; raising the ceiling is a reviewed linter change.
"""

from __future__ import annotations

import fnmatch
import json

from scripts.architecture_linter.checks.registry_shared import GROUP, _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

RULE_MODULE_OWNERSHIP = "registry_delegation.module_ownership"
GUARD_MODULE_OWNERSHIP = "registry-delegation-module-ownership"
UNOWNED_MODULES_PATH = ".apm/architecture/unowned-modules.json"
UNOWNED_MODULE_CEILING = 309

_SRC = "src/apm_cli/"


def _load_allowlist(
    provider: FactsProvider, rule_id: str
) -> tuple[tuple[str, ...] | None, Violation | None]:
    text, error = provider.source_cache.read(UNOWNED_MODULES_PATH)
    if text is None:
        return None, violation(rule_id, UNOWNED_MODULES_PATH, f"cannot read allowlist: {error}")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, violation(rule_id, UNOWNED_MODULES_PATH, f"malformed JSON: {exc.msg}")
    modules = document.get("modules") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "modules"}
        or document.get("version") != 1
        or not isinstance(modules, list)
        or not all(isinstance(item, str) for item in modules)
    ):
        return None, violation(
            rule_id,
            UNOWNED_MODULES_PATH,
            'allowlist must be {"version": 1, "modules": [<path>, ...]}',
        )
    return tuple(modules), None


def check_module_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every src/apm_cli module has one owner or a shrink-only allowlist entry."""
    rule_id = RULE_MODULE_OWNERSHIP
    registry = provider.registry
    if registry is None:
        return (violation(rule_id, UNOWNED_MODULES_PATH, "owner registry unavailable"),)
    allowlist, failure = _load_allowlist(provider, rule_id)
    if allowlist is None:
        return (failure,) if failure is not None else ()

    selectors = tuple(selector for owner in registry.owners for selector in owner.selectors)
    modules = _python_paths(provider, under=_SRC)
    owned = {
        path
        for path in modules
        if any(fnmatch.fnmatchcase(path, selector) for selector in selectors)
    }
    listed = set(allowlist)
    findings: list[Violation] = []

    if list(allowlist) != sorted(listed):
        findings.append(
            violation(rule_id, UNOWNED_MODULES_PATH, "allowlist must be sorted and unique")
        )
    findings.extend(
        violation(
            rule_id,
            path,
            "module has no canonical owner; add it to an owner's selectors in "
            ".apm/architecture/owners (new modules may not join the unowned allowlist)",
        )
        for path in modules
        if path not in owned and path not in listed
    )
    present = set(modules)
    for path in sorted(listed):
        if path in owned:
            findings.append(
                violation(
                    rule_id,
                    UNOWNED_MODULES_PATH,
                    f"stale unowned entry {path}: it now has an owner; remove it and "
                    "lower UNOWNED_MODULE_CEILING",
                )
            )
        elif path not in present:
            findings.append(
                violation(
                    rule_id,
                    UNOWNED_MODULES_PATH,
                    f"stale unowned entry {path}: no such module; remove it and "
                    "lower UNOWNED_MODULE_CEILING",
                )
            )
    if len(allowlist) != UNOWNED_MODULE_CEILING:
        findings.append(
            violation(
                rule_id,
                UNOWNED_MODULES_PATH,
                f"unowned allowlist has {len(allowlist)} entries but the ceiling is "
                f"{UNOWNED_MODULE_CEILING}; the allowlist only shrinks, so lower the "
                "ceiling with it",
            )
        )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_MODULE_OWNERSHIP,
        group=GROUP,
        guard_ids=(GUARD_MODULE_OWNERSHIP,),
        description="Every src/apm_cli module has one owner or a shrink-only unowned entry.",
        check=check_module_ownership,
    ),
)
