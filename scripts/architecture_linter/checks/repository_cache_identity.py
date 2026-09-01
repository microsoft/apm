"""Structural repository-cache-identity analysis over shared traversal facts.

``scripts/check_repository_cache_identity_owner.py`` is the standalone helper
that first encoded these semantics.  Its own docstring records why they exist:
the shell gate's string greps caught the exact retired variable names but not
"equivalent truncation hidden behind a renamed helper or applied after
canonical normalization".  When the shell gate was replaced by the in-process
linter, the ``transport-platform-git-cache-identity`` rule kept only the
lexical half, so a renamed indirection or a post-normalization ``rsplit`` again
sailed through the rule that is supposed to own this decision.

This module is the single in-process owner of the *structural* half.  It ports
every load-bearing shape the helper asserts:

* ``SharedCloneCache.get_or_clone`` assigns ``repository`` exactly once from
  ``normalize_repo_url(repository_url)``, keys entries by the direct
  ``(repository, ref)`` tuple, and performs exactly one Tier-0 bare lookup on
  that same undecorated identity.
* ``_repository_cache_identity`` *directly returns* the composition
  ``normalize_repo_url(dep_ref.to_github_url())`` -- one return, no keywords,
  and no intermediate binding that could truncate the value first.
* ``L0PerRunCache.try_resolve``, ``TieredRefResolver.resolve``, and
  ``TieredRefResolver.seed`` consume that helper directly, with the exact
  call/assignment counts and argument positions the helper requires.
* Every fail-closed case: a missing owner path, an unparseable owner source,
  and each individually missing method or function.

Nothing here reads a file, lists a directory, starts a subprocess, calls
``ast.parse`` / ``ast.walk`` / ``ast.iter_child_nodes``, or subclasses
``ast.NodeVisitor``.  This module registers no collector either: the engine's
one composite traversal already retains every file's ``(node, parent)`` pairs
intrinsically, the canonical
:func:`~scripts.architecture_linter.checks.tree_index.build_tree_index` folds
them, and every question below is a precomputed tuple slice or dict lookup on
the resulting :class:`~scripts.architecture_linter.checks.tree_index.TreeIndex`.

The helper walks with ``ast.walk`` (breadth-first) while :meth:`TreeIndex.walk`
returns the same node *set* in pre-order.  That is immaterial here: every
ported predicate either counts the whole collection, or indexes ``[0]`` only
after asserting the count is exactly one, or is an order-independent ``any``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.checks.python_semantics import (
    assignment_nodes,
    assignments_to,
    binding_nodes,
    direct_definitions,
    effective_definition,
    effective_function,
    has_exclusive_import,
)
from scripts.architecture_linter.checks.tree_index import (
    FUNCTION_NODES,
    TreeIndex,
)

# --------------------------------------------------------------------------
# The exact owner modules this analyzer may know about.
# --------------------------------------------------------------------------

SHARED_CACHE_PATH = "src/apm_cli/deps/shared_clone_cache.py"
TIERED_RESOLVER_PATH = "src/apm_cli/deps/tiered_ref_resolver.py"
OWNER_PATHS: tuple[str, ...] = (SHARED_CACHE_PATH, TIERED_RESOLVER_PATH)

# --------------------------------------------------------------------------
# check_repository_cache_identity_owner.py vocabulary (verbatim).
# --------------------------------------------------------------------------

SHARED_CLASS = "SharedCloneCache"
SHARED_METHOD = "get_or_clone"
IDENTITY_FUNCTION = "_repository_cache_identity"
L0_CLASS = "L0PerRunCache"
L0_METHOD = "try_resolve"
RESOLVER_CLASS = "TieredRefResolver"
RESOLVE_METHOD = "resolve"
SEED_METHOD = "seed"

_NORMALIZER = "normalize_repo_url"
_REPOSITORY_URL = "repository_url"
_REPOSITORY = "repository"
_REF = "ref"
_KEY = "key"
_DEP_REF = "dep_ref"
_GITHUB_URL = "dep_ref.to_github_url"
_BARE_LOOKUP = "self._find_repo_bare"
_L0_GET = "self.cache.get"
_SEED_PUT = "self._cache.put"

# --------------------------------------------------------------------------
# Diagnostic messages (verbatim from the helper).
# --------------------------------------------------------------------------

MISSING_SOURCE_MESSAGE = "configured cache-identity owner path is missing or not a regular file"
UNPARSEABLE_SOURCE_MESSAGE = "cannot parse configured cache-identity owner source"

SHARED_METHOD_MISSING_MESSAGE = f"{SHARED_CLASS}.{SHARED_METHOD} is missing"
SHARED_CLASS_DUPLICATE_MESSAGE = f"{SHARED_CLASS} has duplicate definitions"
SHARED_METHOD_DUPLICATE_MESSAGE = f"{SHARED_CLASS}.{SHARED_METHOD} has duplicate definitions"
SHARED_ASSIGN_MESSAGE = (
    f"{SHARED_METHOD} must assign {_REPOSITORY} exactly once from "
    f"{_NORMALIZER}({_REPOSITORY_URL}) without post-normalization transforms"
)
SHARED_KEY_MESSAGE = f"{SHARED_METHOD} cache key must be the direct ({_REPOSITORY}, {_REF}) tuple"
SHARED_BARE_MESSAGE = "Tier-0 bare lookup must consume the direct normalized repository identity"

IDENTITY_MISSING_MESSAGE = f"{IDENTITY_FUNCTION} is missing"
IDENTITY_DUPLICATE_MESSAGE = f"{IDENTITY_FUNCTION} has duplicate definitions"
IDENTITY_COMPOSITION_MESSAGE = (
    f"{IDENTITY_FUNCTION} must directly return "
    f"{_NORMALIZER}({_GITHUB_URL}()) without indirect truncation"
)
L0_MISSING_MESSAGE = f"{L0_CLASS}.{L0_METHOD} is missing"
L0_CLASS_DUPLICATE_MESSAGE = f"{L0_CLASS} has duplicate definitions"
L0_DUPLICATE_MESSAGE = f"{L0_CLASS}.{L0_METHOD} has duplicate definitions"
L0_LOOKUP_MESSAGE = f"L0 lookup must call cache.get({IDENTITY_FUNCTION}({_DEP_REF}), {_REF})"
RESOLVE_MISSING_MESSAGE = f"{RESOLVER_CLASS}.{RESOLVE_METHOD} is missing"
RESOLVER_CLASS_DUPLICATE_MESSAGE = f"{RESOLVER_CLASS} has duplicate definitions"
RESOLVE_DUPLICATE_MESSAGE = f"{RESOLVER_CLASS}.{RESOLVE_METHOD} has duplicate definitions"
RESOLVE_KEY_MESSAGE = (
    f"resolver coalescing key must be the direct ({IDENTITY_FUNCTION}({_DEP_REF}), {_REF}) tuple"
)
SEED_MISSING_MESSAGE = f"{RESOLVER_CLASS}.{SEED_METHOD} is missing"
SEED_DUPLICATE_MESSAGE = f"{RESOLVER_CLASS}.{SEED_METHOD} has duplicate definitions"
SEED_PUT_MESSAGE = (
    f"lockfile seed must call _cache.put({IDENTITY_FUNCTION}({_DEP_REF}), {_REF}, sha)"
)

# The complete vocabulary this analyzer can emit. Callers (and the mutation
# suite) use it to tell a structural finding apart from the rule's retained
# lexical findings without re-listing the messages themselves.
STRUCTURAL_MESSAGES: tuple[str, ...] = (
    MISSING_SOURCE_MESSAGE,
    UNPARSEABLE_SOURCE_MESSAGE,
    SHARED_CLASS_DUPLICATE_MESSAGE,
    SHARED_METHOD_MISSING_MESSAGE,
    SHARED_METHOD_DUPLICATE_MESSAGE,
    SHARED_ASSIGN_MESSAGE,
    SHARED_KEY_MESSAGE,
    SHARED_BARE_MESSAGE,
    IDENTITY_MISSING_MESSAGE,
    IDENTITY_DUPLICATE_MESSAGE,
    IDENTITY_COMPOSITION_MESSAGE,
    L0_MISSING_MESSAGE,
    L0_CLASS_DUPLICATE_MESSAGE,
    L0_DUPLICATE_MESSAGE,
    L0_LOOKUP_MESSAGE,
    RESOLVE_MISSING_MESSAGE,
    RESOLVER_CLASS_DUPLICATE_MESSAGE,
    RESOLVE_DUPLICATE_MESSAGE,
    RESOLVE_KEY_MESSAGE,
    SEED_MISSING_MESSAGE,
    SEED_DUPLICATE_MESSAGE,
    SEED_PUT_MESSAGE,
)

# ``str(SyntaxError)`` renders as ``invalid syntax (module.py, line 12)``; the
# helper reported ``exc.lineno``, so recover it from the memoized diagnostic
# rather than re-parsing the source a second time to ask again.
_PARSE_ERROR_LINE = re.compile(r", line (\d+)\)\s*$")


@dataclass(frozen=True)
class IdentityFinding:
    """One structural finding, shaped like the helper's rendered violation."""

    path: str
    line: int
    message: str


