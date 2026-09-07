"""Restriction-only package-target-authorization install analyzer.

Ports ``install-deployment-package-target-authorization`` recorded in
``.apm/architecture/owners/install-deployment.json``. Kept in its own module
because its helper chain (parallel-target-read detection, effective-target
selector routing) is large enough to matter for the module-size budget on
its own.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _UNINSTALL_ENGINE,
    _lines,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.python_semantics import (
    NameAssignment,
    assignments_to,
    binding_nodes,
    direct_definitions,
    dotted_name,
    has_exclusive_import,
    is_statically_dead,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import Violation

_GUARD_PACKAGE_TARGET = "install-deployment-package-target-authorization"


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    """Return a simple dotted attribute chain, or an empty tuple."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ()
    parts.append(current.id)
    return tuple(reversed(parts))


def _attribute_name(node: ast.AST) -> str | None:
    """Return a dotted attribute name (Name.attr...), or None."""
    chain = _attribute_chain(node)
    return ".".join(chain) if chain else None


_TARGET_FILTER_OWNER = "src/apm_cli/install/target_filter.py"


_PACKAGE_SERVICES = "src/apm_cli/install/services.py"


_PACKAGE_HOOK = "src/apm_cli/integration/hook_integrator.py"


_TARGET_ATTRIBUTES = frozenset({"target", "targets", "canonical_targets"})


_PACKAGE_INFO_NAMES = frozenset({"package_info", "pkg_info"})


def _find_named_function(index: TreeIndex, name: str) -> ast.AST | None:
    """Return the unique function or method named `name` anywhere in the file."""
    functions = tuple(node for node in index.functions() if node.name == name)
    return functions[0] if len(functions) == 1 else None


def _package_aliases(index: TreeIndex) -> set[str]:
    """Find local names assigned from ``package_info.package`` (fixed point)."""
    if index.root is None:
        return set()
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in index.walk(index.root):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            chain = _attribute_chain(value)
            from_package_info = (
                len(chain) == 2 and chain[1] == "package" and chain[0] in _PACKAGE_INFO_NAMES
            )
            from_alias = isinstance(value, ast.Name) and value.id in aliases
            if not from_package_info and not from_alias:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _parallel_target_reads(provider: FactsProvider, path: str, rule_id: str) -> list[Violation]:
    """Port find_parallel_target_reads for one candidate file."""
    facts = provider.file_facts(path)
    if getattr(facts, "read_error", None) is not None or getattr(facts, "parse_error", None):
        return []
    index = provider.tree_index(path)
    if index is None or index.root is None:
        return []
    lines = _lines(facts)

    def exempt(lineno: int) -> bool:
        return 0 < lineno <= len(lines) and EXEMPT_MARKER in lines[lineno - 1]

    aliases = _package_aliases(index)
    findings: list[Violation] = []
    for node in index.walk(index.root):
        if exempt(getattr(node, "lineno", 0)):
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "canonical_package_targets"
        ):
            findings.append(
                violation(
                    rule_id,
                    path,
                    "canonical_package_targets call outside owner",
                    line=node.lineno,
                )
            )
        if not isinstance(node, ast.Attribute) or node.attr not in _TARGET_ATTRIBUTES:
            continue
        chain = _attribute_chain(node)
        direct = len(chain) == 3 and chain[0] in _PACKAGE_INFO_NAMES and chain[1] == "package"
        aliased = isinstance(node.value, ast.Name) and node.value.id in aliases
        if direct or aliased:
            detail = ".".join(chain) if chain else node.attr
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"parallel package target read: {detail}",
                    line=node.lineno,
                )
            )
    return findings


def _owner_is_complete(index: TreeIndex) -> bool:
    """Validate the one effective restriction-only target resolver body."""
    functions = direct_definitions(
        index,
        "resolve_effective_package_targets",
        kinds=FUNCTION_NODES,
    )
    if len(functions) != 1:
        return False
    function = functions[-1]
    if not has_exclusive_import(
        index,
        name="canonical_package_targets",
        module="apm_cli.models.apm_package",
        level=0,
    ) or binding_nodes(
        index,
        "canonical_package_targets",
        nodes=index.own_scope(function),
    ):
        return False

    declared = assignments_to(index, function, "declared_targets")
    effective = assignments_to(index, function, "effective_targets")
    if len(declared) != 1 or len(effective) != 1:
        return False
    if is_statically_dead(index, declared[0].node) or is_statically_dead(index, effective[0].node):
        return False

    declared_value = declared[0].value
    declared_is_canonical = (
        isinstance(declared_value, ast.Call)
        and dotted_name(declared_value.func) == "canonical_package_targets"
        and len(declared_value.args) == 1
        and isinstance(declared_value.args[0], ast.Name)
        and declared_value.args[0].id == "package"
        and not declared_value.keywords
    )
    if not declared_is_canonical or not _effective_target_expression(index, effective[0].value):
        return False

    returns = tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Return) and not is_statically_dead(index, node)
    )
    return len(returns) == 1 and _returns_effective_target_fields(returns[0].value)


