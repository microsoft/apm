"""Closed-world guard for the orphan removal owner (#4011).

``install/orphans.py`` decides every command's package removal through
``decide_orphan_removal``. This guard scans the full product-source inventory
rather than a list of known callers, so a new command cannot skip the owner:

* the owner's entry point takes keyword-only inputs with no defaults, and its
  public surface is exactly its ``__all__``;
* every other module reaches the owner only through that public surface --
  imports, module aliases, attribute reads and ``getattr``/string lookups of
  any other owner name are rejected;
* every entry-point call, under any alias, passes every required input by
  keyword, and the entry point is never handed around by reference;
* no other module defines a removal selector, tests membership against
  removal evidence, subtracts from lock rows, imports the entry point from a
  re-export, or imports or runs the reachability walks the owner consumes;
* every removal site still calls the owner, and its deletion inputs derive
  from the decision (``install_orphan_removal_sites``).

Compilation's AGENTS.md orphan files are a separate decision and are out of
this guard's vocabulary scope.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.install_orphan_removal_sites import (
    lock_row_subtraction_findings,
    removal_site_findings,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_ORPHAN_SELECTION = "install-deployment-orphan-selection"
_OWNER = "src/apm_cli/install/orphans.py"
_OWNER_MODULE = ("apm_cli", "install", "orphans")
_ENTRY = "decide_orphan_removal"
_REACHABILITY_OWNER = "src/apm_cli/deps/reachability.py"
_REACHABILITY_WALKS = frozenset({"compute_forward_reachable_keys", "retained_lock_keys"})
# Removal evidence the owner alone may test membership against.
_REMOVAL_EVIDENCE = frozenset(
    {
        "candidate_orphans",
        "declared_lock_keys",
        "expected_installed",
        "expected_lock_keys",
        "intended_dep_keys",
        "orphan_cleanup_retained",
        "partial_dropped",
        "partial_dropped_dep_keys",
        "removed_parents",
        "removed_repo_urls",
        "surviving_files",
    }
)
_SELECTOR_NAMES = frozenset(
    {
        "build_children_index",
        "build_expected_install_paths",
        "lock_keys_by_install_path",
        "pin_retained_ancestors",
        "released_and_retained_install_paths",
    }
)
_SELECTOR_PATTERN = re.compile(r"^(find|select|detect|compute|collect|build|decide)_\w*orphan")
_OUT_OF_VOCABULARY = ("src/apm_cli/compilation/",)


def check_orphan_selection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every command's package-removal decision routes through the orphan owner."""
    findings: list[Violation] = []
    public, required, owner_symbols = _owner_contract(provider, findings)
    if public is None:
        return tuple(findings)
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, failures = _facts_for(provider, path, GUARD_ORPHAN_SELECTION)
        findings.extend(failures)
        index = provider.tree_index(path)
        if failures or index is None or index.root is None:
            continue
        findings.extend(_FileScan(path, index.nodes, public, required, owner_symbols).violations())
        findings.extend(
            lock_row_subtraction_findings(
                path, index.nodes, getattr(facts, "lines", ()), GUARD_ORPHAN_SELECTION
            )
        )
    findings.extend(removal_site_findings(provider, GUARD_ORPHAN_SELECTION))
    return tuple(findings)


def _owner_contract(
    provider: FactsProvider, findings: list[Violation]
) -> tuple[frozenset[str] | None, frozenset[str], frozenset[str]]:
    """Return the owner's ``__all__``, the entry's required inputs, and every owner name."""
    _facts, failures = _facts_for(provider, _OWNER, GUARD_ORPHAN_SELECTION)
    index = provider.tree_index(_OWNER)
    if failures or index is None or index.root is None:
        findings.extend(failures)
        return None, frozenset(), frozenset()
    public: frozenset[str] | None = None
    owner_symbols: set[str] = set()
    entry: ast.FunctionDef | None = None
    for node in index.module_children():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner_symbols.add(node.name)
            if node.name == _ENTRY and isinstance(node, ast.FunctionDef):
                entry = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    owner_symbols.add(target.id)
                    if target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        public = frozenset(
                            item.value
                            for item in node.value.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        )
    owner_symbols.discard("__all__")
    if public is None or _ENTRY not in public:
        findings.append(
            _summary(
                GUARD_ORPHAN_SELECTION,
                _OWNER,
                f"The orphan owner must declare __all__ including {_ENTRY}",
            )
        )
        return None, frozenset(), frozenset()
    arguments = entry.args if entry is not None else None
    if (
        arguments is None
        or arguments.posonlyargs
        or arguments.args
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or not arguments.kwonlyargs
        or any(default is not None for default in arguments.kw_defaults)
    ):
        findings.append(
            _summary(
                GUARD_ORPHAN_SELECTION,
                _OWNER,
                f"{_ENTRY} must take keyword-only inputs with no defaults",
            )
        )
        return None, frozenset(), frozenset()
    required = frozenset(argument.arg for argument in arguments.kwonlyargs)
    return public, required, frozenset(owner_symbols)


def _package_parts(path: str) -> list[str]:
    parts = path.removeprefix("src/").removesuffix(".py").split("/")
    return parts[:-1]


def _resolve_from(path: str, node: ast.ImportFrom) -> tuple[str, ...]:
    module = tuple(node.module.split(".")) if node.module else ()
    if not node.level:
        return module
    package = _package_parts(path)
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return (*base, *module)


