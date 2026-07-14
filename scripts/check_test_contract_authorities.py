#!/usr/bin/env python3
"""Guard canonical owners for integration binaries and rendered CLI parity."""

from __future__ import annotations

import argparse
import ast
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


def _direct_binary_env_read_lines(tree: ast.AST) -> list[int]:
    os_aliases = {"os"}
    environ_aliases: set[str] = set()
    getenv_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "os"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name == "environ":
                    environ_aliases.add(local_name)
                elif alias.name == "getenv":
                    getenv_aliases.add(local_name)

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            called = _attribute_name(node.func)
            reads_variable = _literal_string(node.args[0]) == "APM_BINARY_PATH"
            if reads_variable and (
                called in getenv_aliases
                or any(
                    called in {f"{alias}.getenv", f"{alias}.environ.get"} for alias in os_aliases
                )
                or any(called == f"{alias}.get" for alias in environ_aliases)
            ):
                lines.add(node.lineno)
        elif isinstance(node, ast.Subscript) and _literal_string(node.slice) == "APM_BINARY_PATH":
            target = _attribute_name(node.value)
            if target in environ_aliases or any(
                target == f"{alias}.environ" for alias in os_aliases
            ):
                lines.add(node.lineno)
    return sorted(lines)


def _direct_binary_path_lookup_lines(tree: ast.AST) -> list[int]:
    shutil_aliases: set[str] = set()
    which_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            shutil_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "shutil"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            which_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "which"
            )

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        called = _attribute_name(node.func)
        if _literal_string(node.args[0]) not in APM_EXECUTABLE_NAMES:
            continue
        if called in which_aliases or any(called == f"{alias}.which" for alias in shutil_aliases):
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
    return sorted(lines)


def _local_binary_facade_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in LOCAL_BINARY_FACADES:
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
        executable_body = [
            statement
            for statement in node.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        forwards_fixture = (
            len(executable_body) == 1
            and isinstance(executable_body[0], ast.Return)
            and executable_body[0].value is not None
            and any(
                isinstance(child, ast.Name) and child.id == "apm_binary_path"
                for child in ast.walk(executable_body[0].value)
            )
        )
        if fixture and "apm_binary_path" in parameter_names and forwards_fixture:
            lines.add(node.lineno)
    return sorted(lines)


def _subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    module_aliases: set[str] = set()
    call_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            call_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"Popen", "call", "check_call", "check_output", "run"}
            )
    return module_aliases, call_aliases


def _is_subprocess_call(
    node: ast.Call,
    module_aliases: set[str],
    call_aliases: set[str],
) -> bool:
    called = _attribute_name(node.func)
    return called in call_aliases or any(
        called
        in {
            f"{alias}.Popen",
            f"{alias}.call",
            f"{alias}.check_call",
            f"{alias}.check_output",
            f"{alias}.run",
        }
        for alias in module_aliases
    )


def _list_literal_values(node: ast.AST) -> list[str | None]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [_literal_string(element) for element in node.elts]


def _direct_apm_subprocess_lines(tree: ast.AST) -> list[int]:
    module_aliases, call_aliases = _subprocess_aliases(tree)
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
            if runs_python_module or runs_uv_apm:
                lines.add(node.lineno)
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not _is_subprocess_call(node, module_aliases, call_aliases):
            continue
        command = node.args[0]
        values = _list_literal_values(command)
        if values and values[0] in APM_EXECUTABLE_NAMES:
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
    return sorted(lines)


def _path_binary_fallback_lines(tree: ast.AST) -> list[int]:
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _attribute_name(node.func) == "shutil.which"
            and bool(node.args)
            and _literal_string(node.args[0]) == "apm"
        }
    )


def _list_literal_values(node: ast.AST) -> list[str | None]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [_literal_string(element) for element in node.elts]


def _is_subprocess_execution(call: ast.Call) -> bool:
    return _attribute_name(call.func) in {
        "subprocess.Popen",
        "subprocess.run",
    }


def _standalone_binary_selector_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        command = _list_literal_values(node)
        if any(
            command[index : index + 2]
            in (
                ["-m", "apm_cli"],
                ["-m", "apm_cli.cli"],
            )
            for index in range(len(command))
        ):
            lines.add(node.lineno)

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings = {
            value for child in ast.walk(function) if (value := _literal_string(child)) is not None
        }
        runs_subprocess = any(
            isinstance(child, ast.Call) and _is_subprocess_execution(child)
            for child in ast.walk(function)
        )
        if runs_subprocess and {"apm", "./apm", "./dist/apm"}.issubset(strings):
            lines.add(function.lineno)

        for call in (child for child in ast.walk(function) if isinstance(child, ast.Call)):
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "with_name"
                and bool(call.args)
                and _literal_string(call.args[0]) == "apm"
            ):
                lines.add(call.lineno)
            if not _is_subprocess_execution(call) or not call.args:
                continue
            command = _list_literal_values(call.args[0])
            if command and command[0] == "apm":
                lines.add(call.lineno)
            compact = [value for value in command if value is not None]
            if any(
                compact[index : index + 3] == ["uv", "run", "apm"] for index in range(len(compact))
            ):
                lines.add(call.lineno)
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