def _effective_target_expression(index: TreeIndex, value: ast.AST | None) -> bool:
    """Require the direct package-restriction conditional, not a wrapper."""
    if (
        not isinstance(value, ast.IfExp)
        or not isinstance(value.test, ast.Name)
        or value.test.id != "package_restriction_active"
        or not isinstance(value.orelse, ast.Name)
        or value.orelse.id != "consumer_targets"
        or not isinstance(value.body, ast.Call)
        or dotted_name(value.body.func) != "tuple"
        or len(value.body.args) != 1
        or value.body.keywords
    ):
        return False
    body_names = {
        node.id
        for node in index.walk(value.body.args[0])
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return {"consumer_targets", "package_allowed", "target"} <= body_names


def _returns_effective_target_fields(value: ast.AST | None) -> bool:
    """Require the owner result to expose the two validated assignments."""
    if not isinstance(value, ast.Call) or dotted_name(value.func) != "EffectivePackageTargets":
        return False
    keywords = {keyword.arg: keyword.value for keyword in value.keywords if keyword.arg}
    return (
        isinstance(keywords.get("targets"), ast.Name)
        and keywords["targets"].id == "effective_targets"
        and isinstance(keywords.get("package_declared_targets"), ast.Name)
        and keywords["package_declared_targets"].id == "declared_targets"
    )


def _direct_selector_call(value: ast.AST | None) -> bool:
    """Return whether a binding directly calls the canonical target resolver."""
    return (
        isinstance(value, ast.Call)
        and dotted_name(value.func) == "resolve_effective_package_targets"
    )


def _resolver_import_is_canonical(
    index: TreeIndex,
    function: ast.AST,
    dispatch_kind: str,
) -> bool:
    """Require each consumer to bind the resolver from target_filter."""
    if dispatch_kind == "package":
        return has_exclusive_import(
            index,
            name="resolve_effective_package_targets",
            module="target_filter",
            level=1,
        ) and not binding_nodes(
            index,
            "resolve_effective_package_targets",
            nodes=index.own_scope(function),
        )
    module, level = (
        ("apm_cli.install.target_filter", 0)
        if dispatch_kind == "hook"
        else ("install.target_filter", 3)
    )
    return has_exclusive_import(
        index,
        name="resolve_effective_package_targets",
        module=module,
        level=level,
        scope=function,
    )


def _selector_targets_value(value: ast.AST | None) -> bool:
    """Recognize a direct selector target projection, optionally materialized."""
    if isinstance(value, ast.Attribute):
        return _attribute_chain(value) == ("target_selection", "targets")
    return (
        isinstance(value, ast.Call)
        and dotted_name(value.func) in {"list", "tuple"}
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Attribute)
        and _attribute_chain(value.args[0]) == ("target_selection", "targets")
    )


def _source_position(node: ast.AST) -> tuple[int, int]:
    """Return a stable source position for binding-order checks."""
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _binding_reaches_node(
    index: TreeIndex,
    function: ast.AST,
    *,
    name: str,
    assignment: ast.AST,
    use: ast.AST,
) -> bool:
    """Require `assignment` to remain the effective binding at `use`."""
    assignment_position = _source_position(assignment)
    use_position = _source_position(use)
    if assignment_position >= use_position:
        return False
    allowed_ids = {id(node) for node in index.walk(assignment)}
    return not any(
        assignment_position < _source_position(binding) < use_position
        and id(binding) not in allowed_ids
        and not is_statically_dead(index, binding)
        for binding in binding_nodes(
            index,
            name,
            nodes=index.own_scope(function),
        )
    )


def _target_position(target: ast.AST, name: str) -> int | None:
    """Return `name`'s direct position in a loop target."""
    if isinstance(target, ast.Name):
        return 0 if target.id == name else None
    if isinstance(target, (ast.Tuple, ast.List)):
        return next(
            (
                position
                for position, item in enumerate(target.elts)
                if isinstance(item, ast.Name) and item.id == name
            ),
            None,
        )
    return None


