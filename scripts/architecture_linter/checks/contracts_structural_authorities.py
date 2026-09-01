"""Executable test-contract authorities: binary, parity, and ratchet.

Ports ``contracts-tests-test-contract-authorities`` -- executable test
binary selection, rendered CLI parity, and ratchet authority owners -- the
single guard-less structural rule that composes all three sub-scans behind
:func:`check_test_contract_authorities`. Kept apart from
:mod:`contracts_scope_binding`'s lower-level scope/binding primitives purely
for module-size budget reasons.
"""

from __future__ import annotations

import ast
import shlex

from scripts.architecture_linter.checks.contracts_scope_binding import (
    ScopeBindingMaps,
    _assignment_command_values,
    _attribute_name,
    _direct_binary_env_read_lines,
    _direct_binary_path_lookup_lines,
    _is_subprocess_call,
    _list_literal_values,
    _literal_string,
    _local_binary_facade_lines,
    _python_sibling_binary_lines,
    _scope_binding_maps,
    _scope_parent_map,
    _scoped_value,
    _shell_enabled,
    _venv_binary_fallback_lines,
)
from scripts.architecture_linter.checks.contracts_test_shared import (
    _APM_EXECUTABLE_NAMES,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.python_semantics import propagated_assignment_values
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

_BINARY_OWNER = "tests/integration/conftest.py"


_PARITY_OWNER = "scripts/check_cli_docs.py"


_PARITY_FACADE = "registry_docs_mismatches"


_PARITY_INTERNALS = frozenset({"public_top_level_commands", "rendered_cli_reference_pages"})


_PARITY_OWNER_FUNCTIONS = _PARITY_INTERNALS | {_PARITY_FACADE}


_TEST_FILE_INVENTORY_OWNER = "scripts/test_file_inventory.py"


_RATCHET_BASELINE_OWNER = "scripts/ratchet_baseline.py"


_RATCHET_AUTHORITY_CONSUMERS: dict[str, tuple[str, ...]] = {
    "scripts/check_test_assertions.py": (
        "from test_file_inventory import tracked_python_paths",
        "from ratchet_baseline import",
    ),
    "scripts/check_exact_test_duplicates.py": (
        "from test_file_inventory import tracked_python_paths",
        "from ratchet_baseline import",
    ),
    "tests/quality/repository_python_inventory.py": (
        "from scripts.test_file_inventory import tracked_python_paths",
    ),
    "tests/quality/test_ci_topology.py": (
        "from scripts.test_file_inventory import is_test_module_path",
    ),
}


_RATCHET_LOCAL_INVENTORY_SHAPES: dict[str, tuple[str, ...]] = {
    "scripts/check_test_assertions.py": ('rglob("*.py")', '"ls-files"'),
    "scripts/check_exact_test_duplicates.py": ('"ls-files"',),
    "tests/quality/repository_python_inventory.py": ('"ls-files"',),
}


def _direct_apm_subprocess_lines(
    index: TreeIndex,
    binding_maps: ScopeBindingMaps | None = None,
) -> list[int]:
    """Find direct apm subprocess selection outside the canonical owner."""
    bindings_by_scope, scope_by_node = binding_maps or _scope_binding_maps(index)
    scope_parents = _scope_parent_map(index)
    lists_by_scope, strings_by_scope = _assignment_command_values(index, scope_by_node)
    root = index.root
    lines: set[int] = set()
    for node in index.walk(root):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = _list_literal_values(node)
            attributes = {
                _attribute_name(child)
                for child in index.walk(node)
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
            runs_bare_uv_apm = len(values) >= 3 and values[:3] == ["uv", "run", "apm"]
            if runs_python_module or runs_uv_apm or runs_bare_uv_apm:
                lines.add(node.lineno)
        if not isinstance(node, ast.Call) or not node.args:
            continue
        scope = scope_by_node.get(id(node), root)
        if not _is_subprocess_call(node, bindings_by_scope.get(id(scope), {})):
            continue
        command = node.args[0]
        values = _list_literal_values(command)
        if not values and isinstance(command, ast.Name):
            inherited_list = _scoped_value(command.id, scope, lists_by_scope, scope_parents)
            values = inherited_list if isinstance(inherited_list, list) else []
        if values and values[0] in _APM_EXECUTABLE_NAMES:
            lines.add(node.lineno)
        if _shell_enabled(node):
            shell_command = _literal_string(command)
            if shell_command is None and isinstance(command, ast.Name):
                inherited_string = _scoped_value(command.id, scope, strings_by_scope, scope_parents)
                if isinstance(inherited_string, str):
                    shell_command = inherited_string
            if shell_command is not None:
                try:
                    shell_tokens = shlex.split(shell_command, posix=True)
                except ValueError:
                    shell_tokens = []
                if shell_tokens and shell_tokens[0] in _APM_EXECUTABLE_NAMES:
                    lines.add(node.lineno)
        noncanonical_names = {
            child.id
            for child in index.walk(command)
            if isinstance(child, ast.Name)
            and child.id in {"apm_bin", "apm_binary", "apm_command", "apm_executable", "apm_path"}
        }
        if noncanonical_names:
            lines.add(node.lineno)
    for function in index.walk(root):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings = {
            value for child in index.walk(function) if (value := _literal_string(child)) is not None
        }
        runs_subprocess = any(
            isinstance(child, ast.Call)
            and _is_subprocess_call(
                child, bindings_by_scope.get(id(scope_by_node.get(id(child), root)), {})
            )
            for child in index.walk(function)
        )
        if runs_subprocess and {"apm", "./apm", "./dist/apm"}.issubset(strings):
            lines.add(function.lineno)
    return sorted(lines)


def _implicit_lifecycle_runner_lines(index: TreeIndex) -> list[int]:
    """Find lifecycle runners that would choose an APM executable locally."""
    root = index.root
    runner_aliases: set[str] = set()
    module_aliases: set[str] = set()
    for node in index.walk(root):
        if isinstance(node, ast.ImportFrom) and node.module == "tests.utils.apm_lifecycle_runner":
            runner_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "ApmLifecycleRunner"
            )
        elif isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "tests.utils.apm_lifecycle_runner"
            )
    lines: set[int] = set()
    for node in index.walk(root):
        if not isinstance(node, ast.Call):
            continue
        called = _attribute_name(node.func)
        if called not in runner_aliases and not any(
            called == f"{alias}.ApmLifecycleRunner" for alias in module_aliases
        ):
            continue
        command_keyword = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "command"), None
        )
        implicit = not node.args and command_keyword is None
        explicit_none = (
            bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value is None
        ) or (isinstance(command_keyword, ast.Constant) and command_keyword.value is None)
        if implicit or explicit_none:
            lines.add(node.lineno)
    return sorted(lines)