class _FileScan:
    """One file's references to the owner and its removal vocabulary."""

    def __init__(self, path, nodes, public, required, owner_symbols) -> None:
        self.path = path
        self.nodes = nodes
        self.public = public
        self.required = required
        self.owner_symbols = owner_symbols
        self.module_aliases: set[str] = set()
        self.entry_aliases: set[str] = set()
        self.findings: list[Violation] = []

    def _flag(self, node: ast.AST, message: str) -> None:
        self.findings.append(
            violation(GUARD_ORPHAN_SELECTION, self.path, message, line=getattr(node, "lineno", 1))
        )

    def violations(self) -> list[Violation]:
        for node in self.nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_import(node)
        parents: dict[int, ast.AST] = {}
        for node in self.nodes:
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        in_vocabulary = not self.path.startswith(_OUT_OF_VOCABULARY)
        for node in self.nodes:
            if isinstance(node, ast.Attribute):
                self._check_attribute(node, parents)
            elif isinstance(node, ast.Name) and node.id in self.entry_aliases:
                self._check_entry_reference(node, parents)
            elif isinstance(node, ast.Call):
                self._check_dynamic_read(node)
            elif isinstance(node, ast.Constant) and node.value == ".".join(_OWNER_MODULE):
                self._flag(node, "Import the orphan owner statically, never by module name")
            if not in_vocabulary:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lstrip("_")
                if name in _SELECTOR_NAMES or _SELECTOR_PATTERN.match(name):
                    self._flag(
                        node,
                        f"{node.name} is a second orphan removal selector; extend "
                        f"install/orphans.py's {_ENTRY} instead",
                    )
            elif isinstance(node, ast.Compare):
                self._check_membership(node)
            if (
                isinstance(node, ast.Call)
                and self.path != _REACHABILITY_OWNER
                and _terminal(node.func) in _REACHABILITY_WALKS
            ):
                self._flag(
                    node,
                    f"{_terminal(node.func)} feeds removal decisions; pass a "
                    f"ReachabilityProbe to {_ENTRY} instead",
                )
        return self.findings

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if tuple(alias.name.split(".")) == _OWNER_MODULE:
                    self.module_aliases.add(alias.asname or alias.name)
            return
        module = _resolve_from(self.path, node)
        for alias in node.names:
            if (
                alias.name in _REACHABILITY_WALKS
                and self.path != _REACHABILITY_OWNER
                and not self.path.startswith(_OUT_OF_VOCABULARY)
            ):
                self._flag(
                    node,
                    f"{alias.name} feeds removal decisions; pass a ReachabilityProbe "
                    f"to {_ENTRY} instead",
                )
            if alias.name == _ENTRY and module != _OWNER_MODULE:
                self._flag(
                    node,
                    f"Import {_ENTRY} from install/orphans.py only, never through a re-export",
                )
                self.entry_aliases.add(alias.asname or alias.name)
            if module == _OWNER_MODULE:
                if alias.name not in self.public:
                    self._flag(
                        node,
                        f"{alias.name} is private to install/orphans.py; call {_ENTRY}",
                    )
                elif alias.name == _ENTRY:
                    self.entry_aliases.add(alias.asname or alias.name)
            elif (*module, alias.name) == _OWNER_MODULE:
                self.module_aliases.add(alias.asname or alias.name)

    def _is_module(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.module_aliases
        return ast.unparse(node) in self.module_aliases

    def _check_attribute(self, node: ast.Attribute, parents: dict[int, ast.AST]) -> None:
        if not self._is_module(node.value):
            return
        if node.attr not in self.public:
            self._flag(node, f"{node.attr} is private to install/orphans.py; call {_ENTRY}")
        elif node.attr == _ENTRY:
            self._check_entry_reference(node, parents)

    def _check_entry_reference(self, node: ast.AST, parents: dict[int, ast.AST]) -> None:
        parent = parents.get(id(node))
        if not (isinstance(parent, ast.Call) and parent.func is node):
            self._flag(node, f"Call {_ENTRY} directly; never pass or rebind it")
            return
        if parent.args:
            self._flag(parent, f"{_ENTRY} takes keyword-only inputs")
        if any(keyword.arg is None for keyword in parent.keywords):
            self._flag(parent, f"{_ENTRY} inputs must be named, not unpacked")
            return
        missing = self.required - {keyword.arg for keyword in parent.keywords}
        if missing:
            self._flag(
                parent,
                f"{_ENTRY} call omits required input(s): {', '.join(sorted(missing))}",
            )

    def _check_dynamic_read(self, node: ast.Call) -> None:
        if not (isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}):
            return
        if len(node.args) < 2:
            return
        name = node.args[1]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            if name.value in self.owner_symbols:
                self._flag(node, f"Dynamic read of orphan owner name {name.value!r}")
        elif self._is_module(node.args[0]):
            self._flag(node, "Dynamic read of an orphan owner attribute")

    def _check_membership(self, node: ast.Compare) -> None:
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            return
        for comparator in node.comparators:
            name = _terminal(comparator)
            if name in _REMOVAL_EVIDENCE:
                self._flag(
                    node,
                    f"Membership in {name} is an orphan removal predicate; ask {_ENTRY}",
                )


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