def _plan_binding_drives_selector_loop(
    index: TreeIndex,
    function: ast.AST,
    selection_assignment: ast.AST,
    selector_loop: ast.For | ast.AsyncFor,
) -> bool:
    """Prove the hook rebuild's collect-then-dispatch selector flow."""
    parent = index.parent(selector_loop)
    outer_loop: ast.For | ast.AsyncFor | None = None
    selection_position: int | None = None
    while parent is not None and parent is not function:
        if isinstance(parent, (ast.For, ast.AsyncFor)):
            position = _target_position(parent.target, "target_selection")
            if position is not None and isinstance(parent.iter, ast.Name):
                outer_loop = parent
                selection_position = position
                break
        parent = index.parent(parent)
    if outer_loop is None or selection_position is None:
        return False

    plan_name = outer_loop.iter.id
    plan_assignments = tuple(
        assignment
        for assignment in assignments_to(index, function, plan_name)
        if not is_statically_dead(index, assignment.node)
    )
    if len(plan_assignments) != 1 or not isinstance(plan_assignments[0].value, ast.List):
        return False
    if plan_assignments[0].value.elts:
        return False
    if not _binding_reaches_node(
        index,
        function,
        name=plan_name,
        assignment=plan_assignments[0].node,
        use=outer_loop,
    ):
        return False

    append_calls = tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Call)
        and dotted_name(node.func) == f"{plan_name}.append"
        and not is_statically_dead(index, node)
        and _source_position(node) < _source_position(outer_loop)
    )
    if len(append_calls) != 1:
        return False
    append_call = append_calls[0]
    if len(append_call.args) != 1 or append_call.keywords:
        return False
    payload = append_call.args[0]
    if (
        not isinstance(payload, (ast.Tuple, ast.List))
        or selection_position >= len(payload.elts)
        or not isinstance(payload.elts[selection_position], ast.Name)
        or payload.elts[selection_position].id != "target_selection"
    ):
        return False
    if not _binding_reaches_node(
        index,
        function,
        name="target_selection",
        assignment=selection_assignment,
        use=append_call,
    ):
        return False

    plan_calls = tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, ast.Call)
        and dotted_name(node.func).startswith(f"{plan_name}.")
        and not is_statically_dead(index, node)
        and _source_position(plan_assignments[0].node)
        < _source_position(node)
        < _source_position(outer_loop)
    )
    if plan_calls != append_calls:
        return False
    if _carrier_is_mutated(
        index,
        function,
        name=plan_name,
        assignment=plan_assignments[0].node,
        before=outer_loop,
        allowed_calls=append_calls,
    ):
        return False

    outer_position = _source_position(outer_loop)
    selector_position = _source_position(selector_loop)
    allowed_outer_bindings = {id(node) for node in index.walk(outer_loop.target)}
    return not any(
        outer_position < _source_position(binding) < selector_position
        and id(binding) not in allowed_outer_bindings
        and not is_statically_dead(index, binding)
        for binding in binding_nodes(
            index,
            "target_selection",
            nodes=index.own_scope(function),
        )
    )


def _live_calls_in(
    index: TreeIndex,
    function: ast.AST,
    scope: ast.AST,
) -> tuple[ast.Call, ...]:
    """Return executable calls in ``scope`` that belong to ``function``."""
    return tuple(
        node
        for node in index.walk(scope)
        if isinstance(node, ast.Call)
        and index.definition_anchor(node) is function
        and not is_statically_dead(index, node)
    )


def _is_dispatch_call(call: ast.Call, dispatch_kind: str) -> bool:
    """Recognize one consumer-specific integration sink."""
    if dispatch_kind == "package":
        return (
            isinstance(call.func, ast.Call)
            and dotted_name(call.func.func) == "getattr"
            and len(call.func.args) == 2
            and not call.func.keywords
            and isinstance(call.func.args[0], ast.Name)
            and call.func.args[0].id == "_integrator"
            and isinstance(call.func.args[1], ast.Attribute)
            and _attribute_chain(call.func.args[1]) == ("_entry", "integrate_method")
        )
    if dispatch_kind == "hook":
        return dotted_name(call.func) == "self.integrate_hooks_for_target"
    return dotted_name(call.func) == "integrate_package_primitives"