@dataclass(frozen=True)
class SourceState:
    """One owner module exactly as the shared traversal already saw it.

    ``in_inventory`` stands in for the helper's ``Path.is_file()`` probe: the
    linter's one deterministic inventory is the only thing allowed to answer
    "does this file exist", and a read error is treated the same way (the
    helper would have raised there, which is strictly less safe).
    """

    path: str
    in_inventory: bool
    read_error: str | None
    parse_error: str | None
    index: TreeIndex | None

    @property
    def available(self) -> bool:
        """Return whether the file exists and was readable."""
        return self.in_inventory and self.read_error is None


# --------------------------------------------------------------------------
# AST-shape predicates (ported 1:1 from the helper).
# --------------------------------------------------------------------------


def _call_name(node: ast.AST) -> str | None:
    """Return the dotted callee name for a Name/Attribute expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _is_name(node: ast.AST, name: str) -> bool:
    """Return whether `node` is exactly the bare name `name`."""
    return isinstance(node, ast.Name) and node.id == name


def _is_call(node: ast.AST, name: str, args: tuple[str, ...]) -> bool:
    """Return whether `node` calls `name` on exactly these positional names."""
    return (
        isinstance(node, ast.Call)
        and _call_name(node.func) == name
        and len(node.args) == len(args)
        and not node.keywords
        and all(
            _is_name(argument, expected) for argument, expected in zip(node.args, args, strict=True)
        )
    )


def _repository_ref_tuple(node: ast.AST) -> bool:
    """Return whether `node` is the direct ``(repository, ref)`` tuple."""
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and _is_name(node.elts[0], _REPOSITORY)
        and _is_name(node.elts[1], _REF)
    )


def _repository_identity_call(node: ast.AST) -> bool:
    """Return whether `node` is ``_repository_cache_identity(dep_ref)``."""
    return _is_call(node, IDENTITY_FUNCTION, (_DEP_REF,))


def _identity_ref_tuple(node: ast.AST) -> bool:
    """Return whether `node` is ``(_repository_cache_identity(dep_ref), ref)``."""
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and _repository_identity_call(node.elts[0])
        and _is_name(node.elts[1], _REF)
    )


def _direct_identity_composition(node: ast.AST) -> bool:
    """Return whether `node` is ``normalize_repo_url(dep_ref.to_github_url())``."""
    if not isinstance(node, ast.Call) or _call_name(node.func) != _NORMALIZER:
        return False
    if len(node.args) != 1 or node.keywords:
        return False
    url_call = node.args[0]
    return (
        isinstance(url_call, ast.Call)
        and _call_name(url_call.func) == _GITHUB_URL
        and not url_call.args
        and not url_call.keywords
    )


# --------------------------------------------------------------------------
# Index-backed lookups (what the helper did with ``tree.body`` + ``ast.walk``).
# --------------------------------------------------------------------------


def _find_function(
    index: TreeIndex,
    function_name: str,
    *,
    class_name: str | None = None,
) -> ast.AST | None:
    """Return one module-level function, or one method of a top-level class.

    Only the module body is searched and only a top-level ``class_name`` is
    accepted as an owner. Python binds the last same-scope definition, so this
    returns the effective definition; duplicate-definition findings are
    reported separately rather than inspecting a dead earlier body.
    """
    if class_name is not None:
        owner = effective_definition(
            index,
            class_name,
            kinds=(ast.ClassDef,),
        )
        if owner is None:
            return None
        return effective_function(index, function_name, parent=owner)
    return effective_function(index, function_name)


def _duplicate_definition_findings(
    index: TreeIndex,
    path: str,
    *,
    class_methods: Sequence[tuple[str, tuple[str, ...]]] = (),
    module_functions: Sequence[str] = (),
) -> tuple[IdentityFinding, ...]:
    """Reject duplicate guarded definitions while inspecting Python's winner."""
    findings: list[IdentityFinding] = []
    for function_name in module_functions:
        definitions = direct_definitions(
            index,
            function_name,
            kinds=FUNCTION_NODES,
        )
        if len(definitions) > 1:
            message = {
                IDENTITY_FUNCTION: IDENTITY_DUPLICATE_MESSAGE,
            }[function_name]
            findings.append(IdentityFinding(path, _line_of(definitions[-1]), message))

    class_messages = {
        SHARED_CLASS: SHARED_CLASS_DUPLICATE_MESSAGE,
        L0_CLASS: L0_CLASS_DUPLICATE_MESSAGE,
        RESOLVER_CLASS: RESOLVER_CLASS_DUPLICATE_MESSAGE,
    }
    method_messages = {
        (SHARED_CLASS, SHARED_METHOD): SHARED_METHOD_DUPLICATE_MESSAGE,
        (L0_CLASS, L0_METHOD): L0_DUPLICATE_MESSAGE,
        (RESOLVER_CLASS, RESOLVE_METHOD): RESOLVE_DUPLICATE_MESSAGE,
        (RESOLVER_CLASS, SEED_METHOD): SEED_DUPLICATE_MESSAGE,
    }
    for class_name, method_names in class_methods:
        owners = direct_definitions(index, class_name, kinds=(ast.ClassDef,))
        if len(owners) > 1:
            findings.append(IdentityFinding(path, _line_of(owners[-1]), class_messages[class_name]))
        if not owners:
            continue
        effective_owner = owners[-1]
        for method_name in method_names:
            methods = direct_definitions(
                index,
                method_name,
                parent=effective_owner,
                kinds=FUNCTION_NODES,
            )
            if len(methods) > 1:
                findings.append(
                    IdentityFinding(
                        path,
                        _line_of(methods[-1]),
                        method_messages[(class_name, method_name)],
                    )
                )
    return tuple(findings)


