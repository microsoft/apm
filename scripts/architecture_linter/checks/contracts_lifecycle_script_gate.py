"""Lifecycle scripts fire only through the script gate (APM_NO_SCRIPTS, org
``executables.deny_all``, project script trust).

``core/lifecycle_scripts.py`` (``build_runner_from_context``) is the only place
that constructs a ``LifecycleScriptRunner``. A runner built anywhere else fires
scripts the gate would have withheld, as ``apm lifecycle test --execute`` did.
"""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-lifecycle-script-gate"
_OWNER = "src/apm_cli/core/lifecycle_scripts.py"
_RUNNER = "LifecycleScriptRunner"


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Refuse every LifecycleScriptRunner construction outside the gate owner."""
    findings: list[Violation] = []
    owner_facts, failures = checked_facts(provider, _OWNER, RULE_ID, require_python=True)
    findings.extend(failures)
    if owner_facts is not None and not any(
        d.name == "build_runner_from_context" for d in owner_facts.definitions
    ):
        findings.append(violation(RULE_ID, _OWNER, "build_runner_from_context must exist"))
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py"):
            continue
        facts, path_failures = checked_facts(provider, path, RULE_ID, require_python=True)
        findings.extend(path_failures)
        if facts is None:
            continue
        findings.extend(
            violation(
                RULE_ID,
                path,
                f"construct {_RUNNER} only through build_runner_from_context, which applies "
                "APM_NO_SCRIPTS, org deny_all and project script trust",
                line=call.line,
                column=call.column,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _RUNNER
            and (path != _OWNER or call.scope != "build_runner_from_context")
        )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Lifecycle scripts fire only through the script gate owner.",
        _check,
    ),
)
