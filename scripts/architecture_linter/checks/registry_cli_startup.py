"""CLI startup import budget owner (``cli-startup-import-budget``).

``apm --help`` and ``apm --version`` must not import command modules or
GitPython. The canonical owner is ``cli.py``: every verb is registered as a
``_LazyCommand`` stub, and ``deps/__init__.py`` resolves its public names
lazily so light ``apm_cli.deps.*`` submodules do not drag in the downloader
stack. The installed-CLI budget test measures the real imported-module set;
this rule keeps the static shape that budget depends on.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import GROUP, _read_required
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "registry_delegation.cli_startup_imports"
_CLI_ENTRY = "src/apm_cli/cli.py"
_DEPS_PACKAGE = "src/apm_cli/deps/__init__.py"
_BUDGET_TEST = "tests/integration/test_cli_startup_import_budget.py"

# Column-0 imports run at module import time; indented ones are deferred.
_CLI_EAGER_FORBIDDEN = re.compile(
    r"^(?:from|import)\s+"
    r"(?:apm_cli\.(?:commands|deps|install)\b|\.(?:commands|deps|install)\b|git\b|gitdb\b)"
)
_DEPS_EAGER_FORBIDDEN = re.compile(r"^(?:from\s+(?:\.|apm_cli\.deps\b)|(?:from|import)\s+git\b)")
_ADD_COMMAND = re.compile(r"\bcli\.add_command\(")
_LAZY_ADD_COMMAND = re.compile(r"\bcli\.add_command\(\s*_Lazy(?:Alias)?Command\(")


def _eager_import_violations(
    lines: Iterable[str], path: str, pattern: re.Pattern[str], message: str
) -> list[Violation]:
    return [
        violation(_RULE_ID, path, message, line=number)
        for number, line in enumerate(lines, start=1)
        if pattern.search(line) is not None
    ]


def _check_cli_startup_imports(provider: FactsProvider) -> Iterable[Violation]:
    """Keep command modules and GitPython off the CLI startup path."""
    facts_by_path, failures = _read_required(provider, _RULE_ID, (_CLI_ENTRY, _DEPS_PACKAGE))
    if failures:
        return failures
    cli = facts_by_path[_CLI_ENTRY]
    findings = _eager_import_violations(
        cli.lines,
        _CLI_ENTRY,
        _CLI_EAGER_FORBIDDEN,
        "cli.py must not eagerly import command, deps, install, or GitPython modules",
    )
    cli_text = source_text(cli)
    registrations = len(_ADD_COMMAND.findall(cli_text))
    if registrations == 0 or registrations != len(_LAZY_ADD_COMMAND.findall(cli_text)):
        findings.append(
            violation(
                _RULE_ID,
                _CLI_ENTRY,
                "every root verb must be registered as a _LazyCommand stub",
            )
        )
    findings.extend(
        _eager_import_violations(
            facts_by_path[_DEPS_PACKAGE].lines,
            _DEPS_PACKAGE,
            _DEPS_EAGER_FORBIDDEN,
            "deps/__init__.py must resolve submodule exports lazily (PEP 562)",
        )
    )
    budget, budget_failures = checked_facts(provider, _BUDGET_TEST, _RULE_ID, require_python=True)
    findings.extend(budget_failures)
    if not budget_failures and '"git"' not in source_text(budget):
        findings.append(
            violation(
                _RULE_ID,
                _BUDGET_TEST,
                "installed-CLI startup budget must forbid importing GitPython",
            )
        )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=("registry-delegation-cli-startup-import-budget",),
        description=(
            "CLI startup (apm --help / --version) must not import command modules "
            "or GitPython: verbs register as cli.py _LazyCommand stubs."
        ),
        check=_check_cli_startup_imports,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
