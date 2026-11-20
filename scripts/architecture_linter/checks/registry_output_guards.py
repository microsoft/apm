"""Owner-registry guards for console construction, library output and experimental flags.

======================================  ==================================
Owner guard (executed exactly once)     Decision
======================================  ==================================
registry-delegation-console-            Rich consoles and tables come from
construction                            utils/console.py (#3994, #4089)
registry-delegation-experimental-flags  Every experimental flag has a
                                        reader; experimental-only notices
                                        stay verbose; GA help is not
                                        marked experimental (#4089)
======================================  ==================================

:func:`library_output_findings` is the adapter-output sub-check of
``registry_delegation.output_diagnostics`` (owned by
:mod:`registry_owner_guards`): MCP client adapters and the safe installer
emit through ``CommandLogger`` (#4089).

Every check consumes only :class:`FactsProvider` cached lines; no rule reads a
file or shells out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import (
    _SRC,
    GROUP,
    _definition_span,
    _python_paths,
    _scope_lines,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

# --- console construction ----------------------------------------------------

_CONSOLE_OWNER = "src/apm_cli/utils/console.py"
_CONSOLE_CONSTRUCTION = (
    (
        re.compile(r"^\s*from\s+rich\.console\s+import\b.*\bConsole\b"),
        re.compile(r"(?<![\w.])(?:rich\.console\.)?Console\("),
        "build Rich consoles with apm_cli.utils.console.new_console() or "
        "_get_console(); a direct rich Console renders Unicode boxes",
    ),
    (
        re.compile(r"^\s*from\s+rich\.table\s+import\b.*\bTable\b"),
        re.compile(r"(?<![\w.])(?:rich\.table\.)?Table\("),
        "build Rich tables with apm_cli.utils.console.new_table(); a direct rich "
        "Table picks its own box",
    ),
    (
        re.compile(r"^\s*from\s+rich(\.box\s+import\b|\s+import\b.*\bbox\b)"),
        None,
        "table boxes are chosen by apm_cli.utils.console.new_table(), which draws ASCII",
    ),
)


def _check_console_construction(provider: FactsProvider) -> Iterable[Violation]:
    """Rich consoles and tables come from utils/console.py so output stays ASCII."""
    rule_id = "registry_delegation.console_construction"
    findings: list[Violation] = []
    for path in _python_paths(provider, under=_SRC):
        if path == _CONSOLE_OWNER:
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        for number, line in enumerate(facts.lines, start=1):
            for import_pattern, call_pattern, message in _CONSOLE_CONSTRUCTION:
                if import_pattern.search(line) or (call_pattern and call_pattern.search(line)):
                    findings.append(violation(rule_id, path, message, line=number))
    return findings


# --- library (adapter) output ------------------------------------------------

_LIBRARY_OUTPUT_ROOTS = ("src/apm_cli/adapters/",)
_LIBRARY_OUTPUT_FILES = frozenset({"src/apm_cli/core/safe_installer.py"})
_RAW_OUTPUT = re.compile(
    r"(?<![\w.])print\(|\bclick\.s?echo\(|\b_rich_\w+\b|\bsys\.std(?:out|err)\.write\("
)


def library_output_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Adapters and the safe installer emit through CommandLogger, never raw (#4089)."""
    findings: list[Violation] = []
    paths = [
        path
        for path in _python_paths(provider, under=_SRC)
        if path.startswith(_LIBRARY_OUTPUT_ROOTS) or path in _LIBRARY_OUTPUT_FILES
    ]
    for path in paths:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        findings.extend(
            violation(
                rule_id,
                path,
                "emit through CommandLogger (library_logger() or the passed logger), "
                "not print, click.echo or _rich_* helpers",
                line=number,
            )
            for number, line in enumerate(facts.lines, start=1)
            if not line.lstrip().startswith("#") and _RAW_OUTPUT.search(line)
        )
    return findings


# --- experimental flags ------------------------------------------------------

_FLAG_REGISTRY = "src/apm_cli/core/experimental.py"
_FLAG_ENTRY = re.compile(r'^\s{4}"(\w+)": ExperimentalFlag\(')
_DIRECT_READER = re.compile(r"""\b(?:is_enabled\(|experimental_flag\s*=\s*)["'](\w+)["']""")
_FLAG_NAME_CONSTANT = re.compile(r"""^FLAG_NAME\s*=\s*["'](\w+)["']""")
_RECONCILIATION_SCOPE = "src/apm_cli/install/helpers/reconciliation_scope.py"
_INACTIVE_RESOLVER_NOTICE = "_record_inactive_resolver_skip"
_AUDIT_COMMAND = "src/apm_cli/commands/audit.py"
_AUDIT_HELP = (
    'help="Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations"'
)
_AUDIT_OPTION = re.compile(r'^\s{4}"(--[\w/-]+)",?$')
_AUDIT_EXPERIMENTAL_OPTIONS = frozenset(
    {"--external", "--external-sarif", "--external-llm/--no-external-llm", "--external-args"}
)


