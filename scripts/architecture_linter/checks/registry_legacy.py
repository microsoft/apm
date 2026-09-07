"""Pure analyzers for the core-runtime legacy authority checks.

Two of the legacy ``lint-architecture-boundaries.sh`` units delegated to
standalone helper CLIs whose semantics are structural rather than lexical:

* ``scripts/check_diagnostic_ascii_owner.py`` -- printable-ASCII ownership of
  agent diagnostic identifiers (AC12, legacy L1143-1150); and
* ``scripts/check_agents_source_attribution_owner.py`` -- the canonical
  ``source_attribution`` manifest boolean threaded into the AGENTS.md renderer
  (AC2, legacy L210-218).

Both are ported here as pure functions over facts captured by the linter's one
shared AST traversal.  That traversal already records every node's
``(node, parent)`` pair intrinsically, so these analyzers register nothing:
they hand one file's facts to the canonical
:func:`~scripts.architecture_linter.checks.tree_index.build_tree_index` and
answer subtree questions from the resulting index -- without ever re-reading a
file, re-parsing source, calling ``ast.parse``/``ast.walk``, using an
``ast.NodeVisitor``, or shelling out to the original helper.

The canonical index precomputes each node's *own-scope anchor* -- the nearest
enclosing function or lambda -- which is what reproduces the helper's
``_walk_own_scope`` semantics (descend freely, but treat a nested function or
lambda as an included leaf rather than a scope to enter).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.checks.tree_index import (
    FUNCTION_NODES,
    TreeIndex,
)

# --------------------------------------------------------------------------
# The exact file set these analyzers may know about.
# --------------------------------------------------------------------------

DIAGNOSTIC_OWNER_PATH = "src/apm_cli/utils/diagnostics.py"
AGENT_CONSUMER_PATH = "src/apm_cli/integration/agent_integrator.py"
OPENCODE_CONSUMER_PATH = "src/apm_cli/integration/opencode_frontmatter.py"
DISTRIBUTED_COMPILER_PATH = "src/apm_cli/compilation/distributed_compiler.py"

DIAGNOSTIC_ASCII_PATHS: tuple[str, ...] = (
    DIAGNOSTIC_OWNER_PATH,
    AGENT_CONSUMER_PATH,
    OPENCODE_CONSUMER_PATH,
)
TRACKED_PATHS: tuple[str, ...] = (*DIAGNOSTIC_ASCII_PATHS, DISTRIBUTED_COMPILER_PATH)

# --------------------------------------------------------------------------
# check_diagnostic_ascii_owner.py constants (verbatim).
# --------------------------------------------------------------------------

OWNER_MODULE = "apm_cli.utils.diagnostics"
OWNER_SYMBOL = "printable_ascii_text"
RETIRED_SYMBOL = "_ascii_safe_name"
OPENCODE_FUNCTION = "validate_opencode_frontmatter"
# Ordered exactly as the helper's dict; value is its ``require_source`` flag.
AGENT_DIAGNOSTIC_FUNCTIONS: Mapping[str, bool] = {
    "AgentIntegrator._warn_codex_unverified_scope": True,
    "AgentIntegrator._warn_codex_tools_dropped": True,
    "AgentIntegrator._warn_opencode_frontmatter": False,
}
ALLOWED_IDENTITY_DELEGATES: Mapping[str, frozenset[str]] = {
    "AgentIntegrator._warn_opencode_frontmatter": frozenset({OPENCODE_FUNCTION}),
}
_DIAGNOSTIC_SINKS: frozenset[str] = frozenset({"warn", "lossy_agent_compilation"})
_ASCII_PROBE_METHODS: frozenset[str] = frozenset({"isascii", "isprintable"})
_ASCII_CODEC_METHODS: frozenset[str] = frozenset({"encode", "decode"})
_ASCII_BOUNDARY_CONSTANTS: frozenset[int] = frozenset({0x20, 0x7E, 0x7F})

# --------------------------------------------------------------------------
# check_agents_source_attribution_owner.py constants (verbatim).
# --------------------------------------------------------------------------

COMPILER_CLASS = "DistributedAgentsCompiler"
COMPILE_METHOD = "compile_distributed"
RENDER_METHOD = "_generate_agents_content"
CONFIG_KEY = "source_attribution"

_VALUE_ASSIGN_NODES = (ast.Assign, ast.AnnAssign, ast.NamedExpr)


@dataclass(frozen=True)
class LegacyFinding:
    """One structural finding, shaped like a helper's rendered violation."""

    path: str
    line: int
    message: str


