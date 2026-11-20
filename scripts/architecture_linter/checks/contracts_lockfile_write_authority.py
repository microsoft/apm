"""Keep lockfile persistence behind one write authority with a closed writer set (#4086).

``deps/lockfile_write_authority.py`` decides when ``apm.lock.yaml`` reaches
disk. This guard keeps that decision closed-world:

* the :class:`LockfileWriter` vocabulary is exactly the registered writer set,
  and each member's command entry point opens its scope (``@lockfile_writer``);
  nothing else opens a writer scope;
* ``LockFile.write`` stages through the authority and ``LockFile.read`` sees
  what it staged;
* every lockfile write call site is a registered commit site, so a new writer
  cannot appear without a registry change;
* lockfile removal and rollback go through the authority, never a raw
  ``unlink`` or ``write_bytes`` that a staged document could undo.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.contracts_lockfile_generator import _route_findings
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-lockfile-write-authority"
_OWNER = "src/apm_cli/deps/lockfile_write_authority.py"
_LOCKFILE = "src/apm_cli/deps/lockfile.py"

# The closed writer set: (entry point path, function, LockfileWriter member).
# A function either carries ``@lockfile_writer(LockfileWriter.M)`` or wraps the
# handler it calls with ``lockfile_writer(LockfileWriter.M)`` in its body.
WRITER_ENTRY_POINTS = (
    ("src/apm_cli/commands/install.py", "_install_apm_packages", "INSTALL"),
    ("src/apm_cli/install/service.py", "run", "INSTALL"),
    ("src/apm_cli/commands/install.py", "_handle_mcp_install", "INSTALL_MCP"),
    ("src/apm_cli/commands/install.py", "install", "INSTALL_BUNDLE"),
    ("src/apm_cli/commands/lock.py", "_run_lock", "LOCK"),
    ("src/apm_cli/commands/update.py", "_run_dep_update", "UPDATE"),
    ("src/apm_cli/commands/deps/cli.py", "update", "UPDATE"),
    ("src/apm_cli/commands/uninstall/cli.py", "uninstall", "UNINSTALL"),
    ("src/apm_cli/commands/prune.py", "prune", "PRUNE"),
    ("src/apm_cli/commands/compile/cli.py", "_run_compilation", "COMPILE"),
)

# Every function that hands a lockfile to LockFile.write/save (innermost def).
COMMIT_SITES = frozenset(
    {
        ("src/apm_cli/commands/prune.py", "prune"),
        ("src/apm_cli/commands/uninstall/cli.py", "_persist_uninstall_lockfile"),
        ("src/apm_cli/commands/uninstall/cli.py", "_abort_if_retained_target_cleanup_paths"),
        ("src/apm_cli/commands/update.py", "_annotate_lockfile_revision_tags"),
        ("src/apm_cli/install/manifest_reconcile.py", "reconcile_project_deployed_state"),
        ("src/apm_cli/install/phases/claude_plugins.py", "run"),
        ("src/apm_cli/install/phases/lockfile.py", "_write_if_changed"),
        ("src/apm_cli/install/phases/lockfile.py", "_reconcile_target_deployed_files"),
        ("src/apm_cli/install/phases/post_deps_local.py", "run"),
        ("src/apm_cli/install/mcp/integration.py", "run_mcp_integration"),
        ("src/apm_cli/integration/mcp_integrator.py", "update_lockfile"),
        ("src/apm_cli/integration/lsp_integrator.py", "update_lockfile"),
        # A private replay snapshot, written at once under lockfile_scratch_writes.
        ("src/apm_cli/integration/cleanup.py", "_render_locked"),
        # Local-bundle and --mcp handlers, inside their own writer scopes until
        # #4085 runs them through the install pipeline.
        ("src/apm_cli/install/local_bundle_handler.py", "install_local_bundle"),
        ("src/apm_cli/install/local_bundle_hook_trust.py", "_drop_lock_rows"),
        ("src/apm_cli/install/mcp/integration.py", "run_owned_mcp_integration"),
        ("src/apm_cli/install/lsp/integration.py", "run_owned_lsp_integration"),
    }
)
_ROUTES = (
    (_LOCKFILE, "write", "stage_lockfile"),
    (_LOCKFILE, "write", "staged_lockfile_text"),
    (_LOCKFILE, "read", "has_staged_lockfile"),
    (_OWNER, "_commit", "ensure_lockfile_writable"),
    ("src/apm_cli/integration/cleanup.py", "_render_locked", "lockfile_scratch_writes"),
)
_SCOPE_OPENERS = frozenset({"lockfile_writer", "lockfile_write_scope"})
_WRITE_METHODS = frozenset({"write", "save"})
_ENUM_MEMBER = re.compile(r"^    ([A-Z_]+) = \"[a-z-]+\"$", re.MULTILINE)
_DECORATOR_REACH = 12
_RAW_REMOVAL = re.compile(
    r"\b(lockfile_path|lock_path|_lock_path)\.(unlink|write_bytes|write_text)\("
)
# Filelock activity markers named lock_path, not lockfiles.
_RAW_REMOVAL_EXEMPT = frozenset({"src/apm_cli/install/resolution_staging.py"})


def _call_text(lines: tuple[str, ...], line: int, column: int) -> str:
    """The call starting at (*line*, *column*) through its balanced parenthesis."""
    text = "\n".join(lines[line - 1 :])[column:]
    depth = 0
    opened = False
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
            opened = True
        elif char == ")":
            depth -= 1
            if opened and depth == 0:
                return text[: index + 1]
    return text


def _function(facts, name: str):
    found = [item for item in facts.definitions if item.name == name]
    return found[0] if len(found) == 1 else None


def _check_vocabulary(provider: FactsProvider) -> list[Violation]:
    facts, failures = checked_facts(provider, _OWNER, RULE_ID, require_python=True)
    if failures:
        return list(failures)
    members = set(_ENUM_MEMBER.findall("\n".join(facts.lines)))
    registered = {member for _path, _function_name, member in WRITER_ENTRY_POINTS}
    if members != registered:
        return [
            violation(
                RULE_ID,
                _OWNER,
                f"LockfileWriter members {sorted(members)} must equal the registered "
                f"writer set {sorted(registered)}",
            )
        ]
    return []


def _opener_calls(facts, member: str, definition) -> list:
    expected = f"lockfile_writer(LockfileWriter.{member})"
    return [
        call
        for call in facts.calls
        if call.qualname == "lockfile_writer"
        and definition.line <= call.line <= definition.end_line
        and expected in _call_text(facts.lines, call.line, call.column)
    ]


def _check_entry_points(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path, name, member in WRITER_ENTRY_POINTS:
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        definition = _function(facts, name)
        expected = f"lockfile_writer(LockfileWriter.{member})"
        if definition is None or (
            expected not in definition.decorators and not _opener_calls(facts, member, definition)
        ):
            findings.append(violation(RULE_ID, path, f"{name} must open {expected}"))
    return findings


def _registered_opener(facts, path: str, call) -> bool:
    """Whether *call* opens a scope as a registered entry point's decorator or body."""
    for entry_path, name, member in WRITER_ENTRY_POINTS:
        definition = _function(facts, name) if entry_path == path else None
        if definition is None:
            continue
        expected = f"lockfile_writer(LockfileWriter.{member})"
        decorates = (
            expected in definition.decorators
            and definition.line - _DECORATOR_REACH <= call.line < definition.line
        )
        if decorates or call in _opener_calls(facts, member, definition):
            return True
    return False