def _defined_functions(index: TreeIndex) -> set[str]:
    """Return every top-level function name defined in the module."""
    if index.root is None:
        return set()
    return {
        node.name
        for node in index.children(index.root)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parity_import_violations(index: TreeIndex, path: str, rule_id: str) -> list[Violation]:
    """Reject non-facade imports of the rendered-parity owner module."""
    findings: list[Violation] = []
    for node in index.walk(index.root):
        if isinstance(node, ast.ImportFrom) and node.module == "scripts.check_cli_docs":
            for alias in node.names:
                if alias.name in _PARITY_INTERNALS:
                    findings.append(
                        violation(
                            rule_id,
                            path,
                            f"internal rendered parity projection imported: {alias.name}; "
                            f"consume {_PARITY_FACADE}",
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
            for alias in node.names:
                if alias.name == "check_cli_docs":
                    findings.append(
                        violation(
                            rule_id,
                            path,
                            f"rendered parity module imported directly; import {_PARITY_FACADE} only",
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scripts.check_cli_docs":
                    findings.append(
                        violation(
                            rule_id,
                            path,
                            f"rendered parity module imported directly; import {_PARITY_FACADE} only",
                            line=node.lineno,
                        )
                    )
    return findings


def _is_commands_items(call: ast.Call) -> bool:
    """Return whether a call is ``<x>.commands.items()``."""
    called = _attribute_name(call.func)
    return called is not None and called.endswith(".commands.items")


def _registry_projection_lines(index: TreeIndex) -> list[int]:
    """Find direct Click command-registry projections that filter hidden."""
    root = index.root
    lines: set[int] = set()
    for node in index.walk(root):
        if not isinstance(node, (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            continue
        projects_commands = any(
            isinstance(generator.iter, ast.Call) and _is_commands_items(generator.iter)
            for generator in node.generators
        )
        filters_hidden = any(
            isinstance(child, ast.Attribute) and child.attr == "hidden"
            for child in index.walk(node)
        )
        if projects_commands and filters_hidden:
            lines.add(node.lineno)
    for node in index.walk(root):
        if not isinstance(node, ast.For):
            continue
        projects_commands = isinstance(node.iter, ast.Call) and _is_commands_items(node.iter)
        filters_hidden = any(
            isinstance(child, ast.Attribute) and child.attr == "hidden"
            for child in index.walk(node)
        )
        collects_names = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"add", "append", "update"}
            for child in index.walk(node)
        )
        if projects_commands and filters_hidden and collects_names:
            lines.add(node.lineno)
    return sorted(lines)


def _path_string_segments(index: TreeIndex, node: ast.AST) -> set[str]:
    """Return every string literal segment inside an expression subtree."""
    return {value for child in index.walk(node) if (value := _literal_string(child)) is not None}


def _path_segments(index: TreeIndex, node: ast.AST, known: dict[str, set[str]]) -> set[str]:
    """Return literal and name-derived path segments for an expression."""
    segments = _path_string_segments(index, node)
    segments.update(
        segment
        for child in index.walk(node)
        if isinstance(child, ast.Name)
        for segment in known.get(child.id, set())
    )
    return segments


def _rendered_cli_path_names(index: TreeIndex) -> set[str]:
    """Return names bound to a rendered CLI reference path (fixed point)."""
    known = propagated_assignment_values(index, _path_segments)
    return {name for name, segments in known.items() if {"reference", "cli"}.issubset(segments)}


def _rendered_inventory_lines(index: TreeIndex) -> list[int]:
    """Find direct rendered-CLI route inventory (glob/iterdir on reference/cli)."""
    root = index.root
    lines: set[int] = set()
    rendered_path_names = _rendered_cli_path_names(index)
    for node in index.walk(root):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "glob",
            "iterdir",
            "rglob",
        }:
            continue
        parent = node.func.value
        if {"reference", "cli"}.issubset(_path_string_segments(index, parent)) or (
            isinstance(parent, ast.Name) and parent.id in rendered_path_names
        ):
            lines.add(node.lineno)
    for node in index.walk(root):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if "index.html" not in _path_string_segments(index, node):
            continue
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"is_file", "exists"}
            for child in index.walk(node)
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _cannot_inspect(facts: object, path: str, rule_id: str) -> Violation | None:
    """Return a fail-closed violation for an unreadable/unparseable file."""
    if getattr(facts, "read_error", None) is not None:
        return _summary(rule_id, path, f"cannot inspect: {facts.read_error}")
    if getattr(facts, "parse_error", None) is not None:
        return _summary(rule_id, path, f"cannot inspect: {facts.parse_error}")
    return None


def _binary_subcheck_results(index: TreeIndex) -> tuple[tuple[list[int], str], ...]:
    """Run binary-selection detectors with one shared scope-binding derivation."""
    binding_maps = _scope_binding_maps(index)
    return (
        (
            _direct_binary_env_read_lines(index, binding_maps),
            "direct APM_BINARY_PATH read outside {owner}; consume the apm_binary_path fixture",
        ),
        (
            _direct_binary_path_lookup_lines(index, binding_maps),
            "direct PATH lookup for apm outside {owner}; consume the apm_binary_path fixture",
        ),
        (
            _venv_binary_fallback_lines(index),
            "direct .venv apm fallback outside {owner}; consume the apm_binary_path fixture",
        ),
        (
            _python_sibling_binary_lines(index),
            "interpreter-relative apm selection outside {owner}; consume the apm_binary_path fixture",
        ),
        (
            _local_binary_facade_lines(index),
            "local apm binary fixture or facade outside {owner}; inject apm_binary_path directly",
        ),
        (
            _direct_apm_subprocess_lines(index, binding_maps),
            "direct apm subprocess selection outside {owner}; inject apm_binary_path directly",
        ),
        (
            _implicit_lifecycle_runner_lines(index),
            "implicit lifecycle runner APM selection outside {owner}; pass apm_binary_path as the runner command",
        ),
    )


def find_binary_selection_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Reject every direct integration-test binary read outside the owner."""
    findings: list[Violation] = []
    owner_facts = provider.file_facts(_BINARY_OWNER)
    owner_problem = _cannot_inspect(owner_facts, _BINARY_OWNER, rule_id)
    if owner_problem is not None:
        findings.append(owner_problem)
    else:
        owner_index = provider.tree_index(_BINARY_OWNER)
        if owner_index is None or "_resolve_apm_binary" not in _defined_functions(owner_index):
            findings.append(
                _summary(rule_id, _BINARY_OWNER, f"{_BINARY_OWNER} must define _resolve_apm_binary")
            )

    for path in _python_paths(provider, "tests/integration/"):
        if path == _BINARY_OWNER:
            continue
        facts = provider.file_facts(path)
        problem = _cannot_inspect(facts, path, rule_id)
        if problem is not None:
            findings.append(problem)
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        for lines, template in _binary_subcheck_results(index):
            message = template.format(owner=_BINARY_OWNER)
            for line in lines:
                findings.append(violation(rule_id, path, message, line=line))
        if path.count("/") == 2 and "_resolve_apm_binary" in _defined_functions(index):
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"duplicate _resolve_apm_binary definition; owner is {_BINARY_OWNER}",
                    line=1,
                )
            )
    return findings


def _parity_needs_index(text: str) -> bool:
    """Cheap pre-filter: a parity anti-pattern requires one of these tokens.

    Every rendered-parity violation (facade-bypassing import, Click registry
    projection, or rendered-CLI route inventory) is impossible unless the file
    text contains the corresponding token, so files lacking all of them can be
    skipped without a parse or walk -- keeping cost proportional to the handful
    of files that could actually match rather than the whole scanned corpus.
    """
    if "check_cli_docs" in text or "commands.items" in text or "index.html" in text:
        return True
    globs_paths = "rglob" in text or ".glob(" in text or "iterdir" in text
    return globs_paths and "reference" in text and "cli" in text


def find_rendered_parity_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Enforce facade-only consumers and unique registry/page projections."""
    findings: list[Violation] = []
    owner_facts = provider.file_facts(_PARITY_OWNER)
    owner_problem = _cannot_inspect(owner_facts, _PARITY_OWNER, rule_id)
    if owner_problem is not None:
        findings.append(owner_problem)
    else:
        owner_index = provider.tree_index(_PARITY_OWNER)
        defined = _defined_functions(owner_index) if owner_index is not None else set()
        for name in sorted(_PARITY_OWNER_FUNCTIONS - defined):
            findings.append(
                _summary(
                    rule_id,
                    _PARITY_OWNER,
                    f"{_PARITY_OWNER} must define rendered parity owner function: {name}",
                )
            )

    candidates: list[str] = []
    for prefix in ("src/apm_cli/", "scripts/", "tests/"):
        candidates.extend(_python_paths(provider, prefix))
    for path in candidates:
        if path == _PARITY_OWNER:
            continue
        text, read_error = provider.source_cache.read(path)
        if text is None:
            findings.append(_summary(rule_id, path, f"cannot inspect: {read_error}"))
            continue
        if not _parity_needs_index(text):
            continue
        facts = provider.file_facts(path)
        problem = _cannot_inspect(facts, path, rule_id)
        if problem is not None:
            findings.append(problem)
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        findings.extend(_parity_import_violations(index, path, rule_id))
        for line in _registry_projection_lines(index):
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"direct Click command registry projection; consume {_PARITY_FACADE}",
                    line=line,
                )
            )
        for line in _rendered_inventory_lines(index):
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"direct rendered CLI route inventory; consume {_PARITY_FACADE}",
                    line=line,
                )
            )
    return findings


