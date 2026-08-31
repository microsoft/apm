"""Scope and name-binding resolution primitives for structural authorities.

Pure AST-shape helpers -- attribute/literal extraction, one-hop alias
binding resolution, and scope-local name collection -- shared by both
halves of :mod:`contracts_structural_authorities`'s binary-selection and
rendered-parity detection. Split out purely for module-size budget reasons;
every function here is side-effect-free and reads only the cached
:class:`~scripts.architecture_linter.checks.tree_index.TreeIndex`.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence

from scripts.architecture_linter.checks.contracts_test_shared import _APM_EXECUTABLE_NAMES
from scripts.architecture_linter.checks.python_semantics import propagated_assignment_values
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex

_LOCAL_BINARY_FACADES = frozenset({"_resolve_apm_executable", "apm_binary", "apm_command"})


def _attribute_name(node: ast.AST) -> str | None:
    """Return a dotted attribute name (Name.attr...), or None."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _literal_string(node: ast.AST) -> str | None:
    """Return the string value of a string constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _bound_names(node: ast.AST) -> set[str]:
    """Return names bound by a simple or tuple/list assignment target."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for element in node.elts for name in _bound_names(element)}
    return set()


def _resolve_binding(node: ast.AST, bindings: dict[str, str]) -> str | None:
    """Resolve a Name/Attribute expression through the scope binding map."""
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_binding(node.value, bindings)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _ancestors(node: ast.AST, index: TreeIndex) -> list[ast.AST]:
    """Return every ancestor of `node` via the index's precomputed parents."""
    ancestors: list[ast.AST] = []
    current = index.parent(node)
    while current is not None:
        ancestors.append(current)
        current = index.parent(current)
    return ancestors


def _scope_nodes(index: TreeIndex, body: list[ast.stmt]) -> list[ast.AST]:
    """Return statement-subtree nodes for a scope, excluding nested scopes.

    Each body statement contributes its precomputed definition scope: the
    statement plus every descendant sharing its nearest enclosing
    ``def``/``class``, which stops at (and omits) nested definitions. A
    statement that *is* a nested definition contributes nothing, exactly as
    the legacy pre-order walk skipped it.
    """
    return [node for statement in body for node in index.definition_scope(statement)]


def _binding_scope_pairs(index: TreeIndex) -> list[tuple[ast.AST, ast.AST | None]]:
    """Return ``(scope, enclosing scope)`` pairs, outermost first.

    Reproduces the legacy helper's scope recursion without recursing: the
    module is the outermost scope, a class body is transparent (its *direct*
    methods bind in the class's own enclosing scope), and a definition nested
    under an unreachable scope stays unreachable. ``index.nodes`` is pre-order,
    so an enclosing scope is always resolved before anything it encloses.
    """
    root = index.root
    if root is None:
        return []
    pairs: list[tuple[ast.AST, ast.AST | None]] = [(root, None)]
    hosts: dict[int, ast.AST] = {id(root): root}
    transparent: dict[int, ast.AST] = {}
    for node in index.nodes:
        anchor = index.definition_anchor(node)
        if isinstance(node, ast.ClassDef):
            host = root if anchor is None else hosts.get(id(anchor))
            if host is not None:
                transparent[id(node)] = host
        elif isinstance(node, FUNCTION_NODES):
            if anchor is None:
                host = root
            elif isinstance(anchor, ast.ClassDef):
                host = transparent.get(id(anchor)) if index.parent(node) is anchor else None
            else:
                host = hosts.get(id(anchor))
            if host is not None:
                hosts[id(node)] = node
                pairs.append((node, host))
    return pairs


def _scope_local_names(scope: ast.AST, scope_nodes: Sequence[ast.AST]) -> set[str]:
    """Return the names a function scope binds locally (params plus targets)."""
    local_names = {
        argument.arg
        for argument in (
            *scope.args.posonlyargs,
            *scope.args.args,
            *scope.args.kwonlyargs,
        )
    }
    local_names.update(
        name
        for child in scope_nodes
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        for target in (child.targets if isinstance(child, ast.Assign) else [child.target])
        for name in _bound_names(target)
    )
    return local_names