def _flag_readers(provider: FactsProvider, rule_id: str) -> tuple[set[str], list[Violation]]:
    readers: set[str] = set()
    failures: list[Violation] = []
    for path in _python_paths(provider, under=_SRC):
        if path == _FLAG_REGISTRY:
            continue
        facts, path_failures = checked_facts(provider, path, rule_id, require_python=True)
        if path_failures:
            failures.extend(path_failures)
            continue
        source = "\n".join(facts.lines)
        readers.update(_DIRECT_READER.findall(source))
        if "is_enabled(FLAG_NAME" in source:
            readers.update(
                match.group(1)
                for line in facts.lines
                if (match := _FLAG_NAME_CONSTANT.match(line)) is not None
            )
    return readers, failures


def _unread_flag_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Every registered flag has a runtime reader outside the registry (H6)."""
    facts, failures = checked_facts(provider, _FLAG_REGISTRY, rule_id, require_python=True)
    if failures:
        return list(failures)
    readers, reader_failures = _flag_readers(provider, rule_id)
    findings = list(reader_failures)
    for number, line in enumerate(facts.lines, start=1):
        match = _FLAG_ENTRY.match(line)
        if match is not None and match.group(1) not in readers:
            findings.append(
                violation(
                    rule_id,
                    _FLAG_REGISTRY,
                    f"experimental flag '{match.group(1)}' has no reader: read it with "
                    'is_enabled("<name>"), experimental_flag="<name>" or FLAG_NAME, '
                    "or remove the flag",
                    line=number,
                )
            )
    return findings


def _inactive_resolver_notice_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """The inactive experimental resolver skip is a verbose-only notice (DX8, #3994)."""
    facts, failures = checked_facts(provider, _RECONCILIATION_SCOPE, rule_id, require_python=True)
    if failures:
        return list(failures)
    span = _definition_span(facts, _INACTIVE_RESOLVER_NOTICE)
    gated = span is not None and any(
        re.search(r"\bif\b.*\bnot diagnostics\.verbose\b.*:\s*$", text)
        for _number, text in _scope_lines(facts, span)
    )
    if gated:
        return []
    return [
        violation(
            rule_id,
            _RECONCILIATION_SCOPE,
            f"{_INACTIVE_RESOLVER_NOTICE} must return unless diagnostics.verbose: the "
            "skip is expected for every user who has not opted in",
            line=span[0] if span else 1,
        )
    ]


def _audit_help_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Audit help names its modes; only external-scanner options are experimental (H8)."""
    facts, failures = checked_facts(provider, _AUDIT_COMMAND, rule_id, require_python=True)
    if failures:
        return list(failures)
    findings: list[Violation] = []
    if not any(line.strip().startswith(_AUDIT_HELP) for line in facts.lines):
        findings.append(
            violation(
                rule_id,
                _AUDIT_COMMAND,
                "apm audit help must describe both modes: " + _AUDIT_HELP.removeprefix("help="),
            )
        )
    option: str | None = None
    for number, line in enumerate(facts.lines, start=1):
        match = _AUDIT_OPTION.match(line)
        if match is not None:
            option = match.group(1)
        if "[experimental]" in line and option not in _AUDIT_EXPERIMENTAL_OPTIONS:
            findings.append(
                violation(
                    rule_id,
                    _AUDIT_COMMAND,
                    f"only the external-scanner options are experimental; '{option}' is GA, "
                    "so its help must not say [experimental]",
                    line=number,
                )
            )
    return findings


def _check_experimental_flags(provider: FactsProvider) -> Iterable[Violation]:
    """Experimental flags are read, their notices stay verbose, GA help is not experimental."""
    rule_id = "registry_delegation.experimental_flags"
    return (
        *_unread_flag_findings(provider, rule_id),
        *_inactive_resolver_notice_findings(provider, rule_id),
        *_audit_help_findings(provider, rule_id),
    )


RULES: tuple[Rule, ...] = (
    Rule(
        id="registry_delegation.console_construction",
        group=GROUP,
        guard_ids=("registry-delegation-console-construction",),
        description=(
            "Rich consoles and tables are built only by utils/console.py "
            "(new_console, new_table), which renders ASCII."
        ),
        check=_check_console_construction,
    ),
    Rule(
        id="registry_delegation.experimental_flags",
        group=GROUP,
        guard_ids=("registry-delegation-experimental-flags",),
        description=(
            "Every experimental flag has a reader; the inactive-resolver notice is "
            "verbose-only; only external-scanner audit options say [experimental]."
        ),
        check=_check_experimental_flags,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES", "library_output_findings"]
