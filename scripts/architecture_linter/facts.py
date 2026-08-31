"""Single-pass fact extraction: one read, one parse, one AST walk per file.

Three collaborators do the work:

* :class:`SourceCache` reads each file's bytes at most once, memoizing both
  successful reads and read errors.
* :class:`ParseCache` parses each Python source at most once, memoizing both
  the resulting module and parse errors.
* :class:`FactsProvider` is what rules actually receive. It lazily builds a
  frozen :class:`~scripts.architecture_linter.models.FileFacts` per file on
  first request (via the two caches above and one shared composite
  ``ast.NodeVisitor``), then serves every subsequent request for the same
  path from memory. No file is ever read twice, parsed twice, or walked with
  a second ``ast.walk``, no matter how many rules ask for it.

The walk also records the file's raw AST *shape* -- one immutable
``(node, parent)`` record per visited node -- as an intrinsic part of
:class:`~scripts.architecture_linter.models.FileFacts`, alongside the parent
and scope relationships it already tracked. Shape is needed by most analyzers,
so making it intrinsic replaced the seven separately registered collectors that
each re-recorded the same pairs for overlapping file sets; every one of those
duplicates is now a single shared tuple.
:func:`~scripts.architecture_linter.checks.tree_index.build_tree_index` is the
only interpreter of those records.

:class:`Collector` remains the registration hook for *specialized* facts a
group needs computed during the same single pass: the shared visitor calls
every registered collector once per AST node, and their output lands in
``FileFacts.extra``. Re-recording ``(node, parent)`` is not a specialized fact
-- that data is already intrinsic.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from scripts.architecture_linter.inventory import is_safe_repository_relative_path
from scripts.architecture_linter.models import (
    AssignmentFact,
    CallFact,
    DefinitionFact,
    FileFacts,
    ImportFact,
    LiteralFact,
    NodeRecord,
)
from scripts.architecture_linter.registry import OwnerRegistry

if TYPE_CHECKING:
    from scripts.architecture_linter.checks.tree_index import TreeIndex

# Attribute-call names treated as in-place mutation of their receiver, e.g.
# `items.append(x)`. Best-effort recognition, not an exhaustive type-aware
# analysis -- this is a lightweight AST fact, not a data-flow proof.
_MUTATING_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "update",
        "add",
        "discard",
        "setdefault",
        "popitem",
    }
)

_MODULE_SCOPE = "<module>"


def _safe_unparse(node: ast.AST) -> str:
    """Best-effort ``ast.unparse``; never raises out of the shared traversal."""
    try:
        return ast.unparse(node)
    except Exception:  # a single unparsable node must not abort the walk.
        return f"<unparse-error:{type(node).__name__}>"


class SourceCache:
    """Reads each file's text at most once; caches both text and errors."""

    def __init__(
        self,
        root: Path,
        inventory: Collection[str],
        source_overrides: Mapping[str, str | bytes] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._inventory = frozenset(inventory)
        self._source_overrides = MappingProxyType(dict(source_overrides or {}))
        self._cache: dict[str, tuple[str | None, str | None]] = {}
        self._reads_per_file: dict[str, int] = {}
        self.read_attempts = 0
        self.read_successes = 0
        self.read_errors = 0

    def read(self, relative_path: str) -> tuple[str | None, str | None]:
        """Return ``(text, error)`` for `relative_path`, reading at most once."""
        cached = self._cache.get(relative_path)
        if cached is not None:
            return cached
        self.read_attempts += 1
        self._reads_per_file[relative_path] = self._reads_per_file.get(relative_path, 0) + 1
        if not is_safe_repository_relative_path(relative_path):
            result: tuple[str | None, str | None] = (
                None,
                f"unsafe repository-relative path: {relative_path!r}",
            )
            self.read_errors += 1
            self._cache[relative_path] = result
            return result
        override = self._source_overrides.get(relative_path)
        if override is None:
            if relative_path not in self._inventory:
                result = (None, "path is outside repository inventory")
                self.read_errors += 1
                self._cache[relative_path] = result
                return result
            candidate = (self._root / relative_path).resolve(strict=False)
            if not candidate.is_relative_to(self._root):
                result = (None, "path resolves outside repository root")
                self.read_errors += 1
                self._cache[relative_path] = result
                return result
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                result = (None, f"cannot read: {exc}")
                self.read_errors += 1
                self._cache[relative_path] = result
                return result
        else:
            data = override.encode("utf-8") if isinstance(override, str) else override
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            result = (None, f"cannot decode as utf-8: {exc}")
            self.read_errors += 1
            self._cache[relative_path] = result
            return result
        result = (text, None)
        self.read_successes += 1
        self._cache[relative_path] = result
        return result

    @property
    def max_reads_per_file(self) -> int:
        return max(self._reads_per_file.values(), default=0)

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        """Return cached read/decode failures in deterministic path order."""
        return tuple(
            (path, error) for path, (_, error) in sorted(self._cache.items()) if error is not None
        )


class ParseCache:
    """Parses each Python source at most once; caches both tree and errors."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[ast.Module | None, str | None]] = {}
        self._parses_per_file: dict[str, int] = {}
        self.parse_attempts = 0
        self.parse_successes = 0
        self.parse_errors = 0

    def parse(self, relative_path: str, source: str) -> tuple[ast.Module | None, str | None]:
        """Return ``(tree, error)`` for `relative_path`, parsing at most once."""
        cached = self._cache.get(relative_path)
        if cached is not None:
            return cached
        self.parse_attempts += 1
        self._parses_per_file[relative_path] = self._parses_per_file.get(relative_path, 0) + 1
        try:
            tree = ast.parse(source, filename=relative_path)
        except (SyntaxError, ValueError) as exc:
            result: tuple[ast.Module | None, str | None] = (None, str(exc))
            self.parse_errors += 1
            self._cache[relative_path] = result
            return result
        result = (tree, None)
        self.parse_successes += 1
        self._cache[relative_path] = result
        return result

    @property
    def max_parses_per_file(self) -> int:
        return max(self._parses_per_file.values(), default=0)

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        """Return cached syntax failures in deterministic path order."""
        return tuple(
            (path, error) for path, (_, error) in sorted(self._cache.items()) if error is not None
        )


@dataclass(frozen=True)
class VisitContext:
    """Per-node context handed to registered collectors during the one walk."""

    path: str
    node: ast.AST
    parent: ast.AST | None
    scope: str


class Collector(Protocol):
    """A hook that rides the shared traversal instead of re-walking the tree."""

    name: str

    def on_node(self, context: VisitContext) -> Sequence[object]:
        """Return zero or more facts to accumulate for this node, if any."""
        ...


class _CompositeVisitor:
    """The one AST walk every file gets, no matter how many rules ask for it.

    Deliberately does not subclass ``ast.NodeVisitor`` and does not define
    per-type ``visit_*`` methods: a single ``_visit`` method drives the whole
    traversal so every node passes through exactly one counted, collector-
    dispatching choke point, with an explicit parent/scope stack instead of
    relying on ``generic_visit`` dispatch order.

    That same choke point appends the node's intrinsic ``(node, parent)``
    record, so the file's full pre-order shape falls out of the walk every
    rule already pays for -- exactly once, whoever asks.
    """

    def __init__(self, path: str, collectors: Sequence[Collector]) -> None:
        self._path = path
        self._collectors = collectors
        self._scope_stack: list[str] = [_MODULE_SCOPE]
        self.visits = 0
        self.definitions: list[DefinitionFact] = []
        self.imports: list[ImportFact] = []
        self.calls: list[CallFact] = []
        self.assignments: list[AssignmentFact] = []
        self.literals: list[LiteralFact] = []
        self.node_records: list[NodeRecord] = []
        self.extra: dict[str, list[object]] = {collector.name: [] for collector in collectors}

    def walk(self, tree: ast.Module) -> None:
        self._visit(tree, parent=None)

    def _scope(self) -> str:
        return self._scope_stack[-1]

    def _dispatch_collectors(self, node: ast.AST, parent: ast.AST | None) -> None:
        if not self._collectors:
            return
        context = VisitContext(path=self._path, node=node, parent=parent, scope=self._scope())
        for collector in self._collectors:
            produced = collector.on_node(context)
            if produced:
                self.extra[collector.name].extend(produced)

    def _visit(self, node: ast.AST, *, parent: ast.AST | None) -> None:
        self.visits += 1
        self.node_records.append((node, parent))
        self._record(node)
        self._dispatch_collectors(node, parent)

        is_scope_node = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_scope_node:
            self._scope_stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            self._visit(child, parent=node)
        if is_scope_node:
            self._scope_stack.pop()

    def _record(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._record_definition(node)
        elif isinstance(node, ast.Import):
            self._record_import(node)
        elif isinstance(node, ast.ImportFrom):
            self._record_import_from(node)
        elif isinstance(node, ast.Call):
            self._record_call(node)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            self._record_assignment(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._record_literal(node)

    def _record_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async_function"
        else:
            kind = "function"
        self.definitions.append(
            DefinitionFact(
                name=node.name,
                kind=kind,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                scope=self._scope(),
                decorators=tuple(_safe_unparse(d) for d in node.decorator_list),
            )
        )

    def _record_import(self, node: ast.Import) -> None:
        self.imports.append(
            ImportFact(
                module=None,
                names=tuple(alias.name for alias in node.names),
                level=0,
                line=node.lineno,
                column=node.col_offset,
                scope=self._scope(),
            )
        )

    def _record_import_from(self, node: ast.ImportFrom) -> None:
        self.imports.append(
            ImportFact(
                module=node.module,
                names=tuple(alias.name for alias in node.names),
                level=node.level,
                line=node.lineno,
                column=node.col_offset,
                scope=self._scope(),
            )
        )

    def _record_call(self, node: ast.Call) -> None:
        self.calls.append(
            CallFact(
                qualname=_safe_unparse(node.func),
                line=node.lineno,
                column=node.col_offset,
                scope=self._scope(),
            )
        )
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _MUTATING_METHOD_NAMES:
            self.assignments.append(
                AssignmentFact(
                    target=_safe_unparse(func.value),
                    kind="call_mutation",
                    line=node.lineno,
                    column=node.col_offset,
                    scope=self._scope(),
                )
            )

    def _record_assignment(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> None:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            kind = "assign"
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            kind = "aug_assign"
        else:
            targets = [node.target]
            kind = "ann_assign"
        for target in targets:
            if isinstance(target, ast.Attribute):
                kind = f"attribute_{kind}"
            elif isinstance(target, ast.Subscript):
                kind = f"subscript_{kind}"
            self.assignments.append(
                AssignmentFact(
                    target=_safe_unparse(target),
                    kind=kind,
                    line=node.lineno,
                    column=node.col_offset,
                    scope=self._scope(),
                )
            )

    def _record_literal(self, node: ast.Constant) -> None:
        self.literals.append(
            LiteralFact(
                value_repr=repr(node.value),
                kind="str",
                line=node.lineno,
                column=node.col_offset,
                scope=self._scope(),
            )
        )


def _build_file_facts(
    relative_path: str,
    source_cache: SourceCache,
    parse_cache: ParseCache,
    collectors: Sequence[Collector],
) -> FileFacts:
    text, read_error = source_cache.read(relative_path)
    is_python = relative_path.endswith(".py")
    lines = tuple(text.splitlines()) if text is not None else ()

    parse_error: str | None = None
    definitions: tuple[DefinitionFact, ...] = ()
    imports: tuple[ImportFact, ...] = ()
    calls: tuple[CallFact, ...] = ()
    assignments: tuple[AssignmentFact, ...] = ()
    literals: tuple[LiteralFact, ...] = ()
    node_records: tuple[NodeRecord, ...] = ()
    extra: MappingProxyType[str, tuple[object, ...]] = MappingProxyType({})
    visits = 0

    if is_python and text is not None:
        tree, parse_error = parse_cache.parse(relative_path, text)
        if tree is not None:
            visitor = _CompositeVisitor(relative_path, collectors)
            visitor.walk(tree)
            definitions = tuple(visitor.definitions)
            imports = tuple(visitor.imports)
            calls = tuple(visitor.calls)
            assignments = tuple(visitor.assignments)
            literals = tuple(visitor.literals)
            node_records = tuple(visitor.node_records)
            extra = MappingProxyType({name: tuple(items) for name, items in visitor.extra.items()})
            visits = visitor.visits

    return FileFacts(
        path=relative_path,
        exists=read_error is None,
        is_python=is_python,
        read_error=read_error,
        parse_error=parse_error,
        lines=lines,
        definitions=definitions,
        imports=imports,
        calls=calls,
        assignments=assignments,
        literals=literals,
        node_records=node_records,
        extra=extra,
        visits=visits,
    )


class FactsProvider:
    """What rules receive: the inventory, the registry, and lazy file facts.

    Facts are built on first request and memoized, so ten rules asking about
    the same file trigger exactly one read, one parse, and one AST walk in
    total for that file -- not per rule.
    """

    def __init__(
        self,
        root: Path,
        inventory: tuple[str, ...],
        registry: OwnerRegistry | None,
        collectors: Sequence[Collector] = (),
        source_overrides: Mapping[str, str | bytes] | None = None,
    ) -> None:
        self.root = root
        self.inventory = inventory
        self.registry = registry
        self.source_cache = SourceCache(root, inventory, source_overrides)
        self.parse_cache = ParseCache()
        self._collectors = tuple(collectors)
        self._file_facts: dict[str, FileFacts] = {}
        self._tree_indexes: dict[str, TreeIndex | None] = {}
        self._tree_index_builds_per_file: dict[str, int] = {}
        self._tree_index_node_count = 0
        self.tree_index_builds = 0
        self.tree_index_cache_hits = 0
        self.peak_tree_index_nodes = 0

    def file_facts(self, relative_path: str) -> FileFacts:
        """Return the (possibly cached) facts for `relative_path`."""
        cached = self._file_facts.get(relative_path)
        if cached is not None:
            return cached
        facts = _build_file_facts(
            relative_path, self.source_cache, self.parse_cache, self._collectors
        )
        self._file_facts[relative_path] = facts
        return facts

    def tree_index(self, relative_path: str) -> TreeIndex | None:
        """Return the one cached compact tree index for `relative_path`."""
        if relative_path in self._tree_indexes:
            self.tree_index_cache_hits += 1
            return self._tree_indexes[relative_path]

        # Lazy to keep the fact layer import-safe and avoid a models/facts cycle.
        from scripts.architecture_linter.checks.tree_index import build_tree_index

        index = build_tree_index(self.file_facts(relative_path))
        self._tree_indexes[relative_path] = index
        self.tree_index_builds += 1
        self._tree_index_builds_per_file[relative_path] = (
            self._tree_index_builds_per_file.get(relative_path, 0) + 1
        )
        if index is not None:
            self._tree_index_node_count += len(index.nodes)
            self.peak_tree_index_nodes = max(
                self.peak_tree_index_nodes,
                self._tree_index_node_count,
            )
        return index

    @property
    def max_tree_index_builds_per_file(self) -> int:
        """Highest number of compact-index builds observed for one path."""
        return max(self._tree_index_builds_per_file.values(), default=0)

    @property
    def ast_visits(self) -> int:
        """Total AST nodes visited across every file built so far."""
        return sum(facts.visits for facts in self._file_facts.values())
