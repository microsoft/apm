"""Frozen value objects shared across the architecture linter engine.

Every dataclass in this module is immutable (``frozen=True``). Rule authors
receive these objects from :class:`~scripts.architecture_linter.facts.FactsProvider`
and hand them back via :class:`RuleResult`; nothing here is ever mutated after
construction, so the same facts can be safely reused across every rule that
asks for them without defensive copying.

``Rule.check`` is annotated against ``FactsProvider`` only for static analysis
(guarded by ``TYPE_CHECKING``) to avoid a runtime import cycle with
``facts.py``, which itself imports the frozen fact records defined here.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only.
    from scripts.architecture_linter.checks.tree_index import TreeIndex
    from scripts.architecture_linter.facts import FactsProvider


# --------------------------------------------------------------------------
# Per-node facts captured by the single shared AST traversal.
# --------------------------------------------------------------------------


# One ``(node, parent)`` pair, recorded once per visited node by the shared
# traversal. An immutable two-tuple rather than a dataclass on purpose: this is
# the highest-cardinality record the engine keeps (one per AST node across the
# whole repository), so it stays the cheapest immutable container Python has.
# :mod:`scripts.architecture_linter.checks.tree_index` re-exports this alias and
# owns every structural question asked of the records.
NodeRecord = tuple[ast.AST, "ast.AST | None"]


@dataclass(frozen=True)
class ImportFact:
    """One ``import`` or ``from ... import`` statement."""

    module: str | None
    names: tuple[str, ...]
    level: int
    line: int
    column: int
    scope: str


@dataclass(frozen=True)
class CallFact:
    """One call expression, keyed by its best-effort unparsed callee."""

    qualname: str
    line: int
    column: int
    scope: str


@dataclass(frozen=True)
class AssignmentFact:
    """One assignment, augmented assignment, or recognized mutation call."""

    target: str
    kind: str
    line: int
    column: int
    scope: str


@dataclass(frozen=True)
class LiteralFact:
    """One string literal captured by the shared traversal."""

    value_repr: str
    kind: str
    line: int
    column: int
    scope: str


@dataclass(frozen=True)
class DefinitionFact:
    """One function, async function, or class definition."""

    name: str
    kind: str
    line: int
    end_line: int
    scope: str
    decorators: tuple[str, ...]


@dataclass(frozen=True)
class FileFacts:
    """Everything the shared traversal captured for a single file, once.

    ``tree_index`` is the compact intrinsic AST shape built by the same shared
    traversal. Raw ``(node, parent)`` records exist only while this object is
    being constructed; retaining both those records and the query index doubled
    the high-cardinality shape state for the full run.

    ``extra`` remains the escape hatch for *specialized* facts a
    :class:`~scripts.architecture_linter.facts.Collector` computes during the
    same traversal, keyed by collector name. Shape is no longer one of them:
    a collector that only re-records ``(node, parent)`` duplicates the compact
    tree index and must not exist.
    """

    path: str
    exists: bool
    is_python: bool
    read_error: str | None
    parse_error: str | None
    lines: tuple[str, ...]
    definitions: tuple[DefinitionFact, ...]
    imports: tuple[ImportFact, ...]
    calls: tuple[CallFact, ...]
    assignments: tuple[AssignmentFact, ...]
    literals: tuple[LiteralFact, ...]
    tree_index: TreeIndex | None
    extra: Mapping[str, tuple[object, ...]]
    visits: int


# --------------------------------------------------------------------------
# Rule contract.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One reported architecture-boundary violation, Ruff-diagnostic shaped."""

    rule_id: str
    path: str
    line: int
    column: int
    message: str


@dataclass(frozen=True)
class Rule:
    """One executable rule contributed by a rule-group module.

    ``guard_ids`` names the canonical-owner-registry guard IDs (see
    ``.apm/architecture/owners/*.json``) this rule enforces. The runner
    validates those IDs bidirectionally against the registry and confirms
    every registered guard executes exactly once per run.
    """

    id: str
    group: str
    guard_ids: tuple[str, ...]
    description: str
    check: Callable[[FactsProvider], Iterable[Violation]]


@dataclass(frozen=True)
class RuleResult:
    """Outcome of executing exactly one rule, success or failure."""

    rule_id: str
    group: str
    guard_ids: tuple[str, ...]
    violations: tuple[Violation, ...]
    error: str | None


@dataclass(frozen=True)
class GroupResult:
    """Outcome of loading and running exactly one rule-group module."""

    group: str
    module_name: str
    import_error: str | None
    rules: tuple[RuleResult, ...]
    duration_seconds: float


# --------------------------------------------------------------------------
# Aggregated startup/read/parse/registry/rule failures.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """One non-fatal-to-the-process failure surfaced instead of a crash."""

    stage: str
    message: str


# --------------------------------------------------------------------------
# Metrics and the final run report.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetrics:
    """Deterministic counters describing exactly one linter run."""

    inventory_file_count: int
    excluded_root_count: int
    read_attempts: int
    read_successes: int
    read_errors: int
    max_reads_per_file: int
    parse_attempts: int
    parse_successes: int
    parse_errors: int
    max_parses_per_file: int
    ast_visits: int
    tree_index_builds: int
    tree_index_cache_hits: int
    max_tree_index_builds_per_file: int
    peak_tree_index_nodes: int
    per_group_seconds: tuple[tuple[str, float], ...]
    total_seconds: float
    child_process_count: int


@dataclass(frozen=True)
class RunReport:
    """The complete, immutable outcome of one :func:`runner.run` call."""

    violations: tuple[Violation, ...]
    failures: tuple[Failure, ...]
    group_results: tuple[GroupResult, ...]
    metrics: RunMetrics
    exit_code: int