def _live_dispatch_calls(
    index: TreeIndex,
    function: ast.AST,
    dispatch_kind: str,
) -> tuple[ast.Call, ...]:
    """Return every executable real sink in the guarded consumer."""
    return tuple(
        call
        for call in _live_calls_in(index, function, function)
        if _is_dispatch_call(call, dispatch_kind)
    )


def _loop_dispatches_target(
    index: TreeIndex,
    function: ast.AST,
    loop: ast.For | ast.AsyncFor,
    dispatch_kind: str,
    dispatch_call: ast.Call,
) -> bool:
    """Require the selector-driven loop variable to reach the sole real sink."""
    if (
        not isinstance(loop.target, ast.Name)
        or dispatch_call not in _live_calls_in(index, function, loop)
        or not dispatch_call.args
        or not isinstance(dispatch_call.args[0], ast.Name)
    ):
        return False
    return dispatch_call.args[0].id == loop.target.id


_MUTATING_METHODS = frozenset(
    {
        "__iadd__",
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "remove",
        "reverse",
        "sort",
        "update",
    }
)


def _carrier_is_mutated(
    index: TreeIndex,
    function: ast.AST,
    *,
    name: str,
    assignment: ast.AST,
    before: ast.AST,
    allowed_calls: tuple[ast.Call, ...] = (),
) -> bool:
    """Detect in-place mutation of a selector-derived carrier before use."""
    start = _source_position(assignment)
    stop = _source_position(before)
    allowed_ids = {id(call) for call in allowed_calls}
    for node in index.own_scope(function):
        position = _source_position(node)
        if not start < position < stop or is_statically_dead(index, node):
            continue
        if isinstance(node, ast.Call):
            callee = dotted_name(node.func)
            chain = _attribute_chain(node.func)
            if (
                len(chain) >= 2
                and chain[0] == name
                and chain[-1] in _MUTATING_METHODS
                and id(node) not in allowed_ids
            ):
                return True
            if (
                callee in {"setattr", "object.__setattr__"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == name
            ):
                return True
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name) and root.id == name:
                return True
    return False


def _single_live_empty_list_assignment(
    index: TreeIndex,
    function: ast.AST,
    name: str,
) -> NameAssignment | None:
    """Return the one live ``name = []`` binding, if it is exclusive."""
    assignments = tuple(
        assignment
        for assignment in assignments_to(index, function, name)
        if not is_statically_dead(index, assignment.node)
    )
    if len(assignments) != 1:
        return None
    value = assignments[0].value
    return assignments[0] if isinstance(value, ast.List) and not value.elts else None


def _uninstall_plan_dispatches_targets(
    index: TreeIndex,
    function: ast.AST,
    *,
    plan_assignment: NameAssignment,
    plan_append: ast.Call,
    target_list_position: int,
    dispatch_call: ast.Call,
) -> bool:
    """Require the uninstall survivor plan field to feed package integration."""
    plan_name = "target_survivor_plan"
    dispatch_loops = tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, (ast.For, ast.AsyncFor))
        and isinstance(node.iter, ast.Name)
        and node.iter.id == plan_name
        and _source_position(plan_append) < _source_position(node)
        and not is_statically_dead(index, node)
    )
    if len(dispatch_loops) != 1:
        return False
    dispatch_loop = dispatch_loops[0]
    if (
        not isinstance(dispatch_loop.target, (ast.Tuple, ast.List))
        or target_list_position >= len(dispatch_loop.target.elts)
        or not isinstance(dispatch_loop.target.elts[target_list_position], ast.Name)
    ):
        return False
    dispatch_name = dispatch_loop.target.elts[target_list_position].id
    if not _binding_reaches_node(
        index,
        function,
        name=plan_name,
        assignment=plan_assignment.node,
        use=dispatch_loop,
    ):
        return False

    if dispatch_call not in _live_calls_in(index, function, dispatch_loop):
        return False
    keywords = {keyword.arg: keyword.value for keyword in dispatch_call.keywords if keyword.arg}
    target_value = keywords.get("targets")
    if not isinstance(target_value, ast.Name) or target_value.id != dispatch_name:
        return False
    allowed_dispatch_binding_ids = {id(node) for node in index.walk(dispatch_loop.target)}
    return not any(
        _source_position(dispatch_loop)
        < _source_position(binding)
        < _source_position(dispatch_call)
        and id(binding) not in allowed_dispatch_binding_ids
        and not is_statically_dead(index, binding)
        for binding in binding_nodes(
            index,
            dispatch_name,
            nodes=index.own_scope(function),
        )
    )


