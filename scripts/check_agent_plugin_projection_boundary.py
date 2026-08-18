#!/usr/bin/env python3
"""Reject Agent Plugin compatibility and legacy-normalization split authorities."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _function_calls(node: ast.AST) -> set[str]:
    return {_call_name(item) for item in ast.walk(node) if isinstance(item, ast.Call)}


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _calls_public_configuration_thaw(node: ast.AST) -> bool:
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "thaw_frozen_json"
            and len(item.args) == 1
            and isinstance(item.args[0], ast.Attribute)
            and item.args[0].attr == "values"
            and isinstance(item.args[0].value, ast.Name)
            and item.args[0].value.id == "configuration"
        ):
            return True
    return False


def _is_validation_package(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "package"
        and isinstance(node.value, ast.Name)
        and node.value.id == "validation"
    )


def _is_named_assignment(node: ast.AST, target: str, call: str) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == target
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == call
    )


def _stored_name_count(node: ast.AST, name: str) -> int:
    return sum(
        1
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id == name
    )


def check(root: Path) -> list[str]:
    """Return projection-boundary violations under one repository root."""
    source_root = root / "src" / "apm_cli"
    projection_path = source_root / "agent_plugins" / "projection.py"
    package_path = source_root / "models" / "apm_package.py"
    validation_path = source_root / "models" / "validation.py"
    resolver_path = source_root / "deps" / "apm_resolver.py"
    required = (projection_path, package_path, validation_path, resolver_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return [f"required owner file is missing: {path}" for path in missing]

    violations: list[str] = []
    parsed: dict[Path, ast.Module] = {}
    for path in sorted(source_root.rglob("*.py")):
        try:
            parsed[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(f"{path}: could not inspect source: {exc}")

    projection_tree = parsed.get(projection_path)
    package_tree = parsed.get(package_path)
    validation_tree = parsed.get(validation_path)
    resolver_tree = parsed.get(resolver_path)
    if (
        projection_tree is None
        or package_tree is None
        or validation_tree is None
        or resolver_tree is None
    ):
        return violations

    projection_defs = [
        node
        for node in projection_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "project_agent_plugin_package"
    ]
    if len(projection_defs) != 1:
        violations.append(
            f"{projection_path}: project_agent_plugin_package must have exactly one definition"
        )
    elif "APMPackage.from_mapping" not in _function_calls(projection_defs[0]):
        violations.append(f"{projection_path}: projection must call APMPackage.from_mapping")
    else:
        projection_args = projection_defs[0].args
        annotation = projection_defs[0].returns
        if (
            len(projection_args.args) != 1
            or projection_args.args[0].arg != "plugin"
            or not isinstance(projection_args.args[0].annotation, ast.Name)
            or projection_args.args[0].annotation.id != "AgentPlugin"
            or not isinstance(annotation, ast.Name)
            or annotation.id != "APMPackage"
        ):
            violations.append(
                f"{projection_path}: projection must retain its typed AgentPlugin-to-APMPackage "
                "public contract"
            )

    mapping_defs = [
        node
        for node in ast.walk(package_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "from_mapping"
    ]
    if len(mapping_defs) != 1:
        violations.append(f"{package_path}: APMPackage.from_mapping must have one definition")
    file_loader_defs = [
        node
        for node in ast.walk(package_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "from_apm_yml"
    ]
    file_loader_preserves_owner = False
    if len(file_loader_defs) == 1:
        file_loader = file_loader_defs[0]
        owner_assignments = [
            node
            for node in ast.walk(file_loader)
            if _is_named_assignment(node, "result", "cls.from_mapping")
        ]
        file_loader_preserves_owner = (
            len(owner_assignments) == 1
            and _stored_name_count(file_loader, "result") == 1
            and any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Name)
                and node.value.id == "result"
                for node in file_loader.body
            )
        )
    if not file_loader_preserves_owner:
        violations.append(
            f"{package_path}: APMPackage file loading must route through from_mapping owner"
        )

    validation_defs = [
        node
        for node in validation_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_agent_plugin"
    ]
    if len(validation_defs) != 1:
        violations.append(f"{validation_path}: _validate_agent_plugin must have one definition")
    else:
        validation_def = validation_defs[0]
        calls = _function_calls(validation_def)
        if "project_agent_plugin_package" not in calls:
            violations.append(
                f"{validation_path}: native validation must call project_agent_plugin_package"
            )
        package_assignments = [
            node
            for node in ast.walk(validation_def)
            if _is_named_assignment(node, "package", "project_agent_plugin_package")
        ]
        result_package_assignments = [
            node
            for node in ast.walk(validation_def)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "package"
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "result"
            and isinstance(node.value, ast.Name)
            and node.value.id == "package"
        ]
        if (
            "APMPackage" in calls
            or "normalize_plugin_directory" in calls
            or len(package_assignments) != 1
            or _stored_name_count(validation_def, "package") != 1
            or len(result_package_assignments) != 1
        ):
            violations.append(
                f"{validation_path}: native validation bypasses projection or enters normalization"
            )

    resolver_defs = [
        node
        for node in resolver_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "APMDependencyResolver"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "_try_load_dependency_package"
    ]
    if len(resolver_defs) != 1:
        violations.append(
            f"{resolver_path}: _try_load_dependency_package must have exactly one definition"
        )
    else:
        resolver_calls = _function_calls(resolver_defs[0])
        if "validate_apm_package" not in resolver_calls:
            violations.append(
                f"{resolver_path}: Agent Plugin dependency loading must preserve "
                "the projected package"
            )
        native_branches = [
            node
            for node in ast.walk(resolver_defs[0])
            if isinstance(node, ast.If)
            and any(
                isinstance(item, ast.Attribute)
                and item.attr == "AGENT_PLUGIN"
                and isinstance(item.value, ast.Name)
                and item.value.id == "PackageType"
                for item in ast.walk(node.test)
            )
        ]
        native_branch_preserves_package = (
            len(native_branches) == 1
            and bool(native_branches[0].body)
            and isinstance(native_branches[0].body[-1], ast.Return)
            and _is_validation_package(native_branches[0].body[-1].value)
        )
        if not native_branch_preserves_package:
            violations.append(
                f"{resolver_path}: Agent Plugin dependency loading must preserve "
                "the projected package"
            )

    raw_reader_calls = {
        "json.load",
        "json.loads",
        "load_yaml",
        "read_json_document",
        "yaml.load",
        "yaml.safe_load",
    }
    for path, tree in parsed.items():
        relative = path.relative_to(root).as_posix()
        for function in _functions(tree):
            calls = _function_calls(function)
            if "normalize_plugin_directory" in calls and not (
                relative == "src/apm_cli/models/validation.py"
                and function.name == "_validate_marketplace_plugin"
            ):
                violations.append(
                    f"{relative}:{function.lineno}: Claude normalization call outside "
                    "_validate_marketplace_plugin"
                )
            if (
                "APMPackage" in calls
                and calls.intersection(raw_reader_calls)
                and relative != "src/apm_cli/agent_plugins/projection.py"
            ):
                violations.append(
                    f"{relative}:{function.lineno}: raw document parsing constructs APMPackage"
                )

    projection_calls = _function_calls(projection_tree)
    allowed_projection_calls = {
        "APMPackage.from_mapping",
        "AgentPluginManifestAuthorityError",
        "_project_apm_configuration",
        "data.update",
        "dict",
        "get",
        "isinstance",
        "thaw_frozen_json",
    }
    unexpected_projection_calls = sorted(projection_calls - allowed_projection_calls)
    if unexpected_projection_calls:
        violations.append(
            f"{projection_path}: projection call surface must remain pure; "
            f"unexpected calls: {', '.join(unexpected_projection_calls)}"
        )
    thaw_bindings: list[tuple[ast.AST, ast.alias]] = []
    for node in ast.walk(projection_tree):
        if isinstance(node, ast.ImportFrom):
            thaw_bindings.extend(
                (node, alias)
                for alias in node.names
                if (alias.asname or alias.name) == "thaw_frozen_json"
            )
        elif isinstance(node, ast.Import):
            thaw_bindings.extend(
                (node, alias)
                for alias in node.names
                if (alias.asname or alias.name.split(".", 1)[0]) == "thaw_frozen_json"
            )
    thaw_imported = (
        len(thaw_bindings) == 1
        and isinstance(thaw_bindings[0][0], ast.ImportFrom)
        and thaw_bindings[0][0].level == 1
        and thaw_bindings[0][0].module == "ir"
        and thaw_bindings[0][1].name == "thaw_frozen_json"
        and thaw_bindings[0][1].asname is None
    )
    thaw_rebound = any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "thaw_frozen_json"
        )
        or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == "thaw_frozen_json"
        )
        for node in ast.walk(projection_tree)
    )
    configuration_defs = [
        node
        for node in projection_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_project_apm_configuration"
    ]
    thaw_assignment_is_preserved = False
    if len(configuration_defs) == 1:
        configuration_def = configuration_defs[0]
        thaw_assignments = [
            node
            for node in ast.walk(configuration_def)
            if _is_named_assignment(node, "projected", "thaw_frozen_json")
            and isinstance(node.value, ast.Call)
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Attribute)
            and node.value.args[0].attr == "values"
            and isinstance(node.value.args[0].value, ast.Name)
            and node.value.args[0].value.id == "configuration"
        ]
        thaw_assignment_is_preserved = (
            len(thaw_assignments) == 1
            and _stored_name_count(configuration_def, "projected") == 1
            and bool(configuration_def.body)
            and isinstance(configuration_def.body[-1], ast.Return)
            and isinstance(configuration_def.body[-1].value, ast.Name)
            and configuration_def.body[-1].value.id == "projected"
        )
    if (
        not thaw_imported
        or thaw_rebound
        or len(configuration_defs) != 1
        or not _calls_public_configuration_thaw(configuration_defs[0])
        or not thaw_assignment_is_preserved
    ):
        violations.append(f"{projection_path}: projection must thaw canonical FrozenJson")
    if "APMPackage" in projection_calls:
        violations.append(f"{projection_path}: projection must not call APMPackage directly")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    violations = check(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
