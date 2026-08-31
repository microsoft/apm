"""The one canonical tree index over the engine's intrinsic node records.

Every analyzer that needs AST *shape* -- children, descendants, ancestry,
own-scope membership, class-qualified function lookup -- goes through this
module. Nothing else in the linter is allowed to reconstruct that shape.

One authority, no registration:

* The shared composite traversal in :mod:`scripts.architecture_linter.facts`
  records one immutable :data:`NodeRecord` per visited node as an intrinsic
  part of :class:`~scripts.architecture_linter.models.FileFacts`. Shape is not
  an opt-in fact a group registers a collector for; it is simply what the one
  walk produces. Seven path-scoped collectors, registered across the six rule
  groups, used to re-record these same pairs for overlapping file sets, which
  meant every node of a ``src/apm_cli`` file was retained three or more times
  over.
* :class:`TreeIndex` is the *only* query surface built from those records.
  :func:`build_tree_index` folds one file's record stream into precomputed
  children, descendants, parents, own-scope anchors, definition anchors, and a
  class-qualified function table in a single linear pass, so every subtree
  question an analyzer asks is a dict lookup or a tuple slice rather than a
  second traversal.

Because the composite traversal is depth-first pre-order, a file's records
arrive parent-before-child and every subtree occupies one contiguous run.
:func:`build_tree_index` exploits exactly that: descendant sets are slices of
the pre-order node tuple, which is both the cheapest way to materialize them
and the reason :meth:`TreeIndex.walk` can return precomputed tuples instead of
walking anything. No function in this module reads a file, parses source,
calls ``ast.parse``/``ast.walk``/``ast.iter_child_nodes``, uses an
``ast.NodeVisitor``, or starts a subprocess.
"""

from __future__ import annotations

import ast
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.models import NodeRecord

# Function-shaped definitions (what a "function" means to every analyzer).
FUNCTION_NODES: tuple[type[ast.AST], ...] = (ast.FunctionDef, ast.AsyncFunctionDef)

# Own-scope barriers: a nested function or lambda ends the enclosing scope.
# Reproduces the legacy helpers' ``_walk_own_scope`` boundary exactly.
OWN_SCOPE_BARRIERS: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
)

# Definition barriers: a nested def/class ends the enclosing *definition*
# scope. Reproduces the legacy test-contract helper's scope-node boundary.
DEFINITION_NODES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


@dataclass(frozen=True)
class TreeIndex:
    """Precomputed AST shape for one file: the only subtree query surface.

    Built once per file by :func:`build_tree_index`. Every accessor is a dict
    lookup, an array read, or a slice of an already-materialized tuple; none of
    them descends an AST.

    Adjacency is addressed by ``id`` where that is exact (parents and children
    of real nodes) and by *pre-order position* everywhere a shared node could
    lie: CPython reuses one ``ast.Load``/``ast.Store``/``ast.Del`` and one
    operator instance across an entire module, so an id-keyed scope lookup
    would answer for whichever occurrence was recorded last. Those shared
    instances are always childless, which is why positional scope arrays are
    both necessary and sufficient.
    """

    root: ast.AST | None
    nodes: tuple[ast.AST, ...]
    _parent_positions: array[int]
    _ends: array[int]
    _own_scope_anchors: array[int]
    _definition_anchors: array[int]
    _functions: tuple[ast.AST, ...]
    _functions_by_qualname: dict[str, ast.AST]
    _qualnames_by_position: dict[int, str]

    # -- adjacency -------------------------------------------------------
    def children(self, node: ast.AST) -> tuple[ast.AST, ...]:
        """Return the direct children of `node` in source order."""
        return tuple(ast.iter_child_nodes(node))

    def module_children(self) -> tuple[ast.AST, ...]:
        """Return the module body (what a helper reads as ``tree.body``)."""
        return () if self.root is None else self.children(self.root)

    def parent(self, node: ast.AST) -> ast.AST | None:
        """Return the recorded parent of `node`, or ``None`` at the root."""
        position = self._position(node)
        if position is None:
            return None
        parent_position = self._parent_positions[position]
        return None if parent_position < 0 else self.nodes[parent_position]

    def walk(self, node: ast.AST) -> Sequence[ast.AST]:
        """Return a compact pre-order view of `node` and its descendants.

        The returned tuple is the same node set ``ast.walk(node)`` would
        yield. Known subtrees are represented by an O(1) range view over the
        file's one pre-order node tuple; unknown nodes degrade to ``(node,)``.
        """
        position = self._position(node)
        if position is None:
            return (node,)
        return _NodeRange(self.nodes, position, self._ends[position])

    # -- scopes ----------------------------------------------------------
    def own_scope(self, function: ast.AST) -> tuple[ast.AST, ...]:
        """Return the ``_walk_own_scope`` set for one function or lambda.

        A node belongs to `function`'s own scope when `function` is its
        nearest enclosing function-or-lambda ancestor: a nested definition is
        itself included (it is the boundary), but nothing inside it is.
        """
        return self._scoped(function, self._own_scope_anchors)

    def definition_anchor(self, node: ast.AST) -> ast.AST | None:
        """Return the nearest enclosing ``def``/``async def``/``class``."""
        position = self._position(node)
        if position is None:
            return None
        anchor = self._definition_anchors[position]
        return None if anchor < 0 else self.nodes[anchor]

    def definition_scope(self, node: ast.AST) -> tuple[ast.AST, ...]:
        """Return `node`'s statement subtree, excluding nested definitions.

        Pre-order, `node` first, stopping at (and omitting) every nested
        ``def``/``async def``/``class``. This is the legacy test-contract
        helper's scope-node set for one statement; a `node` that is itself a
        definition contributes nothing.
        """
        position = self._position(node)
        if position is None:
            return ()
        anchor = self._definition_anchors[position]
        return tuple(
            self.nodes[offset]
            for offset in range(position, self._ends[position])
            if self._definition_anchors[offset] == anchor
            and not isinstance(self.nodes[offset], DEFINITION_NODES)
        )

    def _scoped(self, node: ast.AST, anchors: array[int]) -> tuple[ast.AST, ...]:
        """Return the descendants of `node` anchored directly to `node`."""
        position = self._position(node)
        if position is None:
            return ()
        return tuple(
            self.nodes[offset]
            for offset in range(position + 1, self._ends[position])
            if anchors[offset] == position
        )

    def _position(self, node: ast.AST) -> int | None:
        """Return the traversal position attached during the one shared walk."""
        position = getattr(node, "_apm_arch_position", None)
        if not isinstance(position, int) or position < 0 or position >= len(self.nodes):
            return None
        return position if self.nodes[position] is node else None

    # -- definitions -----------------------------------------------------
    def functions(self) -> tuple[ast.AST, ...]:
        """Return every function or async-function definition, pre-order."""
        return self._functions

    def function(self, qualname: str) -> ast.AST | None:
        """Return the function registered under one class-qualified name."""
        return self._functions_by_qualname.get(qualname)

    def function_qualname(self, function: ast.AST) -> str:
        """Return the class-qualified name recorded for `function`."""
        position = self._position(function)
        if position is None:
            raise RuntimeError("function is not part of this tree index")
        try:
            return self._qualnames_by_position[position]
        except KeyError:
            raise RuntimeError(
                f"tree index has no qualified name at function position {position}"
            ) from None