def _scope_bindings(
    scope: ast.AST, scope_nodes: Sequence[ast.AST], inherited: dict[str, str]
) -> dict[str, str]:
    """Resolve one scope's module-alias bindings to a fixed point."""
    bindings = dict(inherited)
    if isinstance(scope, FUNCTION_NODES):
        for name in _scope_local_names(scope, scope_nodes):
            bindings.pop(name, None)

    for statement in scope_nodes:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name in {"os", "shutil", "subprocess", "sys"}:
                    bindings[alias.asname or alias.name] = alias.name
        elif isinstance(statement, ast.ImportFrom) and statement.module in {
            "os",
            "shutil",
            "subprocess",
        }:
            for alias in statement.names:
                bindings[alias.asname or alias.name] = f"{statement.module}.{alias.name}"

    for _ in range(len(scope_nodes) + 1):
        changed = False
        for statement in scope_nodes:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            value = statement.value
            if value is None:
                continue
            resolved = _resolve_binding(value, bindings)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                for name in _bound_names(target):
                    if resolved is None:
                        bindings.pop(name, None)
                    elif bindings.get(name) != resolved:
                        bindings[name] = resolved
                        changed = True
        if not changed:
            break
    return bindings


def _scope_binding_maps(
    index: TreeIndex,
) -> tuple[dict[int, dict[str, str]], dict[int, ast.AST]]:
    """Reproduce check_test_contract_authorities._scope_binding_maps.

    The legacy helper recursed scope-into-scope and re-descended each scope's
    statements to attribute nodes. Here the index supplies the binding-scope
    chain outermost-first and each scope's statement set, so one flat pass
    produces the same two maps with no second traversal.
    """
    bindings_by_scope: dict[int, dict[str, str]] = {}
    scope_by_node: dict[int, ast.AST] = {}
    if index.root is None:
        return bindings_by_scope, scope_by_node

    for scope, host in _binding_scope_pairs(index):
        scope_nodes = _scope_nodes(index, scope.body)
        inherited = {} if host is None else bindings_by_scope.get(id(host), {})
        bindings_by_scope[id(scope)] = _scope_bindings(scope, scope_nodes, inherited)
        for node in scope_nodes:
            scope_by_node[id(node)] = scope
    return bindings_by_scope, scope_by_node


ScopeBindingMaps = tuple[dict[int, dict[str, str]], dict[int, ast.AST]]


def _direct_binary_env_read_lines(
    index: TreeIndex,
    binding_maps: ScopeBindingMaps | None = None,
) -> list[int]:
    """Find direct APM_BINARY_PATH environment reads."""
    bindings_by_scope, scope_by_node = binding_maps or _scope_binding_maps(index)
    root = index.root
    lines: set[int] = set()
    for node in index.walk(root):
        scope = scope_by_node.get(id(node), root)
        bindings = bindings_by_scope.get(id(scope), {})
        if isinstance(node, ast.Call) and node.args:
            called = _resolve_binding(node.func, bindings)
            reads_variable = _literal_string(node.args[0]) == "APM_BINARY_PATH"
            if reads_variable and called in {"os.environ.get", "os.getenv"}:
                lines.add(node.lineno)
        elif isinstance(node, ast.Subscript) and _literal_string(node.slice) == "APM_BINARY_PATH":
            if _resolve_binding(node.value, bindings) == "os.environ":
                lines.add(node.lineno)
    return sorted(lines)


