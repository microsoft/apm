"""Keep the older-CLI lockfile gate routed through its owner (#2334).

``deps/lockfile_generator.py`` owns whether the running CLI may use a
lockfile another APM generated. ``ensure_lockfile_writable`` (and so every
``LockFile.write``) enforces it; commands that write, replay or certify the
lockfile call it at entry; ``LockFile.read`` warns read-only callers.

The gate's only signal is the ``apm_version`` stamp, so ``LockFile.write``
stamps the running CLI and no other module serialises a lockfile itself (#3962).
"""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-lockfile-generator-gate"
STAMP_RULE_ID = "contracts-tooling-lockfile-generator-stamp"
_OWNER = "src/apm_cli/deps/lockfile_generator.py"
_LOCKFILE = "src/apm_cli/deps/lockfile.py"
_ROUTES = (
    (_OWNER, "_precedence", "release_precedence"),
    (_LOCKFILE, "ensure_lockfile_writable", "assert_writable"),
    (_LOCKFILE, "write", "ensure_lockfile_writable"),
    (_LOCKFILE, "read", "warn_if_newer"),
    ("src/apm_cli/commands/install.py", "install", "check_current_cli"),
    ("src/apm_cli/commands/update.py", "_run_dep_update", "require_current_cli"),
    ("src/apm_cli/commands/uninstall/cli.py", "uninstall", "require_current_cli"),
    ("src/apm_cli/commands/prune.py", "prune", "require_current_cli"),
    ("src/apm_cli/commands/lock.py", "_run_lock", "require_current_cli"),
    ("src/apm_cli/commands/pack.py", "pack_cmd", "check_current_cli"),
    ("src/apm_cli/bundle/local_bundle.py", "_read_bundle_lockfile", "warn_if_newer"),
    ("src/apm_cli/commands/audit.py", "audit", "require_current_cli"),
    ("src/apm_cli/commands/deps/cli.py", "update", "require_current_cli"),
)
_RENDER_ROUTES = (
    (_OWNER, "require_current_cli", "refusal_exit"),
    ("src/apm_cli/commands/pack.py", "pack_cmd", "refusal_exit"),
    ("src/apm_cli/commands/install.py", "install", "refusal_exit"),
)
# Callers that render a caught refusal themselves; every other command uses
# require_current_cli, so all refusals share one renderer (#3967).
_RAW_GATE_CALLERS = frozenset(
    {_OWNER, "src/apm_cli/commands/install.py", "src/apm_cli/commands/pack.py"}
)
_STAMP_ROUTES = ((_LOCKFILE, "write", "installed_apm_version"),)
# Bundle packing re-shapes an already stamped project lockfile; it never
# persists apm.lock.yaml.
_SERIALIZERS = frozenset({_LOCKFILE, "src/apm_cli/bundle/lockfile_enrichment.py"})


def _route_findings(
    provider: FactsProvider, rule_id: str, routes: tuple[tuple[str, str, str], ...]
) -> list[Violation]:
    """Require each route's function to call its owner inside its definition."""
    findings: list[Violation] = []
    for path, function, callee in routes:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        definitions = [item for item in facts.definitions if item.name == function]
        if len(definitions) != 1 or not any(
            call.qualname.rsplit(".", 1)[-1] == callee
            and definitions[0].line <= call.line <= definitions[0].end_line
            for call in facts.calls
        ):
            findings.append(violation(rule_id, path, f"{function} must call {callee}"))
    return findings


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require each gate site to call the canonical owner and render its refusal."""
    findings = _route_findings(provider, RULE_ID, _ROUTES + _RENDER_ROUTES)
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py") or path in _RAW_GATE_CALLERS:
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        findings.extend(failures)
        findings.extend(
            violation(
                RULE_ID,
                path,
                "gate commands through require_current_cli so refusals render one way",
                line=call.line,
                column=call.column,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] == "check_current_cli"
        )
    return tuple(findings)


def _check_stamp(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require the one lockfile writer to stamp apm_version and nothing to bypass it."""
    findings = _route_findings(provider, STAMP_RULE_ID, _STAMP_ROUTES)
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py") or path in _SERIALIZERS:
            continue
        facts, failures = checked_facts(provider, path, STAMP_RULE_ID, require_python=True)
        findings.extend(failures)
        findings.extend(
            violation(
                STAMP_RULE_ID,
                path,
                "persist lockfiles through LockFile.write, which stamps apm_version",
                line=call.line,
                column=call.column,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] == "to_yaml"
        )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "An older APM refuses a newer generator's lockfile through one owner.",
        _check,
    ),
    Rule(
        STAMP_RULE_ID,
        "contracts_tests",
        (STAMP_RULE_ID,),
        "Every lockfile write records the running APM through LockFile.write.",
        _check_stamp,
    ),
)
