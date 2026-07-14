#!/usr/bin/env python3
"""Guard canonical owners for integration binaries and rendered CLI parity."""

from __future__ import annotations

import argparse
import ast
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY_OWNER = Path("tests/integration/conftest.py")
PARITY_OWNER = Path("scripts/check_cli_docs.py")
PARITY_FACADE = "registry_docs_mismatches"
PARITY_INTERNALS = {
    "public_top_level_commands",
    "rendered_cli_reference_pages",
}
PARITY_OWNER_FUNCTIONS = {
    *PARITY_INTERNALS,
    PARITY_FACADE,
}
APM_EXECUTABLE_NAMES = {"apm", "apm.cmd", "apm.exe"}
LOCAL_BINARY_FACADES = {
    "_resolve_apm_executable",
    "apm_binary",
    "apm_command",
}


def _python_files(root: Path, locations: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for location in locations:
        base = root / location
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(base.rglob("*.py"))
    return sorted(path for path in files if path.is_file())


def _attribute_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for element in node.elts for name in _bound_names(element)}
    return set()


def _resolve_binding(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_binding(node.value, bindings)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _scope_nodes(body: list[ast.stmt]) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes.append(node)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes


def _scope_binding_maps(
    tree: ast.Module,
) -> tuple[dict[int, dict[str, str]], dict[int, ast.AST]]:
    bindings_by_scope: dict[int, dict[str, str]] = {}
    scope_by_node: dict[int, ast.AST] = {}

    def map_scope(scope: ast.AST, inherited: dict[str, str]) -> None:
        body = (
            scope.body
            if isinstance(scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))
            else []
        )
        scope_nodes = _scope_nodes(body)
        bindings = dict(inherited)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
            for name in local_names:
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
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                for target in targets:
                    for name in _bound_names(target):
                        if resolved is None:
                            bindings.pop(name, None)
                        elif bindings.get(name) != resolved:
                            bindings[name] = resolved
                            changed = True
            if not changed:
                break

        bindings_by_scope[id(scope)] = bindings

        def mark(node: ast.AST) -> None:
            scope_by_node[id(node)] = scope
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    map_scope(child, bindings)
                elif isinstance(child, ast.ClassDef):
                    for nested in child.body:
                        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            map_scope(nested, bindings)
                else:
                    mark(child)

        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                map_scope(statement, bindings)
            elif isinstance(statement, ast.ClassDef):
                for nested in statement.body:
                    if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        map_scope(nested, bindings)
            else:
                mark(statement)

    map_scope(tree, {})
    return bindings_by_scope, scope_by_node


