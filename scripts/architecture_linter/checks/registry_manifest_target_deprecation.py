"""Owner guard for the deprecated manifest ``target:`` key (#3762).

``registry-delegation-manifest-target-deprecation`` defends one decision:
the deprecation of the singular ``target:`` key has exactly one warning
emitter (the manifest declaration owner in ``models/apm_package.py``) and one
rewrite path (``models/manifest_target_migration.py``), reachable only from
the explicit ``apm manifest migrate`` command. Install and every other
command must never rewrite the manifest.

Facts-only: every check consumes cached :class:`FileFacts`.
"""

from __future__ import annotations

from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import _SRC, GROUP, _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "registry_delegation.manifest_target_deprecation"
_WARNING_OWNER = "src/apm_cli/models/apm_package.py"
_WARNING_FUNCTION = "_warn_deprecated_manifest_keys"
_WARNING_PHRASE = "deprecated 'target:' key"
_MIGRATION_MODULE = "manifest_target_migration"
_MIGRATION_OWNER = "src/apm_cli/models/manifest_target_migration.py"
_MIGRATION_COMMAND = "src/apm_cli/commands/manifest.py"


def _imports_migration(module: str | None, names: tuple[str, ...]) -> bool:
    if module is not None and module.split(".")[-1] == _MIGRATION_MODULE:
        return True
    return _MIGRATION_MODULE in names


def _check_manifest_target_deprecation(provider: FactsProvider) -> Iterable[Violation]:
    """One warning emitter, one rewrite path, reachable only from the command."""
    findings: list[Violation] = []
    warning_definers: set[str] = set()
    migration_importers: set[str] = set()
    for path in _python_paths(provider, under=_SRC):
        facts, failures = checked_facts(provider, path, _RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if any(
            definition.name == _WARNING_FUNCTION and definition.kind == "function"
            for definition in facts.definitions
        ):
            warning_definers.add(path)
        if path != _WARNING_OWNER:
            for literal in facts.literals:
                if _WARNING_PHRASE in literal.value_repr:
                    findings.append(
                        violation(
                            _RULE_ID,
                            path,
                            "the deprecated 'target:' warning must be emitted only by "
                            f"{_WARNING_OWNER}::{_WARNING_FUNCTION}",
                            line=literal.line,
                            column=literal.column + 1,
                        )
                    )
        for fact in facts.imports:
            if path != _MIGRATION_OWNER and _imports_migration(fact.module, fact.names):
                migration_importers.add(path)
                if path != _MIGRATION_COMMAND:
                    findings.append(
                        violation(
                            _RULE_ID,
                            path,
                            "only 'apm manifest migrate' may rewrite apm.yml's 'target:' key; "
                            "install and other commands must never rewrite the manifest",
                            line=fact.line,
                            column=fact.column + 1,
                        )
                    )
    if warning_definers != {_WARNING_OWNER}:
        findings.append(
            violation(
                _RULE_ID,
                _WARNING_OWNER,
                f"{_WARNING_FUNCTION} must be defined exactly once, in {_WARNING_OWNER}",
            )
        )
    if _MIGRATION_COMMAND not in migration_importers:
        findings.append(
            violation(
                _RULE_ID,
                _MIGRATION_COMMAND,
                f"'apm manifest migrate' must route through {_MIGRATION_OWNER}",
            )
        )
    return findings


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=("registry-delegation-manifest-target-deprecation",),
        description=(
            "Deprecated 'target:' key: one warning emitter in models/apm_package.py and one "
            "rewrite path reachable only from 'apm manifest migrate'."
        ),
        check=_check_manifest_target_deprecation,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