def _sole_assignment_value(
    index: TreeIndex,
    function: ast.AST,
    target_name: str,
) -> ast.AST | None:
    """Return one exclusive own-scope assignment value, else ``None``.

    ``assignments_to`` is the shared authority for assignment forms, including
    augmented assignment. ``binding_nodes`` closes the remaining rebinding
    paths (loop/with targets, deletion, imports, and similar stores) so a
    canonical value cannot be replaced before it drives a cache operation.
    """
    assignments = assignments_to(index, function, target_name)
    if len(assignments) != 1 or assignments[0].value is None:
        return None
    assignment_node = assignments[0].node
    direct_target = (
        len(assignment_node.targets) == 1 and assignment_node.targets[0]
        if isinstance(assignment_node, ast.Assign)
        else assignment_node.target
        if isinstance(assignment_node, (ast.AnnAssign, ast.NamedExpr, ast.AugAssign))
        else None
    )
    if not _is_name(direct_target, target_name):
        return None
    allowed_binding_ids = {id(node) for node in index.walk(assignment_node)}
    if any(
        id(node) not in allowed_binding_ids
        for node in binding_nodes(
            index,
            target_name,
            nodes=index.own_scope(function),
        )
    ):
        return None
    if any(
        isinstance(node, ast.Nonlocal) and target_name in node.names
        for node in index.walk(function)
    ):
        return None
    return assignments[0].value


def _calls_to(index: TreeIndex, function: ast.AST, callee: str) -> list[ast.Call]:
    """Return every call to the dotted `callee` anywhere inside `function`."""
    return [
        node
        for node in index.walk(function)
        if isinstance(node, ast.Call) and _call_name(node.func) == callee
    ]


def _returns_in(index: TreeIndex, function: ast.AST) -> list[ast.Return]:
    """Return every ``return`` statement anywhere inside `function`."""
    return [node for node in index.walk(function) if isinstance(node, ast.Return)]


def _line_of(node: ast.AST) -> int:
    """Return a node's 1-based line, defaulting to 1 for line-less nodes."""
    return max(getattr(node, "lineno", 1), 1)


# --------------------------------------------------------------------------
# Owner analyses (ported 1:1 from ``_analyze_shared`` / ``_analyze_tiered``).
# --------------------------------------------------------------------------


def shared_cache_findings(index: TreeIndex) -> tuple[IdentityFinding, ...]:
    """Return ``SharedCloneCache`` violations for the shared-clone owner."""
    path = SHARED_CACHE_PATH
    findings = list(
        _duplicate_definition_findings(
            index,
            path,
            class_methods=((SHARED_CLASS, (SHARED_METHOD,)),),
        )
    )
    method = _find_function(index, SHARED_METHOD, class_name=SHARED_CLASS)
    if method is None:
        findings.append(IdentityFinding(path, 1, SHARED_METHOD_MISSING_MESSAGE))
        return tuple(findings)

    line = _line_of(method)

    repository_value = _sole_assignment_value(index, method, _REPOSITORY)
    if (
        repository_value is None
        or not has_exclusive_import(
            index,
            name=_NORMALIZER,
            module="cache.url_normalize",
            level=2,
        )
        or binding_nodes(index, _NORMALIZER, nodes=index.own_scope(method))
        or not _is_call(repository_value, _NORMALIZER, (_REPOSITORY_URL,))
    ):
        findings.append(IdentityFinding(path, line, SHARED_ASSIGN_MESSAGE))

    key_value = _sole_assignment_value(index, method, _KEY)
    if key_value is None or not _repository_ref_tuple(key_value):
        findings.append(IdentityFinding(path, line, SHARED_KEY_MESSAGE))

    bare_lookups = _calls_to(index, method, _BARE_LOOKUP)
    if len(bare_lookups) != 1 or not (
        len(bare_lookups[0].args) == 1 and _is_name(bare_lookups[0].args[0], _REPOSITORY)
    ):
        findings.append(IdentityFinding(path, line, SHARED_BARE_MESSAGE))
    return tuple(findings)


def _identity_findings(index: TreeIndex) -> tuple[IdentityFinding, ...] | None:
    """Return identity-helper violations, or ``None`` when it is missing.

    ``None`` reproduces the helper's short-circuit: a missing
    ``_repository_cache_identity`` is the only tiered defect that suppresses
    the remaining consumer checks.
    """
    identity = _find_function(index, IDENTITY_FUNCTION)
    if identity is None:
        return None
    returns = _returns_in(index, identity)
    if (
        len(returns) != 1
        or returns[0].value is None
        or not has_exclusive_import(
            index,
            name=_NORMALIZER,
            module="cache.url_normalize",
            level=2,
            scope=identity,
        )
        or not _direct_identity_composition(returns[0].value)
        or assignment_nodes(index, identity)
    ):
        return (
            IdentityFinding(TIERED_RESOLVER_PATH, _line_of(identity), IDENTITY_COMPOSITION_MESSAGE),
        )
    return ()


def _l0_findings(index: TreeIndex) -> tuple[IdentityFinding, ...]:
    """Return the L0 per-run lookup violations."""
    path = TIERED_RESOLVER_PATH
    l0 = _find_function(index, L0_METHOD, class_name=L0_CLASS)
    if l0 is None:
        return (IdentityFinding(path, 1, L0_MISSING_MESSAGE),)
    valid = any(
        isinstance(node.value, ast.Call)
        and _call_name(node.value.func) == _L0_GET
        and len(node.value.args) == 2
        and _repository_identity_call(node.value.args[0])
        and _is_name(node.value.args[1], _REF)
        for node in _returns_in(index, l0)
    )
    if valid:
        return ()
    return (IdentityFinding(path, _line_of(l0), L0_LOOKUP_MESSAGE),)


def _resolve_findings(index: TreeIndex) -> tuple[IdentityFinding, ...]:
    """Return the resolver coalescing-key violations."""
    path = TIERED_RESOLVER_PATH
    resolve = _find_function(index, RESOLVE_METHOD, class_name=RESOLVER_CLASS)
    if resolve is None:
        return (IdentityFinding(path, 1, RESOLVE_MISSING_MESSAGE),)
    key_value = _sole_assignment_value(index, resolve, _KEY)
    if key_value is None or not _identity_ref_tuple(key_value):
        return (IdentityFinding(path, _line_of(resolve), RESOLVE_KEY_MESSAGE),)
    return ()