def _direct_binary_env_read_lines(tree: ast.AST) -> list[int]:
    bindings_by_scope, scope_by_node = _scope_binding_maps(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        scope = scope_by_node.get(id(node), tree)
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


def _direct_binary_path_lookup_lines(tree: ast.AST) -> list[int]:
    bindings_by_scope, scope_by_node = _scope_binding_maps(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        scope = scope_by_node.get(id(node), tree)
        called = _resolve_binding(
            node.func,
            bindings_by_scope.get(id(scope), {}),
        )
        if _literal_string(node.args[0]) not in APM_EXECUTABLE_NAMES:
            continue
        if called == "shutil.which":
            lines.add(node.lineno)
    return sorted(lines)


def _assignment_string_tokens(tree: ast.AST) -> dict[str, set[str]]:
    assignments: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if names:
            assignments.append((names, node.value))

    known: dict[str, set[str]] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for names, value in assignments:
            tokens = _expression_string_tokens(value, known)
            for name in names:
                if not tokens.issubset(known.get(name, set())):
                    known.setdefault(name, set()).update(tokens)
                    changed = True
        if not changed:
            break
    return known


def _expression_string_tokens(
    node: ast.AST,
    known: dict[str, set[str]],
) -> set[str]:
    tokens: set[str] = set()
    for child in ast.walk(node):
        value = _literal_string(child)
        if value is not None:
            tokens.update(part.casefold() for part in value.replace("\\", "/").split("/") if part)
        elif isinstance(child, ast.Name):
            tokens.update(known.get(child.id, set()))
    return tokens


def _venv_binary_fallback_lines(tree: ast.AST) -> list[int]:
    known = _assignment_string_tokens(tree)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def is_path_construction(node: ast.AST) -> bool:
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
        return called in {
            "Path",
            "PurePath",
            "PurePosixPath",
            "PureWindowsPath",
            "join",
            "joinpath",
        }

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not is_path_construction(node):
            continue
        if any(is_path_construction(ancestor) for ancestor in _ancestors(node, parents)):
            continue
        tokens = _expression_string_tokens(node, known)
        if ".venv" in tokens and tokens.intersection(APM_EXECUTABLE_NAMES):
            lines.add(node.lineno)
    return sorted(lines)


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    ancestors: list[ast.AST] = []
    while node in parents:
        node = parents[node]
        ancestors.append(node)
    return ancestors


def _python_sibling_binary_lines(tree: ast.AST) -> list[int]:
    sys_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sys_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "sys"
            )

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "with_name":
            continue
        if _literal_string(node.args[0]) not in APM_EXECUTABLE_NAMES:
            continue
        names = {
            _attribute_name(child)
            for child in ast.walk(node.func.value)
            if isinstance(child, ast.Attribute)
        }
        if any(f"{alias}.executable" in names for alias in sys_aliases):
            lines.add(node.lineno)
    known = _assignment_string_tokens(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        tokens = _expression_string_tokens(node, known)
        names = {
            _attribute_name(child) for child in ast.walk(node) if isinstance(child, ast.Attribute)
        }
        if tokens.intersection(APM_EXECUTABLE_NAMES) and any(
            name is not None and name.endswith(".executable") for name in names
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _local_binary_facade_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()

    def returns_binary_value(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        tainted = {"apm_binary_path"}
        for _ in range(len(function.body) + 1):
            changed = False
            for child in ast.walk(function):
                if not isinstance(child, (ast.Assign, ast.AnnAssign)):
                    continue
                value = child.value
                if value is None or not any(
                    isinstance(node, ast.Name) and node.id in tainted for node in ast.walk(value)
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
                isinstance(node, ast.Name) and node.id in tainted for node in ast.walk(child.value)
            )
            for child in ast.walk(function)
        )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in {*LOCAL_BINARY_FACADES, "_resolve_apm_binary", "apm_binary_path"}:
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


def _is_subprocess_call(
    node: ast.Call,
    bindings: dict[str, str],
) -> bool:
    return _resolve_binding(node.func, bindings) in {
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }


def _list_literal_values(node: ast.AST) -> list[str | None]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [_literal_string(element) for element in node.elts]


def _assignment_command_values(
    tree: ast.AST,
    scope_by_node: dict[int, ast.AST],
) -> tuple[
    dict[int, dict[str, list[str | None]]],
    dict[int, dict[str, str]],
]:
    assignments: dict[int, list[tuple[list[str], ast.AST]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [name for target in targets for name in _bound_names(target)]
        scope = scope_by_node.get(id(node), tree)
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


def _scope_parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    scope_parents: dict[int, ast.AST] = {}
    for scope in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        parent = parents.get(scope)
        while parent is not None and not isinstance(
            parent,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            parent = parents.get(parent)
        if parent is not None:
            scope_parents[id(scope)] = parent
    return scope_parents


def _scoped_value(
    name: str,
    scope: ast.AST,
    values_by_scope: dict[int, dict[str, object]],
    scope_parents: dict[int, ast.AST],
) -> object | None:
    current: ast.AST | None = scope
    while current is not None:
        values = values_by_scope.get(id(current), {})
        if name in values:
            return values[name]
        current = scope_parents.get(id(current))
    return None


def _shell_enabled(call: ast.Call) -> bool:
    return any(
        keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def _direct_apm_subprocess_lines(tree: ast.AST) -> list[int]:
    bindings_by_scope, scope_by_node = _scope_binding_maps(tree)
    scope_parents = _scope_parent_map(tree)
    lists_by_scope, strings_by_scope = _assignment_command_values(
        tree,
        scope_by_node,
    )
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = _list_literal_values(node)
            attributes = {
                _attribute_name(child)
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
            }
            runs_python_module = (
                len(values) >= 3
                and values[1] == "-m"
                and values[2] in {"apm_cli", "apm_cli.cli"}
                and any(
                    attribute is not None and attribute.endswith(".executable")
                    for attribute in attributes
                )
            )
            runs_uv_apm = (
                len(values) >= 5
                and values[1:5] == ["-m", "uv", "run", "apm"]
                and any(
                    attribute is not None and attribute.endswith(".executable")
                    for attribute in attributes
                )
            )
            runs_bare_uv_apm = len(values) >= 3 and values[:3] == [
                "uv",
                "run",
                "apm",
            ]
            if runs_python_module or runs_uv_apm or runs_bare_uv_apm:
                lines.add(node.lineno)
        if not isinstance(node, ast.Call) or not node.args:
            continue
        scope = scope_by_node.get(id(node), tree)
        if not _is_subprocess_call(
            node,
            bindings_by_scope.get(id(scope), {}),
        ):
            continue
        command = node.args[0]
        values = _list_literal_values(command)
        if not values and isinstance(command, ast.Name):
            inherited_list = _scoped_value(
                command.id,
                scope,
                lists_by_scope,
                scope_parents,
            )
            values = inherited_list if isinstance(inherited_list, list) else []
        if values and values[0] in APM_EXECUTABLE_NAMES:
            lines.add(node.lineno)
        if _shell_enabled(node):
            shell_command = _literal_string(command)
            if shell_command is None and isinstance(command, ast.Name):
                inherited_string = _scoped_value(
                    command.id,
                    scope,
                    strings_by_scope,
                    scope_parents,
                )
                if isinstance(inherited_string, str):
                    shell_command = inherited_string
            if shell_command is not None:
                try:
                    shell_tokens = shlex.split(shell_command, posix=True)
                except ValueError:
                    shell_tokens = []
                if shell_tokens and shell_tokens[0] in APM_EXECUTABLE_NAMES:
                    lines.add(node.lineno)
        noncanonical_names = {
            child.id
            for child in ast.walk(command)
            if isinstance(child, ast.Name)
            and child.id
            in {
                "apm_bin",
                "apm_binary",
                "apm_command",
                "apm_executable",
                "apm_path",
            }
        }
        if noncanonical_names:
            lines.add(node.lineno)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings = {
            value for child in ast.walk(function) if (value := _literal_string(child)) is not None
        }
        runs_subprocess = any(
            isinstance(child, ast.Call)
            and _is_subprocess_call(
                child,
                bindings_by_scope.get(
                    id(scope_by_node.get(id(child), tree)),
                    {},
                ),
            )
            for child in ast.walk(function)
        )
        if runs_subprocess and {"apm", "./apm", "./dist/apm"}.issubset(strings):
            lines.add(function.lineno)
    return sorted(lines)


def _defined_functions(tree: ast.AST) -> set[str]:
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parse(path: Path, root: Path) -> tuple[ast.Module | None, str | None]:
    try:
        return ast.parse(path.read_text(encoding="utf-8")), None
    except (OSError, SyntaxError) as error:
        return None, f"[x] cannot inspect {path.relative_to(root)}: {error}"


def find_binary_selection_violations(root: Path) -> list[str]:
    """Reject every direct integration-test read outside the canonical owner."""
    diagnostics: list[str] = []
    owner = root / BINARY_OWNER
    owner_tree, owner_error = _parse(owner, root)
    if owner_error is not None:
        diagnostics.append(owner_error)
    elif owner_tree is None or "_resolve_apm_binary" not in _defined_functions(owner_tree):
        diagnostics.append(f"[x] {BINARY_OWNER} must define _resolve_apm_binary")

    integration_root = root / "tests" / "integration"
    for path in _python_files(root, ("tests/integration",)):
        if path == owner:
            continue
        tree, error = _parse(path, root)
        if error is not None:
            diagnostics.append(error)
            continue
        if tree is None:
            continue
        relative = path.relative_to(root).as_posix()
        env_read_lines = _direct_binary_env_read_lines(tree)
        for line in env_read_lines:
            diagnostics.append(
                f"[x] direct APM_BINARY_PATH read outside {BINARY_OWNER}: "
                f"{relative}:{line}; consume the apm_binary_path fixture"
            )
        for line in _direct_binary_path_lookup_lines(tree):
            diagnostics.append(
                f"[x] direct PATH lookup for apm outside {BINARY_OWNER}: "
                f"{relative}:{line}; consume the apm_binary_path fixture"
            )
        for line in _venv_binary_fallback_lines(tree):
            diagnostics.append(
                f"[x] direct .venv apm fallback outside {BINARY_OWNER}: "
                f"{relative}:{line}; consume the apm_binary_path fixture"
            )
        for line in _python_sibling_binary_lines(tree):
            diagnostics.append(
                f"[x] interpreter-relative apm selection outside {BINARY_OWNER}: "
                f"{relative}:{line}; consume the apm_binary_path fixture"
            )
        for line in _local_binary_facade_lines(tree):
            diagnostics.append(
                f"[x] local apm binary fixture or facade outside {BINARY_OWNER}: "
                f"{relative}:{line}; inject apm_binary_path directly"
            )
        for line in _direct_apm_subprocess_lines(tree):
            diagnostics.append(
                f"[x] direct apm subprocess selection outside {BINARY_OWNER}: "
                f"{relative}:{line}; inject apm_binary_path directly"
            )
        if path.parent == integration_root and "_resolve_apm_binary" in _defined_functions(tree):
            diagnostics.append(
                f"[x] duplicate _resolve_apm_binary definition: {relative}; owner is {BINARY_OWNER}"
            )
    return sorted(diagnostics)


def _parity_import_violations(tree: ast.AST, relative: str) -> list[str]:
    diagnostics: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "scripts.check_cli_docs":
            for alias in node.names:
                if alias.name in PARITY_INTERNALS:
                    diagnostics.append(
                        f"[x] internal rendered parity projection imported: "
                        f"{relative}:{node.lineno} {alias.name}; "
                        f"consume {PARITY_FACADE}"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
            for alias in node.names:
                if alias.name == "check_cli_docs":
                    diagnostics.append(
                        f"[x] rendered parity module imported directly: "
                        f"{relative}:{node.lineno}; import {PARITY_FACADE} only"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scripts.check_cli_docs":
                    diagnostics.append(
                        f"[x] rendered parity module imported directly: "
                        f"{relative}:{node.lineno}; import {PARITY_FACADE} only"
                    )
    return diagnostics


def _is_commands_items(call: ast.Call) -> bool:
    called = _attribute_name(call.func)
    return called is not None and called.endswith(".commands.items")


def _registry_projection_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp),
        ):
            continue
        projects_commands = any(
            isinstance(generator.iter, ast.Call) and _is_commands_items(generator.iter)
            for generator in node.generators
        )
        filters_hidden = any(
            isinstance(child, ast.Attribute) and child.attr == "hidden" for child in ast.walk(node)
        )
        if projects_commands and filters_hidden:
            lines.add(node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        projects_commands = isinstance(node.iter, ast.Call) and _is_commands_items(node.iter)
        filters_hidden = any(
            isinstance(child, ast.Attribute) and child.attr == "hidden" for child in ast.walk(node)
        )
        collects_names = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"add", "append", "update"}
            for child in ast.walk(node)
        )
        if projects_commands and filters_hidden and collects_names:
            lines.add(node.lineno)
    return sorted(lines)


def _path_string_segments(node: ast.AST) -> set[str]:
    return {value for child in ast.walk(node) if (value := _literal_string(child)) is not None}


def _path_segments(node: ast.AST, known: dict[str, set[str]]) -> set[str]:
    segments = _path_string_segments(node)
    segments.update(
        segment
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
        for segment in known.get(child.id, set())
    )
    return segments


def _rendered_cli_path_names(tree: ast.AST) -> set[str]:
    assignments: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if names:
            assignments.append((names, node.value))

    known: dict[str, set[str]] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for names, value in assignments:
            segments = _path_segments(value, known)
            for name in names:
                if not segments.issubset(known.get(name, set())):
                    known.setdefault(name, set()).update(segments)
                    changed = True
        if not changed:
            break
    return {name for name, segments in known.items() if {"reference", "cli"}.issubset(segments)}


def _rendered_inventory_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    rendered_path_names = _rendered_cli_path_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "glob",
            "iterdir",
            "rglob",
        }:
            continue
        parent = node.func.value
        if {"reference", "cli"}.issubset(_path_string_segments(parent)) or (
            isinstance(parent, ast.Name) and parent.id in rendered_path_names
        ):
            lines.add(node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if "index.html" not in _path_string_segments(node):
            continue
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"is_file", "exists"}
            for child in ast.walk(node)
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _owner_definition_violations(root: Path) -> list[str]:
    owner = root / PARITY_OWNER
    tree, error = _parse(owner, root)
    if error is not None:
        return [error]
    if tree is None:
        return [f"[x] cannot inspect {PARITY_OWNER}"]
    missing = sorted(PARITY_OWNER_FUNCTIONS - _defined_functions(tree))
    return [
        f"[x] {PARITY_OWNER} must define rendered parity owner function: {name}" for name in missing
    ]


def find_rendered_parity_violations(root: Path) -> list[str]:
    """Enforce facade-only consumers and unique registry/page projections."""
    diagnostics = _owner_definition_violations(root)
    owner = root / PARITY_OWNER
    for path in _python_files(root, ("src/apm_cli", "scripts", "tests")):
        if path == owner:
            continue
        tree, error = _parse(path, root)
        if error is not None:
            diagnostics.append(error)
            continue
        if tree is None:
            continue
        relative = path.relative_to(root).as_posix()
        diagnostics.extend(_parity_import_violations(tree, relative))
        for line in _registry_projection_lines(tree):
            diagnostics.append(
                f"[x] direct Click command registry projection: {relative}:{line}; "
                f"consume {PARITY_FACADE}"
            )
        for line in _rendered_inventory_lines(tree):
            diagnostics.append(
                f"[x] direct rendered CLI route inventory: {relative}:{line}; "
                f"consume {PARITY_FACADE}"
            )
    return sorted(set(diagnostics))


def check(root: Path) -> list[str]:
    """Return all canonical-owner violations for the repository."""
    return [
        *find_binary_selection_violations(root),
        *find_rendered_parity_violations(root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    diagnostics = check(args.root.resolve())
    for diagnostic in diagnostics:
        print(diagnostic)
    if diagnostics:
        print(f"[x] {len(diagnostics)} test contract authority violation(s) found")
        return 1
    print("[+] test contract authority check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