def _subtree_ends(parent_positions: Sequence[int]) -> array[int]:
    """Return each pre-order position's exclusive subtree end.

    Depth-first pre-order makes every subtree a contiguous run, so a node's
    descendant set is ``nodes[position:end]``. Walking positions downward
    finalizes each node before it propagates its end up to its parent.
    """
    ends = array("I", (position + 1 for position in range(len(parent_positions))))
    for position in range(len(parent_positions) - 1, 0, -1):
        parent_position = parent_positions[position]
        if parent_position < 0:
            continue
        ends[parent_position] = max(ends[parent_position], ends[position])
    return ends


@dataclass(frozen=True)
class _NodeRange(Sequence[ast.AST]):
    """Immutable, non-copying view over one contiguous pre-order subtree."""

    nodes: tuple[ast.AST, ...]
    start: int
    stop: int

    def __len__(self) -> int:
        return self.stop - self.start

    def __iter__(self) -> Iterator[ast.AST]:
        for position in range(self.start, self.stop):
            yield self.nodes[position]

    def __getitem__(self, index: int | slice) -> ast.AST | tuple[ast.AST, ...]:
        if isinstance(index, slice):
            positions = range(self.start, self.stop)[index]
            return tuple(self.nodes[position] for position in positions)
        position = index
        if position < 0:
            position += len(self)
        if position < 0 or position >= len(self):
            raise IndexError(index)
        return self.nodes[self.start + position]


def build_tree_index(records: Sequence[NodeRecord]) -> TreeIndex | None:
    """Fold one file's intrinsic node records into a :class:`TreeIndex`.

    The raw records are a construction-only stream from the shared traversal.
    The returned index keeps one node tuple plus compact integer arrays; it does
    not retain record tuples or per-node dictionaries.

    Returns ``None`` when that traversal recorded nothing for the file (not
    Python, unreadable, or unparseable), which every caller treats as "no
    structural claim can be made about this file".
    """
    if not records:
        return None

    nodes: list[ast.AST] = []
    parent_positions = array("i")
    own_anchors = array("i")
    definition_anchors = array("i")
    qualnames: dict[int, str] = {}
    functions: list[ast.AST] = []
    by_qualname: dict[str, ast.AST] = {}
    root: ast.AST | None = None

    # The shared traversal is pre-order, so a parent is always recorded before
    # its children and every lookup below is already populated. A definition's
    # class-qualified name is composed from its *definition anchor* rather than
    # from a per-node prefix chain: the anchor already carries the full chain,
    # so intermediate expression nodes need to record nothing.
    for node, parent in records:
        position = len(nodes)
        node._apm_arch_position = position
        nodes.append(node)
        if parent is None:
            root = node
            parent_positions.append(-1)
            own_anchors.append(-1)
            definition_anchors.append(-1)
            anchor = -1
        else:
            parent_position = parent._apm_arch_position
            parent_positions.append(parent_position)
            own_anchors.append(
                parent_position
                if isinstance(parent, OWN_SCOPE_BARRIERS)
                else own_anchors[parent_position]
            )
            anchor = (
                parent_position
                if isinstance(parent, DEFINITION_NODES)
                else definition_anchors[parent_position]
            )
            definition_anchors.append(anchor)
        if isinstance(node, DEFINITION_NODES):
            qualname = node.name if anchor < 0 else f"{qualnames[anchor]}.{node.name}"
            qualnames[position] = qualname
            if isinstance(node, FUNCTION_NODES):
                functions.append(node)
                by_qualname[qualname] = node

    ordered = tuple(nodes)
    ends = _subtree_ends(parent_positions)
    return TreeIndex(
        root=root,
        nodes=ordered,
        _parent_positions=parent_positions,
        _ends=ends,
        _own_scope_anchors=own_anchors,
        _definition_anchors=definition_anchors,
        _functions=tuple(functions),
        _functions_by_qualname=by_qualname,
        _qualnames_by_position=qualnames,
    )


__all__ = [
    "DEFINITION_NODES",
    "FUNCTION_NODES",
    "OWN_SCOPE_BARRIERS",
    "NodeRecord",
    "TreeIndex",
    "build_tree_index",
]
