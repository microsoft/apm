"""Keep the older-CLI lockfile gate routed through its owner (#2334).

``deps/lockfile_generator.py`` owns whether the running CLI may use a
lockfile another APM generated. ``ensure_lockfile_writable`` (and so every
``LockFile.write``) enforces it; commands that write, replay or certify the
lockfile call it at entry; ``LockFile.read`` warns read-only callers.
"""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-lockfile-generator-gate"
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
    ("src/apm_cli/commands/pack.py", "pack_cmd", "require_current_cli"),
    ("src/apm_cli/commands/audit.py", "audit", "require_current_cli"),
    ("src/apm_cli/commands/deps/cli.py", "update", "require_current_cli"),
)


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require each gate site to call the canonical owner inside its definition."""
    findings: list[Violation] = []
    for path, function, callee in _ROUTES:
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        definitions = [item for item in facts.definitions if item.name == function]
        if len(definitions) != 1 or not any(
            call.qualname.rsplit(".", 1)[-1] == callee
            and definitions[0].line <= call.line <= definitions[0].end_line
            for call in facts.calls
        ):
            findings.append(violation(RULE_ID, path, f"{function} must call {callee}"))
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "An older APM refuses a newer generator's lockfile through one owner.",
        _check,
    ),
)
