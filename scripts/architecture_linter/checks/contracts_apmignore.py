"""Package ship/deploy/compile path membership from ``.apmignore``."""

from __future__ import annotations

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import FileFacts, Violation

_OWNER = "src/apm_cli/utils/apmignore.py"
_CONSTANTS = "src/apm_cli/constants.py"
_CLASS = "ApmIgnoreSpec"
_FILENAME = ".apmignore"
_SRC_PREFIX = "src/apm_cli/"


def _pathspec_import_site(facts: FileFacts) -> tuple[int, int] | None:
    """Return the first pathspec / GitIgnoreSpec import coordinate, if any."""
    for item in facts.imports:
        module = item.module or ""
        if module == "pathspec" or module.startswith("pathspec."):
            return item.line, item.column + 1
        if "GitIgnoreSpec" in item.names:
            return item.line, item.column + 1
    return None


def check_apmignore_membership(
    provider: FactsProvider,
    rule_id: str,
) -> tuple[Violation, ...]:
    """Require ``.apmignore`` parsing to stay in utils/apmignore.py."""
    findings: list[Violation] = []
    owner, failures = checked_facts(provider, _OWNER, rule_id, require_python=True)
    findings.extend(failures)
    if not failures:
        definitions = tuple(
            definition
            for definition in owner.definitions
            if definition.name == _CLASS
            and definition.kind == "class"
            and definition.scope == "<module>"
        )
        if len(definitions) != 1:
            findings.append(
                violation(
                    rule_id,
                    _OWNER,
                    f"{_CLASS} must have exactly one module-level class definition",
                    line=1,
                )
            )
        if _pathspec_import_site(owner) is None:
            findings.append(
                violation(
                    rule_id,
                    _OWNER,
                    "apmignore owner must import pathspec.GitIgnoreSpec",
                    line=1,
                )
            )

    for path in provider.inventory:
        if path == _OWNER or not path.startswith(_SRC_PREFIX) or not path.endswith(".py"):
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        if path != _CONSTANTS:
            for literal in facts.literals:
                if _FILENAME in literal.value_repr:
                    findings.append(
                        violation(
                            rule_id,
                            path,
                            f"{_FILENAME} filename must stay in {_OWNER}",
                            line=literal.line,
                            column=literal.column + 1,
                        )
                    )
        imported = _pathspec_import_site(facts)
        if imported is not None:
            line, column = imported
            findings.append(
                violation(
                    rule_id,
                    path,
                    "pathspec GitIgnoreSpec parsing must stay in utils/apmignore.py",
                    line=line,
                    column=column,
                )
            )
    return tuple(findings)


__all__ = ["check_apmignore_membership"]