def _uninstall_collection_drives_dispatch(
    index: TreeIndex,
    function: ast.AST,
    selection_assignment: ast.AST,
    selector_loop: ast.For | ast.AsyncFor,
    dispatch_call: ast.Call,
) -> bool:
    """Prove selected uninstall targets flow through its plan into integration."""
    if not isinstance(selector_loop.target, ast.Name) or not _binding_reaches_node(
        index,
        function,
        name="target_selection",
        assignment=selection_assignment,
        use=selector_loop,
    ):
        return False
    target_name = selector_loop.target.id
    target_list_name = "authorized_targets"
    target_list_assignment = _single_live_empty_list_assignment(
        index,
        function,
        target_list_name,
    )
    if target_list_assignment is None:
        return False
    target_appends = tuple(
        call
        for call in _live_calls_in(index, function, selector_loop)
        if dotted_name(call.func) == f"{target_list_name}.append"
    )
    if (
        len(target_appends) != 1
        or len(target_appends[0].args) != 1
        or target_appends[0].keywords
        or not isinstance(target_appends[0].args[0], ast.Name)
        or target_appends[0].args[0].id != target_name
        or not _binding_reaches_node(
            index,
            function,
            name=target_list_name,
            assignment=target_list_assignment.node,
            use=target_appends[0],
        )
    ):
        return False
    target_list_calls = tuple(
        call
        for call in index.own_scope(function)
        if isinstance(call, ast.Call)
        and dotted_name(call.func).startswith(f"{target_list_name}.")
        and not is_statically_dead(index, call)
        and _source_position(target_list_assignment.node)
        < _source_position(call)
        < _source_position(dispatch_call)
    )
    if target_list_calls != target_appends:
        return False
    if _carrier_is_mutated(
        index,
        function,
        name=target_list_name,
        assignment=target_list_assignment.node,
        before=dispatch_call,
        allowed_calls=target_appends,
    ):
        return False

    plan_name = "target_survivor_plan"
    plan_assignment = _single_live_empty_list_assignment(index, function, plan_name)
    if plan_assignment is None:
        return False
    plan_appends = tuple(
        call
        for call in index.own_scope(function)
        if isinstance(call, ast.Call)
        and dotted_name(call.func) == f"{plan_name}.append"
        and not is_statically_dead(index, call)
    )
    if len(plan_appends) != 1 or len(plan_appends[0].args) != 1 or plan_appends[0].keywords:
        return False
    plan_calls = tuple(
        call
        for call in index.own_scope(function)
        if isinstance(call, ast.Call)
        and dotted_name(call.func).startswith(f"{plan_name}.")
        and not is_statically_dead(index, call)
        and _source_position(plan_assignment.node)
        < _source_position(call)
        < _source_position(dispatch_call)
    )
    if plan_calls != plan_appends:
        return False
    if _carrier_is_mutated(
        index,
        function,
        name=plan_name,
        assignment=plan_assignment.node,
        before=dispatch_call,
        allowed_calls=plan_appends,
    ):
        return False
    payload = plan_appends[0].args[0]
    if not isinstance(payload, (ast.Tuple, ast.List)):
        return False
    target_list_positions = tuple(
        position
        for position, element in enumerate(payload.elts)
        if isinstance(element, ast.Name) and element.id == target_list_name
    )
    if len(target_list_positions) != 1 or not _binding_reaches_node(
        index,
        function,
        name=target_list_name,
        assignment=target_list_assignment.node,
        use=plan_appends[0],
    ):
        return False
    target_list_position = target_list_positions[0]
    return _uninstall_plan_dispatches_targets(
        index,
        function,
        plan_assignment=plan_assignment,
        plan_append=plan_appends[0],
        target_list_position=target_list_position,
        dispatch_call=dispatch_call,
    )