def _sorted_findings(findings: Sequence[LegacyFinding]) -> tuple[LegacyFinding, ...]:
    """Sort exactly as the helper's ``(path, line, message)`` ordering."""
    return tuple(sorted(findings, key=lambda item: (item.path, item.line, item.message)))


# --------------------------------------------------------------------------
# Diagnostic-identity predicates (ported 1:1 from the helper).
# --------------------------------------------------------------------------


def _is_source_name(node: ast.AST) -> bool:
    """Return whether `node` is exactly the ``source.name`` load expression."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "name"
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id == "source"
        and isinstance(node.value.ctx, ast.Load)
    )


def _is_package_name(node: ast.AST) -> bool:
    """Return whether `node` is exactly the ``package_name`` load expression."""
    return (
        isinstance(node, ast.Name) and node.id == "package_name" and isinstance(node.ctx, ast.Load)
    )


def _is_identity(node: ast.AST) -> bool:
    """Return whether `node` is source.name or package_name."""
    return _is_source_name(node) or _is_package_name(node)


def _is_owner_call(node: ast.AST, argument: str | None = None) -> bool:
    """Return whether `node` directly calls the owner on `argument`.

    `argument` selects the required single positional identity: ``"source"``
    for ``source.name``, ``"package"`` for ``package_name``, or ``None`` to
    accept any single argument.
    """
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == OWNER_SYMBOL
        and len(node.args) == 1
    ):
        return False
    if argument is None:
        return True
    if argument == "source":
        return _is_source_name(node.args[0])
    return _is_package_name(node.args[0])


def _contains_name(index: TreeIndex, node: ast.AST, name: str) -> bool:
    """Return whether an expression subtree contains a load of `name`."""
    return any(
        isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id == name
        for child in index.walk(node)
    )


def _diagnostic_calls(index: TreeIndex, function: ast.AST) -> tuple[ast.AST, ...]:
    """Return DiagnosticCollector calls that render one Codex warning."""
    return tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DIAGNOSTIC_SINKS
    )


def _message_appends(index: TreeIndex, function: ast.AST) -> tuple[ast.AST, ...]:
    """Return ``messages.append(...)`` calls in one function's own scope."""
    return tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "messages"
        and node.func.attr == "append"
    )


def _identity_is_directly_owned_in_diagnostic(
    index: TreeIndex, function: ast.AST, *, require_source: bool
) -> bool:
    """Return whether one diagnostic call owns source and package rendering."""
    for call in _diagnostic_calls(index, function):
        owner_calls = [node for node in index.walk(call) if _is_owner_call(node)]
        source_owned = any(_is_owner_call(node, "source") for node in owner_calls)
        package_owned = any(_is_owner_call(node, "package") for node in owner_calls)
        if package_owned and (source_owned or not require_source):
            return True
    return False


def _assignments_to(index: TreeIndex, function: ast.AST, name: str) -> tuple[ast.AST, ...]:
    """Return simple assignments to `name` in one function scope."""
    return tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )


def _opencode_identity_flow_is_owned(index: TreeIndex, function: ast.AST) -> bool:
    """Return whether OpenCode messages derive identity only from the owner."""
    safe_name_assignments = _assignments_to(index, function, "safe_name")
    identifier_assignments = _assignments_to(index, function, "identifier")
    if len(safe_name_assignments) != 1 or len(identifier_assignments) != 1:
        return False
    if not _is_owner_call(safe_name_assignments[0].value, "source"):
        return False
    identifier = identifier_assignments[0].value
    if not any(_is_owner_call(node, "package") for node in index.walk(identifier)):
        return False
    if not _contains_name(index, identifier, "safe_name"):
        return False
    message_appends = _message_appends(index, function)
    return bool(message_appends) and all(
        _contains_name(index, call, "identifier") for call in message_appends
    )