def _direct_binary_path_lookup_lines(
    index: TreeIndex,
    binding_maps: ScopeBindingMaps | None = None,
) -> list[int]:
    """Find direct ``shutil.which("apm")`` PATH lookups."""
    bindings_by_scope, scope_by_node = binding_maps or _scope_binding_maps(index)
    root = index.root
    lines: set[int] = set()
    for node in index.walk(root):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        scope = scope_by_node.get(id(node), root)
        called = _resolve_binding(node.func, bindings_by_scope.get(id(scope), {}))
        if _literal_string(node.args[0]) not in _APM_EXECUTABLE_NAMES:
            continue
        if called == "shutil.which":
            lines.add(node.lineno)
    return sorted(lines)


def _expression_string_tokens(
    index: TreeIndex, node: ast.AST, known: dict[str, set[str]]
) -> set[str]:
    """Collect casefolded path tokens from an expression subtree."""
    tokens: set[str] = set()
    for child in index.walk(node):
        value = _literal_string(child)
        if value is not None:
            tokens.update(part.casefold() for part in value.replace("\\", "/").split("/") if part)
        elif isinstance(child, ast.Name):
            tokens.update(known.get(child.id, set()))
    return tokens


def _assignment_string_tokens(index: TreeIndex) -> dict[str, set[str]]:
    """Propagate string path tokens across simple assignments (fixed point)."""
    return propagated_assignment_values(index, _expression_string_tokens)


def _is_path_construction(node: ast.AST) -> bool:
    """Return whether a node builds a filesystem path."""
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div)
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        called = node.func.attr
    elif isinstance(node.func, ast.Name):
        called = node.func.id
    else:
        return False
    return called in {"Path", "PurePath", "PurePosixPath", "PureWindowsPath", "join", "joinpath"}


def _venv_binary_fallback_lines(index: TreeIndex) -> list[int]:
    """Find ``.venv`` apm fallbacks built from path construction."""
    known = _assignment_string_tokens(index)
    lines: set[int] = set()
    for node in index.walk(index.root):
        if not _is_path_construction(node):
            continue
        if any(_is_path_construction(ancestor) for ancestor in _ancestors(node, index)):
            continue
        tokens = _expression_string_tokens(index, node, known)
        if ".venv" in tokens and tokens.intersection(_APM_EXECUTABLE_NAMES):
            lines.add(node.lineno)
    return sorted(lines)