def _seed_findings(index: TreeIndex) -> tuple[IdentityFinding, ...]:
    """Return the lockfile-seed violations."""
    path = TIERED_RESOLVER_PATH
    seed = _find_function(index, SEED_METHOD, class_name=RESOLVER_CLASS)
    if seed is None:
        return (IdentityFinding(path, 1, SEED_MISSING_MESSAGE),)
    puts = _calls_to(index, seed, _SEED_PUT)
    if len(puts) != 1 or not (
        len(puts[0].args) >= 2
        and _repository_identity_call(puts[0].args[0])
        and _is_name(puts[0].args[1], _REF)
    ):
        return (IdentityFinding(path, _line_of(seed), SEED_PUT_MESSAGE),)
    return ()


def tiered_resolver_findings(index: TreeIndex) -> tuple[IdentityFinding, ...]:
    """Return every tiered-resolver violation, in the helper's message order."""
    duplicates = _duplicate_definition_findings(
        index,
        TIERED_RESOLVER_PATH,
        module_functions=(IDENTITY_FUNCTION,),
        class_methods=(
            (L0_CLASS, (L0_METHOD,)),
            (RESOLVER_CLASS, (RESOLVE_METHOD, SEED_METHOD)),
        ),
    )
    identity = _identity_findings(index)
    if identity is None:
        return (*duplicates, IdentityFinding(TIERED_RESOLVER_PATH, 1, IDENTITY_MISSING_MESSAGE))
    return (
        *duplicates,
        *identity,
        *_l0_findings(index),
        *_resolve_findings(index),
        *_seed_findings(index),
    )


# --------------------------------------------------------------------------
# Fail-closed entry point.
# --------------------------------------------------------------------------


def _parse_error_line(parse_error: str) -> int:
    """Recover ``SyntaxError.lineno`` from the memoized parse diagnostic."""
    match = _PARSE_ERROR_LINE.search(parse_error)
    return int(match.group(1)) if match else 1


def _analyze_one(
    state: SourceState,
    analyzer: Callable[[TreeIndex], tuple[IdentityFinding, ...]],
) -> tuple[IdentityFinding, ...]:
    """Run one owner analysis, failing closed on an unusable parse."""
    if state.parse_error is not None:
        line = _parse_error_line(state.parse_error)
        return (IdentityFinding(state.path, line, UNPARSEABLE_SOURCE_MESSAGE),)
    if state.index is None:
        # Readable and syntactically valid, yet the shared traversal recorded
        # nothing: no structural claim can be made, so refuse to pass.
        return (IdentityFinding(state.path, 1, UNPARSEABLE_SOURCE_MESSAGE),)
    return analyzer(state.index)


def analyze(shared: SourceState, tiered: SourceState) -> tuple[IdentityFinding, ...]:
    """Return every semantic owner violation across the configured pair.

    Reproduces ``check()`` end to end, including its short-circuit: when either
    configured owner path is absent, only the missing-path diagnostics are
    reported and neither module is analyzed.  Parse failures do *not*
    short-circuit -- the sibling module is still analyzed, exactly as the
    helper's per-file ``_parse`` did.
    """
    unavailable = tuple(
        IdentityFinding(state.path, 1, MISSING_SOURCE_MESSAGE)
        for state in (shared, tiered)
        if not state.available
    )
    if unavailable:
        return unavailable
    return tuple(
        sorted(
            (
                *_analyze_one(shared, shared_cache_findings),
                *_analyze_one(tiered, tiered_resolver_findings),
            ),
            key=lambda finding: (finding.path, finding.line, finding.message),
        )
    )


__all__ = [
    "IDENTITY_FUNCTION",
    "L0_CLASS",
    "L0_METHOD",
    "MISSING_SOURCE_MESSAGE",
    "OWNER_PATHS",
    "RESOLVER_CLASS",
    "RESOLVE_METHOD",
    "SEED_METHOD",
    "SHARED_CACHE_PATH",
    "SHARED_CLASS",
    "SHARED_METHOD",
    "STRUCTURAL_MESSAGES",
    "TIERED_RESOLVER_PATH",
    "UNPARSEABLE_SOURCE_MESSAGE",
    "IdentityFinding",
    "SourceState",
    "TreeIndex",
    "analyze",
    "shared_cache_findings",
    "tiered_resolver_findings",
]