def _raw_identity_lines(index: TreeIndex, root: ast.AST) -> tuple[int, ...]:
    """Return raw identity loads not owned by ``printable_ascii_text``.

    ``IfExp.test`` is control flow rather than rendered data, so a predicate
    such as ``... if package_name else ...`` is allowed. Both branches remain
    checked because either may contribute bytes to output. Descent stops at an
    identity node, exactly as the helper's recursive visitor does.

    Both prunes are expressed as reachability over the index's precomputed
    pre-order subtree rather than as a second traversal: a node is skipped when
    its parent was pruned, when its parent is itself an identity node, or when
    it is the ``test`` of an enclosing ``IfExp``. Pre-order guarantees a parent
    is classified before its children, so one linear pass is exact.
    """
    lines: list[int] = []
    pruned: set[int] = set()
    for node in index.walk(root):
        if node is root:
            # The helper seeds its walk with an explicit ``None`` parent, so the
            # subtree root is judged unowned even when its enclosing node is a call.
            parent: ast.AST | None = None
        else:
            parent = index.parent(node)
            if (
                parent is None
                or id(parent) in pruned
                or _is_identity(parent)
                or (isinstance(parent, ast.IfExp) and node is parent.test)
            ):
                pruned.add(id(node))
                continue
        if _is_identity(node) and not (isinstance(parent, ast.Call) and _is_owner_call(parent)):
            lines.append(getattr(node, "lineno", 1))
    return tuple(lines)


def _call_name(call: ast.Call) -> str | None:
    """Return the simple name of one call target."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _directly_consumes_identity(call: ast.Call) -> bool:
    """Return whether a call directly receives a diagnostic identity value."""
    if any(_is_identity(argument) for argument in call.args):
        return True
    if any(_is_identity(keyword.value) for keyword in call.keywords):
        return True
    return isinstance(call.func, ast.Attribute) and _is_identity(call.func.value)


def _non_owner_identity_calls(
    index: TreeIndex, function: ast.AST, allowed_delegates: frozenset[str]
) -> tuple[ast.Call, ...]:
    """Return calls that locally transform diagnostic identity inputs."""
    return tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Call)
        and _directly_consumes_identity(node)
        and not _is_owner_call(node)
        and _call_name(node) not in allowed_delegates
    )


def _ascii_codec_call(node: ast.AST) -> bool:
    """Return whether a call locally encodes or decodes as ASCII."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _ASCII_CODEC_METHODS
        and node.args
    ):
        return False
    encoding = node.args[0]
    return (
        isinstance(encoding, ast.Constant)
        and isinstance(encoding.value, str)
        and encoding.value.lower() == "ascii"
    )


def _local_ascii_signal(node: ast.AST) -> bool:
    """Return whether a consumer locally implements printable-ASCII logic."""
    if _ascii_codec_call(node):
        return True
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "ord":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in _ASCII_PROBE_METHODS:
            return True
    return isinstance(node, ast.Constant) and node.value in _ASCII_BOUNDARY_CONSTANTS