def find_ratchet_authority_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Require ratchet consumers to route through shared file/baseline owners."""
    findings: list[Violation] = []
    for owner in (_TEST_FILE_INVENTORY_OWNER, _RATCHET_BASELINE_OWNER):
        if not provider.file_facts(owner).exists:
            findings.append(_summary(rule_id, owner, f"missing ratchet authority owner: {owner}"))

    for relative, required_imports in _RATCHET_AUTHORITY_CONSUMERS.items():
        facts = provider.file_facts(relative)
        if not facts.exists:
            findings.append(
                _summary(rule_id, relative, f"missing ratchet authority consumer: {relative}")
            )
            continue
        for required_import in required_imports:
            if not _present(facts, required_import):
                findings.append(
                    _summary(
                        rule_id, relative, f"must consume ratchet authority: {required_import}"
                    )
                )

    for relative, forbidden_shapes in _RATCHET_LOCAL_INVENTORY_SHAPES.items():
        facts = provider.file_facts(relative)
        if not facts.exists:
            continue
        for forbidden in forbidden_shapes:
            if _present(facts, forbidden):
                findings.append(
                    _summary(
                        rule_id,
                        relative,
                        f"duplicate tracked Python inventory: {forbidden}; "
                        f"owner is {_TEST_FILE_INVENTORY_OWNER}",
                    )
                )
    return findings
