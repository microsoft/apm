"""Keep explicit marketplace catalog freshness routed through its owners (#3279).

``marketplace/client.py`` owns revalidation (``require_fresh``) and the
per-invocation catalog answer (``catalog_freshness_scope`` /
``fetch_or_cache``). ``InstallService.catalog_freshness`` owns which command
intents require it, derived from the shared ref-freshness policy.
"""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-catalog-freshness"
_CLIENT = "src/apm_cli/marketplace/client.py"
_MARKETPLACE_COMMANDS = "src/apm_cli/commands/marketplace/__init__.py"
_ROUTES = (
    ("src/apm_cli/install/service.py", "catalog_freshness", "catalog_freshness_scope"),
    (
        "src/apm_cli/install/service.py",
        "catalog_freshness",
        "RefFreshnessPolicy.for_install_intent",
    ),
    ("src/apm_cli/marketplace/resolver.py", "resolve_marketplace_plugin", "fetch_or_cache"),
    ("src/apm_cli/marketplace/shadow_detector.py", "detect_shadows", "fetch_or_cache"),
    (_CLIENT, "fetch_or_cache", "fetch_marketplace"),
    (_MARKETPLACE_COMMANDS, "update", "fetch_marketplace"),
)
# Closed world (#3961): every caller of the shared install entry runs inside the
# command's freshness envelope. Maps (path, caller) to the function that opens
# the envelope; an unregistered caller fails, so a new update path cannot skip
# revalidation the way `apm deps update` and `apm lock --update` did.
_INSTALL_ENTRY = "_install_apm_dependencies"
_ENVELOPE = "InstallService.catalog_freshness"
_ENVELOPED_CALLERS = {
    ("src/apm_cli/commands/install.py", "_install_apm_packages"): (
        "src/apm_cli/commands/install.py",
        "install",
    ),
    ("src/apm_cli/commands/update.py", "_run_dep_update_locked"): (
        "src/apm_cli/commands/update.py",
        "_run_dep_update",
    ),
    ("src/apm_cli/commands/deps/cli.py", "update"): ("src/apm_cli/commands/deps/cli.py", "update"),
    ("src/apm_cli/commands/lock.py", "_run_lock"): ("src/apm_cli/commands/lock.py", "_run_lock"),
}
# Revalidation-or-fail must stay explicit where the owner is invoked.
_REQUIRED_TEXT = (
    (_CLIENT, "fetch_or_cache", "require_fresh=invocation.require_fresh"),
    (_CLIENT, "fetch_marketplace", "strict = require_fresh and"),
)
# Every catalog read in ``apm marketplace update`` must revalidate.
_STRICT_CALLS = ((_MARKETPLACE_COMMANDS, "update", "fetch_marketplace", "require_fresh=True"),)
# Clearing the cache first would discard the last valid catalog (and its
# validators) before a replacement is validated.
_FORBIDDEN_CALLS = ((_MARKETPLACE_COMMANDS, "update", "clear_marketplace_cache"),)


def _single_definition(facts, function: str):
    definitions = [definition for definition in facts.definitions if definition.name == function]
    return definitions[0] if len(definitions) == 1 else None


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require scoped owner calls and forbid cache-destroying refresh shortcuts."""
    findings: list[Violation] = []
    loaded: dict[str, object] = {}

    def facts_for(path: str):
        if path not in loaded:
            facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
            findings.extend(failures)
            loaded[path] = None if failures else facts
        return loaded[path]

    for path, function, callee in _ROUTES:
        facts = facts_for(path)
        if facts is None:
            continue
        definition = _single_definition(facts, function)
        if definition is None or not any(
            call.qualname == callee and definition.line <= call.line <= definition.end_line
            for call in facts.calls
        ):
            findings.append(violation(RULE_ID, path, f"{function} must call {callee}"))
    for path in provider.inventory:
        if not (path.startswith("src/apm_cli/") and path.endswith(".py")):
            continue
        facts = facts_for(path)
        if facts is None:
            continue
        for call in facts.calls:
            if call.qualname.rsplit(".", 1)[-1] != _INSTALL_ENTRY:
                continue
            envelope = _ENVELOPED_CALLERS.get((path, call.scope))
            if envelope is None:
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        f"{call.scope} calls {_INSTALL_ENTRY} outside a registered "
                        f"{_ENVELOPE} envelope (#3961)",
                        line=call.line,
                    )
                )
                continue
            env_facts = facts_for(envelope[0])
            definition = _single_definition(env_facts, envelope[1]) if env_facts else None
            if definition is None or not any(
                c.qualname == _ENVELOPE and definition.line <= c.line <= definition.end_line
                for c in env_facts.calls
            ):
                findings.append(
                    violation(RULE_ID, envelope[0], f"{envelope[1]} must open {_ENVELOPE}")
                )
    for path, function, text in _REQUIRED_TEXT:
        facts = facts_for(path)
        if facts is None:
            continue
        definition = _single_definition(facts, function)
        body = (
            "\n".join(facts.lines[definition.line - 1 : definition.end_line]) if definition else ""
        )
        if text not in body:
            findings.append(violation(RULE_ID, path, f"{function} must keep {text!r}"))
    for path, function, callee, text in _STRICT_CALLS:
        facts = facts_for(path)
        if facts is None:
            continue
        definition = _single_definition(facts, function)
        lines = [
            call.line
            for call in facts.calls
            if definition is not None
            and call.qualname == callee
            and definition.line <= call.line <= definition.end_line
        ]
        if any(text not in facts.lines[line - 1] for line in lines):
            findings.append(
                violation(RULE_ID, path, f"every {callee} in {function} needs {text!r}")
            )
    for path, function, callee in _FORBIDDEN_CALLS:
        facts = facts_for(path)
        if facts is None:
            continue
        definition = _single_definition(facts, function)
        if definition is not None and any(
            call.qualname == callee and definition.line <= call.line <= definition.end_line
            for call in facts.calls
        ):
            findings.append(violation(RULE_ID, path, f"{function} must not call {callee}"))
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="marketplace_integrations",
        guard_ids=(RULE_ID,),
        description="Explicit marketplace refresh revalidates through the client owner once per source.",
        check=_check,
    ),
)