def _check_sources(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py") or path in {_OWNER, _LOCKFILE}:
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        for call in facts.calls:
            head, _, method = call.qualname.rpartition(".")
            if (method or call.qualname) in _SCOPE_OPENERS:
                if not _registered_opener(facts, path, call):
                    findings.append(
                        violation(
                            RULE_ID,
                            path,
                            f"{call.scope or '<module>'} opens a lockfile writer scope outside "
                            "the registered writer set",
                            line=call.line,
                        )
                    )
                continue
            if not head or method not in _WRITE_METHODS:
                continue
            if "lock" not in _call_text(facts.lines, call.line, call.column).lower():
                continue
            if (path, call.scope) not in COMMIT_SITES:
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        f"{call.scope or '<module>'} writes a lockfile outside the "
                        "registered commit sites of the lockfile write authority",
                        line=call.line,
                        column=call.column,
                    )
                )
        if path in _RAW_REMOVAL_EXEMPT:
            continue
        text = "\n".join(facts.lines)
        findings.extend(
            violation(
                RULE_ID,
                path,
                "remove or restore lockfiles through remove_lockfile/restore_lockfile_bytes",
                line=text.count("\n", 0, match.start()) + 1,
            )
            for match in _RAW_REMOVAL.finditer(text)
        )
    return findings


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep lockfile writers, commit sites and persistence closed-world."""
    return tuple(
        [
            *_check_vocabulary(provider),
            *_check_entry_points(provider),
            *_route_findings(provider, RULE_ID, _ROUTES),
            *_check_sources(provider),
        ]
    )


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Lockfiles persist through one write authority with a closed writer set.",
        _check,
    ),
)