def _imports_owner(index: TreeIndex) -> bool:
    """Return whether the consumer imports the canonical owner directly."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == OWNER_MODULE
        and any(alias.name == OWNER_SYMBOL and alias.asname is None for alias in node.names)
        for node in index.module_children()
    )


# --------------------------------------------------------------------------
# AC12: printable-ASCII ownership over agent diagnostic identifiers.
# --------------------------------------------------------------------------


def _function_findings(
    path: str, index: TreeIndex, qualname: str, function: ast.AST, *, is_agent: bool
) -> list[LegacyFinding]:
    """Return every routing violation for one required consumer function."""
    findings: list[LegacyFinding] = []
    if is_agent:
        flow_is_owned = _identity_is_directly_owned_in_diagnostic(
            index, function, require_source=AGENT_DIAGNOSTIC_FUNCTIONS[qualname]
        )
        output_calls = _diagnostic_calls(index, function)
    else:
        flow_is_owned = _opencode_identity_flow_is_owned(index, function)
        output_calls = _message_appends(index, function)

    if not flow_is_owned:
        findings.append(
            LegacyFinding(
                path,
                function.lineno,
                f"{qualname} must derive rendered diagnostic identity directly from "
                f"{OWNER_MODULE}.{OWNER_SYMBOL}",
            )
        )
    for output_call in output_calls:
        findings.extend(
            LegacyFinding(path, line, f"{qualname} must not render raw source.name or package_name")
            for line in _raw_identity_lines(index, output_call)
        )
    for assignment in index.own_scope(function):
        if not isinstance(assignment, _VALUE_ASSIGN_NODES):
            continue
        value = assignment.value
        if value is None:
            continue
        findings.extend(
            LegacyFinding(
                path, line, f"{qualname} must not assign raw diagnostic identity for later use"
            )
            for line in _raw_identity_lines(index, value)
        )
    allowed_delegates = ALLOWED_IDENTITY_DELEGATES.get(qualname, frozenset())
    findings.extend(
        LegacyFinding(
            path,
            call.lineno,
            f"{qualname} must not pass source.name or package_name through "
            "a local normalization path",
        )
        for call in _non_owner_identity_calls(index, function, allowed_delegates)
    )
    return findings


def _module_scope_findings(path: str, index: TreeIndex) -> list[LegacyFinding]:
    """Return owner-shadowing and local-normalizer findings for one consumer."""
    findings: list[LegacyFinding] = []
    for node in index.nodes:
        if isinstance(node, FUNCTION_NODES) and node.name in {OWNER_SYMBOL, RETIRED_SYMBOL}:
            findings.append(
                LegacyFinding(
                    path,
                    node.lineno,
                    f"must not define local diagnostic ASCII normalizer {node.name}",
                )
            )
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == OWNER_SYMBOL
        ):
            findings.append(
                LegacyFinding(path, node.lineno, f"must not shadow canonical owner {OWNER_SYMBOL}")
            )
        elif isinstance(node, ast.Name) and node.id == RETIRED_SYMBOL:
            findings.append(
                LegacyFinding(path, node.lineno, f"retired {RETIRED_SYMBOL} must not be restored")
            )
        elif _local_ascii_signal(node):
            findings.append(
                LegacyFinding(
                    path,
                    getattr(node, "lineno", 1),
                    f"must delegate printable-ASCII normalization to "
                    f"{OWNER_MODULE}.{OWNER_SYMBOL}, not reimplement it locally",
                )
            )
    return findings


def _consumer_findings(path: str, index: TreeIndex) -> list[LegacyFinding]:
    """Return every violation the helper reports for one consumer file."""
    findings: list[LegacyFinding] = []
    if not _imports_owner(index):
        findings.append(
            LegacyFinding(path, 1, f"must import {OWNER_SYMBOL} directly from {OWNER_MODULE}")
        )
    is_agent = path == AGENT_CONSUMER_PATH
    expected = tuple(AGENT_DIAGNOSTIC_FUNCTIONS) if is_agent else (OPENCODE_FUNCTION,)
    for qualname in expected:
        function = index.function(qualname)
        if function is None:
            findings.append(
                LegacyFinding(path, 1, f"required consumer function missing: {qualname}")
            )
            continue
        findings.extend(_function_findings(path, index, qualname, function, is_agent=is_agent))
    findings.extend(_module_scope_findings(path, index))
    return findings


def diagnostic_ascii_findings(indexes: Mapping[str, TreeIndex | None]) -> tuple[LegacyFinding, ...]:
    """Return canonical-owner and consumer-routing violations, helper-ordered.

    Mirrors ``scripts/check_diagnostic_ascii_owner.py::check``: one violation
    per structural defect, sorted by ``(path, line, message)``. A missing index
    for a configured file fails closed with its own violation rather than
    silently skipping the file.
    """
    findings: list[LegacyFinding] = []
    missing = tuple(path for path in DIAGNOSTIC_ASCII_PATHS if indexes.get(path) is None)
    if missing:
        return _sorted_findings(
            [
                LegacyFinding(path, 1, "diagnostic ASCII owner check failed closed: no parsed tree")
                for path in missing
            ]
        )

    owner = indexes[DIAGNOSTIC_OWNER_PATH]
    owner_defs = [
        node
        for node in owner.module_children()
        if isinstance(node, FUNCTION_NODES) and node.name == OWNER_SYMBOL
    ]
    if len(owner_defs) != 1:
        findings.append(
            LegacyFinding(
                DIAGNOSTIC_OWNER_PATH,
                1,
                f"{OWNER_MODULE}.{OWNER_SYMBOL} must have exactly one definition",
            )
        )
    for path in (AGENT_CONSUMER_PATH, OPENCODE_CONSUMER_PATH):
        findings.extend(_consumer_findings(path, indexes[path]))
    return _sorted_findings(findings)


# --------------------------------------------------------------------------
# AC2: AGENTS.md cosmetics must consume the canonical config boolean.
# --------------------------------------------------------------------------


def _find_compile_method(index: TreeIndex) -> ast.AST | None:
    """Return ``DistributedAgentsCompiler.compile_distributed`` from the module body."""
    for node in index.module_children():
        if not isinstance(node, ast.ClassDef) or node.name != COMPILER_CLASS:
            continue
        for member in index.children(node):
            if isinstance(member, ast.FunctionDef) and member.name == COMPILE_METHOD:
                return member
    return None


def _reads_attribution_from_config(index: TreeIndex, method: ast.AST) -> bool:
    """Return whether the method binds the canonical manifest boolean."""
    for node in index.walk(method):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == CONFIG_KEY
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "get"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "config"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            continue
        if node.value.args[0].value == CONFIG_KEY:
            return True
    return False


def _forwards_attribution_to_renderer(index: TreeIndex, method: ast.AST) -> bool:
    """Return whether the renderer receives the canonical boolean unchanged."""
    for node in index.walk(method):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == RENDER_METHOD
        ):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == CONFIG_KEY
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == CONFIG_KEY
            ):
                return True
    return False


def agents_source_attribution_findings(index: TreeIndex | None) -> tuple[LegacyFinding, ...]:
    """Return authority violations for the configured distributed compiler.

    Mirrors ``scripts/check_agents_source_attribution_owner.py::find_violations``
    including its message order: the config-read defect precedes the renderer-
    forwarding defect, and a missing owner method short-circuits both.
    """
    path = DISTRIBUTED_COMPILER_PATH
    if index is None:
        return (LegacyFinding(path, 1, "cannot analyze distributed compiler: no parsed tree"),)
    method = _find_compile_method(index)
    if method is None:
        return (LegacyFinding(path, 1, f"{COMPILER_CLASS}.{COMPILE_METHOD} is missing"),)

    findings: list[LegacyFinding] = []
    if not _reads_attribution_from_config(index, method):
        findings.append(
            LegacyFinding(
                path,
                method.lineno,
                f"{COMPILE_METHOD} must read {CONFIG_KEY} from config before rendering",
            )
        )
    if not _forwards_attribution_to_renderer(index, method):
        findings.append(
            LegacyFinding(
                path,
                method.lineno,
                f"{COMPILE_METHOD} must pass {CONFIG_KEY}={CONFIG_KEY} to "
                f"{RENDER_METHOD}, not the placement source map",
            )
        )
    return tuple(findings)


__all__ = [
    "AGENT_CONSUMER_PATH",
    "DIAGNOSTIC_ASCII_PATHS",
    "DIAGNOSTIC_OWNER_PATH",
    "DISTRIBUTED_COMPILER_PATH",
    "OPENCODE_CONSUMER_PATH",
    "TRACKED_PATHS",
    "LegacyFinding",
    "TreeIndex",
    "agents_source_attribution_findings",
    "diagnostic_ascii_findings",
]
