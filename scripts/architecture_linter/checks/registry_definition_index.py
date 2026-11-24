"""Which ``src/apm_cli`` modules define a name: read once per linter run (#5196).

The registry owner guards ask, for many names, which source files define them.
``_defining_files`` answers from one index built per :class:`FactsProvider`
instead of rescanning every module per question; ``_scan_defining_files`` is
the direct scan it replaces, kept for equivalence checks.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass

from scripts.architecture_linter.checks.registry_shared import _SRC, _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Violation


@dataclass(frozen=True)
class _DefinitionIndex:
    """Every ``src/apm_cli`` definition, read once per run (#5196).

    ``kinds_by_name`` maps a definition name to the files defining it (in
    inventory order) and the kinds found there; ``unreadable`` lists, in
    inventory order, the files whose read or parse failed.
    """

    kinds_by_name: dict[str, dict[str, frozenset[str]]]
    unreadable: tuple[str, ...]


_DEFINITION_INDEXES: weakref.WeakKeyDictionary[FactsProvider, _DefinitionIndex] = (
    weakref.WeakKeyDictionary()
)


def _definition_index(provider: FactsProvider) -> _DefinitionIndex:
    index = _DEFINITION_INDEXES.get(provider)
    if index is not None:
        return index
    found: dict[str, dict[str, set[str]]] = {}
    unreadable: list[str] = []
    for path in _python_paths(provider, under=_SRC):
        facts = provider.file_facts(path)
        if facts.read_error is not None or facts.parse_error is not None:
            unreadable.append(path)
            continue
        for definition in facts.definitions:
            found.setdefault(definition.name, {}).setdefault(path, set()).add(definition.kind)
    index = _DefinitionIndex(
        {
            name: {path: frozenset(kinds) for path, kinds in paths.items()}
            for name, paths in found.items()
        },
        tuple(unreadable),
    )
    _DEFINITION_INDEXES[provider] = index
    return index


def _defining_files(
    provider: FactsProvider,
    rule_id: str,
    name: str,
    *,
    kinds: tuple[str, ...],
) -> tuple[frozenset[str], tuple[Violation, ...]]:
    """Return the set of source files defining `name`, failing closed on parse.

    Reports which ``src/apm_cli`` Python modules in the deterministic inventory
    declare a top-level-or-nested definition named `name` of one of the
    accepted `kinds`. A read/parse failure on any file is a fail-closed
    violation, mirroring the original bootstrap linter, which parsed every
    module and crashed on a bad tree. The modules are read once per run
    (``_definition_index``); ``_scan_defining_files`` is the direct scan.
    """
    index = _definition_index(provider)
    failures: list[Violation] = []
    for path in index.unreadable:
        failures.extend(checked_facts(provider, path, rule_id, require_python=True)[1])
    accepted = frozenset(kinds)
    definers = {
        path for path, found in index.kinds_by_name.get(name, {}).items() if found & accepted
    }
    return frozenset(definers), tuple(failures)


def _scan_defining_files(
    provider: FactsProvider,
    rule_id: str,
    name: str,
    *,
    kinds: tuple[str, ...],
) -> tuple[frozenset[str], tuple[Violation, ...]]:
    """The direct per-call scan ``_defining_files`` replaces (equivalence checks)."""
    definers: set[str] = set()
    failures: list[Violation] = []
    for path in _python_paths(provider, under=_SRC):
        facts, read_failures = checked_facts(provider, path, rule_id, require_python=True)
        if read_failures:
            failures.extend(read_failures)
            continue
        if any(
            definition.name == name and definition.kind in kinds for definition in facts.definitions
        ):
            definers.add(path)
    return frozenset(definers), tuple(failures)