def _consumer_routes_through_selector(
    index: TreeIndex,
    function_name: str,
    dispatch_kind: str,
) -> bool:
    """Return whether live own-scope selector output drives a dispatch loop."""
    function = _find_named_function(index, function_name)
    if function is None or not _resolver_import_is_canonical(index, function, dispatch_kind):
        return False
    selection_assignments = tuple(
        assignment
        for assignment in assignments_to(index, function, "target_selection")
        if not is_statically_dead(index, assignment.node)
    )
    if len(selection_assignments) != 1 or not _direct_selector_call(selection_assignments[0].value):
        return False
    selection_assignment = selection_assignments[0].node
    dispatch_calls = _live_dispatch_calls(index, function, dispatch_kind)
    if len(dispatch_calls) != 1:
        return False
    dispatch_call = dispatch_calls[0]
    if _carrier_is_mutated(
        index,
        function,
        name="target_selection",
        assignment=selection_assignment,
        before=dispatch_call,
    ):
        return False

    loops = tuple(
        node
        for node in index.own_scope(function)
        if isinstance(node, (ast.For, ast.AsyncFor)) and not is_statically_dead(index, node)
    )
    for loop in loops:
        direct_selection = (
            isinstance(loop.iter, ast.Attribute)
            and _attribute_chain(loop.iter) == ("target_selection", "targets")
            and (
                _binding_reaches_node(
                    index,
                    function,
                    name="target_selection",
                    assignment=selection_assignment,
                    use=loop,
                )
                or _plan_binding_drives_selector_loop(
                    index,
                    function,
                    selection_assignment,
                    loop,
                )
            )
        )
        if direct_selection:
            if dispatch_kind == "uninstall":
                if _uninstall_collection_drives_dispatch(
                    index,
                    function,
                    selection_assignment,
                    loop,
                    dispatch_call,
                ):
                    return True
            elif _loop_dispatches_target(
                index,
                function,
                loop,
                dispatch_kind,
                dispatch_call,
            ):
                return True
        if not isinstance(loop.iter, ast.Name):
            continue
        alias = loop.iter.id
        alias_assignments = tuple(
            assignment
            for assignment in assignments_to(index, function, alias)
            if not is_statically_dead(index, assignment.node)
        )
        if (
            len(alias_assignments) == 1
            and _selector_targets_value(alias_assignments[0].value)
            and _binding_reaches_node(
                index,
                function,
                name="target_selection",
                assignment=selection_assignment,
                use=loop,
            )
            and _binding_reaches_node(
                index,
                function,
                name=alias,
                assignment=alias_assignments[0].node,
                use=loop,
            )
            and not _carrier_is_mutated(
                index,
                function,
                name=alias,
                assignment=alias_assignments[0].node,
                before=dispatch_call,
            )
            and _loop_dispatches_target(
                index,
                function,
                loop,
                dispatch_kind,
                dispatch_call,
            )
        ):
            return True
    return False


def check_package_target_authorization(provider: FactsProvider) -> tuple[Violation, ...]:
    """Restriction-only package target authorization must have one owner."""
    rule_id = _GUARD_PACKAGE_TARGET
    required = (_TARGET_FILTER_OWNER, _PACKAGE_SERVICES, _PACKAGE_HOOK, _UNINSTALL_ENGINE)
    missing = [path for path in required if not provider.file_facts(path).exists]
    if missing:
        return tuple(
            _summary(rule_id, _TARGET_FILTER_OWNER, f"missing required path: {path}")
            for path in missing
        )

    findings: list[Violation] = []
    owner_index = provider.tree_index(_TARGET_FILTER_OWNER)
    if owner_index is None or not _owner_is_complete(owner_index):
        findings.append(
            _summary(
                rule_id, _TARGET_FILTER_OWNER, "incomplete resolve_effective_package_targets owner"
            )
        )
    for consumer_path, function_name, dispatch_kind, message in (
        (
            _PACKAGE_SERVICES,
            "integrate_package_primitives",
            "package",
            "selector result does not drive dispatch",
        ),
        (
            _PACKAGE_HOOK,
            "reconcile_after_removal",
            "hook",
            "selector result does not drive hook rebuild",
        ),
        (
            _UNINSTALL_ENGINE,
            "_sync_integrations_after_uninstall",
            "uninstall",
            "selector result does not drive survivor rebuild",
        ),
    ):
        consumer_index = provider.tree_index(consumer_path)
        if consumer_index is None or not _consumer_routes_through_selector(
            consumer_index, function_name, dispatch_kind
        ):
            findings.append(_summary(rule_id, consumer_path, message))

    candidates = list(_python_paths(provider, "src/apm_cli/install/"))
    candidates.extend(_python_paths(provider, "src/apm_cli/integration/"))
    if _UNINSTALL_ENGINE not in candidates:
        candidates.append(_UNINSTALL_ENGINE)
    for path in candidates:
        if path == _TARGET_FILTER_OWNER:
            continue
        findings.extend(_parallel_target_reads(provider, path, rule_id))
    return tuple(findings)