def _python_sibling_binary_lines(index: TreeIndex) -> list[int]:
    """Find interpreter-relative apm selection (``sys.executable`` siblings)."""
    root = index.root
    sys_aliases: set[str] = set()
    for node in index.walk(root):
        if isinstance(node, ast.Import):
            sys_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "sys"
            )
    lines: set[int] = set()
    for node in index.walk(root):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "with_name":
            continue
        if _literal_string(node.args[0]) not in _APM_EXECUTABLE_NAMES:
            continue
        names = {
            _attribute_name(child)
            for child in index.walk(node.func.value)
            if isinstance(child, ast.Attribute)
        }
        if any(f"{alias}.executable" in names for alias in sys_aliases):
            lines.add(node.lineno)
    known = _assignment_string_tokens(index)
    for node in index.walk(root):
        if not isinstance(node, ast.BinOp):
            continue
        tokens = _expression_string_tokens(index, node, known)
        names = {
            _attribute_name(child) for child in index.walk(node) if isinstance(child, ast.Attribute)
        }
        if tokens.intersection(_APM_EXECUTABLE_NAMES) and any(
            name is not None and name.endswith(".executable") for name in names
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _local_binary_facade_lines(index: TreeIndex) -> list[int]:
    """Find local apm binary facades or fixtures outside the owner."""
    lines: set[int] = set()

    def returns_binary_value(function: ast.AST) -> bool:
        tainted = {"apm_binary_path"}
        for _ in range(len(function.body) + 1):
            changed = False
            for child in index.walk(function):
                if not isinstance(child, (ast.Assign, ast.AnnAssign)):
                    continue
                value = child.value
                if value is None or not any(
                    isinstance(node, ast.Name) and node.id in tainted for node in index.walk(value)
                ):
                    continue
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    for name in _bound_names(target):
                        if name not in tainted:
                            tainted.add(name)
                            changed = True
            if not changed:
                break
        return any(
            isinstance(child, ast.Return)
            and child.value is not None
            and any(
                isinstance(node, ast.Name) and node.id in tainted
                for node in index.walk(child.value)
            )
            for child in index.walk(function)
        )

    for node in index.walk(index.root):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in {*_LOCAL_BINARY_FACADES, "_resolve_apm_binary", "apm_binary_path"}:
            lines.add(node.lineno)
            continue
        fixture = any(
            (_attribute_name(decorator) or "").endswith(".fixture")
            or (
                isinstance(decorator, ast.Call)
                and (_attribute_name(decorator.func) or "").endswith(".fixture")
            )
            for decorator in node.decorator_list
        )
        parameter_names = {
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        }
        if fixture and "apm_binary_path" in parameter_names and returns_binary_value(node):
            lines.add(node.lineno)
    return sorted(lines)


def _is_subprocess_call(node: ast.Call, bindings: dict[str, str]) -> bool:
    """Return whether a call is a resolved ``subprocess.*`` invocation."""
    return _resolve_binding(node.func, bindings) in {
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }


def _list_literal_values(node: ast.AST) -> list[str | None]:
    """Return the string values of a list/tuple literal's elements."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [_literal_string(element) for element in node.elts]


def _assignment_command_values(
    index: TreeIndex, scope_by_node: dict[int, ast.AST]
) -> tuple[dict[int, dict[str, list[str | None]]], dict[int, dict[str, str]]]:
    """Propagate command list/string assignments per scope (fixed point)."""
    root = index.root
    assignments: dict[int, list[tuple[list[str], ast.AST]]] = {}
    for node in index.walk(root):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [name for target in targets for name in _bound_names(target)]
        scope = scope_by_node.get(id(node), root)
        assignments.setdefault(id(scope), []).append((names, node.value))

    lists_by_scope: dict[int, dict[str, list[str | None]]] = {}
    strings_by_scope: dict[int, dict[str, str]] = {}
    for scope_id, scoped_assignments in assignments.items():
        lists: dict[str, list[str | None]] = {}
        strings: dict[str, str] = {}
        for _ in range(len(scoped_assignments) + 1):
            changed = False
            for names, value in scoped_assignments:
                list_value = _list_literal_values(value)
                string_value = _literal_string(value)
                if isinstance(value, ast.Name):
                    list_value = lists.get(value.id, [])
                    string_value = strings.get(value.id)
                for name in names:
                    if list_value and lists.get(name) != list_value:
                        lists[name] = list_value
                        changed = True
                    elif string_value is not None and strings.get(name) != string_value:
                        strings[name] = string_value
                        changed = True
            if not changed:
                break
        lists_by_scope[scope_id] = lists
        strings_by_scope[scope_id] = strings
    return lists_by_scope, strings_by_scope


def _scope_parent_map(index: TreeIndex) -> dict[int, ast.AST]:
    """Return a ``function-scope -> enclosing-scope`` map."""
    scope_parents: dict[int, ast.AST] = {}
    for scope in (node for node in index.walk(index.root) if isinstance(node, FUNCTION_NODES)):
        parent = index.parent(scope)
        while parent is not None and not isinstance(
            parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            parent = index.parent(parent)
        if parent is not None:
            scope_parents[id(scope)] = parent
    return scope_parents


def _scoped_value(
    name: str,
    scope: ast.AST,
    values_by_scope: dict[int, dict[str, object]],
    scope_parents: dict[int, ast.AST],
) -> object | None:
    """Resolve a name through the enclosing-scope chain."""
    current: ast.AST | None = scope
    while current is not None:
        values = values_by_scope.get(id(current), {})
        if name in values:
            return values[name]
        current = scope_parents.get(id(current))
    return None


def _shell_enabled(call: ast.Call) -> bool:
    """Return whether a subprocess call passes ``shell=True``."""
    return any(
        keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )
