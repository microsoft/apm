"""Static per-module facts for assertion-quality and e2e-drive ratchets.

Only ``scripts/check_test_assertions.py`` and the taxonomy cross-check use
these readers. Module-level ``pytestmark`` stays the behavioral
classification authority; ``module_markers`` reads that same declaration
statically, and ``tests/quality/test_test_taxonomy.py`` verifies it agrees
with pytest collection.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

OWNER_PACKAGE = "apm_cli"
# Attribute reads that observe user-visible output, exit codes or durable state.
OBSERVABLE_ATTRIBUTES = frozenset(
    {
        "returncode",
        "exit_code",
        "stdout",
        "stderr",
        "output",
        "out",
        "err",
        "readouterr",
        "text",
        "records",
        "messages",
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "read_text",
        "read_bytes",
        "iterdir",
        "glob",
        "rglob",
        "stat",
        "lstat",
        "readlink",
        "open",
    }
)
OBSERVABLE_CALLS = frozenset(
    {"open", "listdir", "walk", "scandir", "exists", "isfile", "isdir", "islink"}
)
OBSERVABLE_FIXTURES = frozenset(
    {"capsys", "capfd", "capsysbinary", "capfdbinary", "caplog", "snapshot"}
)
MOCK_ASSERTIONS = re.compile(r"^assert_(not_)?(any_)?(called|awaited|has_calls|has_awaits)")
HELPER_ASSERTION = re.compile(r"^_?(assert|expect|verify|check)")
DRIVER_NAMES = frozenset(
    {"apm_binary", "apm_binary_path", "apm_engine_command", "ApmLifecycleRunner", "driver"}
)
DRIVER_STRINGS = frozenset({"APM_BINARY_PATH", "APM_LIFECYCLE_DRIVER"})


def module_markers(tree: ast.Module) -> frozenset[str]:
    """Return mark names declared by the module-level ``pytestmark`` assignment."""
    names: set[str] = set()
    for statement in tree.body:
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
            if isinstance(statement, ast.AnnAssign)
            else []
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
        ):
            continue
        for node in ast.walk(statement.value or ast.Constant(None)):  # type: ignore[union-attr]
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            ):
                names.add(node.attr)
    return frozenset(names)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _observes(node: ast.AST, observable: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in OBSERVABLE_ATTRIBUTES:
            return True
        if isinstance(child, ast.Call) and _call_name(child) in OBSERVABLE_CALLS:
            return True
        if isinstance(child, ast.Name) and (
            child.id in observable or child.id in OBSERVABLE_FIXTURES
        ):
            return True
    return False


def _assigned_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def asserts_observable_outcome(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether a test asserts output, an exit code or durable state."""
    observable: set[str] = set()
    for _ in range(2):
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
                if _observes(node.value, observable):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        observable |= _assigned_names(target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None and _observes(item.context_expr, observable):
                        observable |= _assigned_names(item.optional_vars)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and _observes(
                node.iter, observable
            ):
                observable |= _assigned_names(node.target)
    for node in ast.walk(function):
        if isinstance(node, ast.Assert) and _observes(node.test, observable):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "raises" or (
                HELPER_ASSERTION.match(name) and not MOCK_ASSERTIONS.match(name)
            ):
                return True
    return False


def _owner_module_path(root: Path, module: str) -> Path | None:
    relative = Path("src", *module.split("."))
    for candidate in (relative.with_suffix(".py"), relative / "__init__.py"):
        if (root / candidate).is_file():
            return root / candidate
    return None


def owner_imports(root: Path, tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Return (local name -> owner module or symbol, imported owner modules)."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] == OWNER_PACKAGE:
                    modules.add(alias.name)
                    if alias.asname:
                        names[alias.asname] = alias.name
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".", 1)[0] == OWNER_PACKAGE
            and node.level == 0
        ):
            modules.add(node.module)
            for alias in node.names:
                qualified = f"{node.module}.{alias.name}"
                if _owner_module_path(root, qualified) is not None:
                    modules.add(qualified)
                names[alias.asname or alias.name] = qualified
    return names, modules


def _defined_names(path: Path, cache: dict[Path, frozenset[str]]) -> frozenset[str]:
    if path not in cache:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(statement.name)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                defined |= {
                    child.id
                    for target in targets
                    for child in ast.walk(target)
                    if isinstance(child, ast.Name)
                }
        cache[path] = frozenset(defined)
    return cache[path]


def _patch_target(node: ast.Call, names: dict[str, str]) -> tuple[str, str] | None:
    """Return (module or symbol, attribute) patched by one call, if any."""
    func = node.func
    name = _call_name(node)
    is_object = isinstance(func, ast.Attribute) and func.attr == "object"
    is_patch = name == "patch" or (
        is_object and isinstance(func.value, (ast.Name, ast.Attribute))  # type: ignore[union-attr]
    )
    is_setattr = name == "setattr" and isinstance(func, ast.Attribute)
    if not (is_patch or is_setattr) or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        if is_object or "." not in first.value:
            return None
        module, _sep, attribute = first.value.rpartition(".")
        return module, attribute
    if (
        isinstance(first, ast.Name)
        and first.id in names
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return names[first.id], node.args[1].value
    return None


def owner_patches(
    root: Path,
    tree: ast.Module,
    cache: dict[Path, frozenset[str]],
    *,
    defined_only: bool,
) -> Iterator[tuple[int, str]]:
    """Yield (line, target) for patches of an owner module the test imports.

    With ``defined_only`` the patched attribute must be defined in that owner
    module (patching an imported collaborator where it is looked up is fine).
    """
    names, modules = owner_imports(root, tree)
    symbols = {value for value in names.values() if value not in modules}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _patch_target(node, names)
        if target is None:
            continue
        owner, attribute = target
        if owner in symbols:
            owner, attribute = owner.rpartition(".")[0], owner.rpartition(".")[2]
        if owner not in modules:
            continue
        if defined_only:
            path = _owner_module_path(root, owner)
            if path is None or attribute not in _defined_names(path, cache):
                continue
        yield node.lineno, f"{owner}.{attribute}"


def uses_cli_runner(tree: ast.Module) -> bool:
    """Return whether the module drives the CLI in-process through CliRunner."""
    return any(
        (isinstance(node, ast.Name) and node.id == "CliRunner")
        or (isinstance(node, ast.alias) and node.name.endswith("CliRunner"))
        for node in ast.walk(tree)
    )


def drives_installed_cli(tree: ast.Module) -> bool:
    """Return whether the module runs the installed ``apm`` executable."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DRIVER_NAMES:
            return True
        if isinstance(node, ast.arg) and node.arg in DRIVER_NAMES:
            return True
        if isinstance(node, ast.alias) and (node.asname or node.name) in DRIVER_NAMES:
            return True
        if isinstance(node, ast.Constant) and node.value in DRIVER_STRINGS:
            return True
        if (
            isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "apm"
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "which"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "apm"
        ):
            return True
    return False
