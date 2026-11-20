"""Removal-site half of the closed-world orphan guard (#4011).

``install_orphan_removal.py`` polices every reference *to* the orphan owner.
This module polices what each command does *with* removal:

* every removal site still routes through the owner (a positive check, so
  deleting the call is a violation, not silence);
* at those sites, every deletion input -- ``safe_rmtree`` targets, the lock
  rows a command drops, the paths it previews -- derives from the owner's
  decision object rather than a local computation;
* a removal site never subtracts or filters an installed-path or lock-row
  universe itself, and no product module outside the owner subtracts from
  lock rows (``set(lock.dependencies) - wanted``).

The data flow is intra-procedural and name-based: a name is decision-derived
when it is bound from an owner call (or a ``decision`` parameter) or from an
expression reading a decision-derived name.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for, _summary
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import Violation

_ENTRY = "decide_orphan_removal"
# Calls whose result is the owner's decision (or a projection of it).
_DECISION_PRODUCERS = frozenset(
    {
        _ENTRY,
        "_inspect_installed_orphans",
        "_orphan_decision",
        "_preview_orphan_files",
        "_project_transitive_orphans",
        "_uninstall_orphan_decision",
    }
)
_DECISION_PARAMETERS = frozenset({"decision"})
_DELETERS = frozenset({"safe_rmtree"})
# Calls and attributes that enumerate what is installed or locked.
_UNIVERSE_CALLS = frozenset(
    {
        "_entries_leading_to",
        "_scan_installed_packages",
        "get_package_dependencies",
        "glob",
        "iterdir",
        "listdir",
        "rglob",
        "scandir",
    }
)
_UNIVERSE_ATTRIBUTES = frozenset({"dependencies", "packages"})
# Outside registered sites, only the apm_modules package scan is a universe.
_INSTALLED_SCAN = "_scan_installed_packages"
_GROWTH_METHODS = frozenset({"add", "append", "extend", "insert", "update"})
_CONSTANT_CALLS = frozenset({"set", "frozenset", "list", "tuple", "dict"})
# Lock rows are the lock model's own business.
_LOCK_MODEL = "src/apm_cli/deps/lockfile.py"

# (path, scope, callee): each removal site must reach the owner.
_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("src/apm_cli/commands/_helpers.py", "_check_orphaned_packages", "_inspect_installed_orphans"),
    ("src/apm_cli/commands/_helpers.py", "_inspect_installed_orphans", _ENTRY),
    ("src/apm_cli/commands/deps/cli.py", "_resolve_scope_deps", "_inspect_installed_orphans"),
    ("src/apm_cli/commands/prune.py", "prune", _ENTRY),
    ("src/apm_cli/commands/uninstall/engine.py", "_uninstall_orphan_decision", _ENTRY),
    (
        "src/apm_cli/commands/uninstall/engine.py",
        "_project_transitive_orphans",
        "_uninstall_orphan_decision",
    ),
    (
        "src/apm_cli/commands/uninstall/engine.py",
        "_dry_run_uninstall",
        "_project_transitive_orphans",
    ),
    (
        "src/apm_cli/commands/uninstall/engine.py",
        "_cleanup_transitive_orphans",
        "_project_transitive_orphans",
    ),
    ("src/apm_cli/install/package_selection.py", "previous_selected_subtree_keys", _ENTRY),
    ("src/apm_cli/install/phases/cleanup.py", "_orphan_decision", _ENTRY),
    ("src/apm_cli/install/phases/cleanup.py", "_run_cleanup", "_orphan_decision"),
    ("src/apm_cli/install/phases/lockfile.py", "_orphan_decision", _ENTRY),
    ("src/apm_cli/install/phases/lockfile.py", "_merge_existing", "self._orphan_decision"),
    ("src/apm_cli/install/presentation/dry_run.py", "render_and_exit", "_preview_orphan_files"),
    ("src/apm_cli/install/presentation/dry_run.py", "_preview_orphan_files", _ENTRY),
    ("src/apm_cli/policy/ci_checks.py", "_check_no_orphans", _ENTRY),
)

# (path, scope) -> names (or ``"return"``) whose every binding must derive
# from the decision. ``safe_rmtree`` targets in these scopes are sinks too.
_SINKS: dict[tuple[str, str], frozenset[str]] = {
    ("src/apm_cli/commands/_helpers.py", "_check_orphaned_packages"): frozenset({"return"}),
    ("src/apm_cli/commands/deps/cli.py", "_resolve_scope_deps"): frozenset(
        {"expected_paths", "selected_orphans"}
    ),
    ("src/apm_cli/commands/prune.py", "prune"): frozenset(
        {"missing_orphaned_keys", "orphaned_packages", "planned_pruned_keys", "pruned_keys"}
    ),
    ("src/apm_cli/commands/uninstall/engine.py", "_cleanup_transitive_orphans"): frozenset(),
    ("src/apm_cli/commands/uninstall/engine.py", "_dry_run_uninstall"): frozenset(
        {"actual_orphans"}
    ),
    ("src/apm_cli/install/package_selection.py", "previous_selected_subtree_keys"): frozenset(
        {"return"}
    ),
    ("src/apm_cli/install/phases/cleanup.py", "_remove_dropped_package_dirs"): frozenset(),
    ("src/apm_cli/install/phases/cleanup.py", "_run_cleanup"): frozenset(
        {"ctx.partial_pinned_dep_keys", "orphan_only_files"}
    ),
    ("src/apm_cli/install/phases/cleanup.py", "_trim_superseded_package_roots"): frozenset(),
    ("src/apm_cli/install/presentation/dry_run.py", "render_and_exit"): frozenset(
        {"_orphan_preview"}
    ),
    ("src/apm_cli/install/presentation/dry_run.py", "_preview_orphan_files"): frozenset({"return"}),
    ("src/apm_cli/policy/ci_checks.py", "_check_no_orphans"): frozenset({"orphaned"}),
}


def removal_site_findings(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Return route, sink and local-predicate findings for every removal site."""
    findings: list[Violation] = []
    for path, scope, callee in _ROUTES:
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        if not any(call.qualname == callee and call.scope == scope for call in facts.calls):
            findings.append(
                _summary(rule_id, path, f"{scope} must route orphan removal through {callee}")
            )
    for (path, scope), sinks in _SINKS.items():
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        function = next(
            (
                node
                for node in index.functions()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == scope
            ),
            None,
        )
        if function is None:
            findings.append(_summary(rule_id, path, f"Removal site {scope} is missing"))
            continue
        lines = getattr(provider.file_facts(path), "lines", ())
        findings.extend(_SiteScan(path, function, sinks, rule_id, lines).run())
    return findings


def installed_scan_findings(
    path: str, index, lines: tuple[str, ...], rule_id: str
) -> list[Violation]:
    """Hold every function that scans ``apm_modules`` to the site rules.

    Outside the registered sites, a function that enumerates installed
    packages may neither subtract nor filter that scan, nor delete a path
    the orphan decision did not name.
    """
    nodes = index.nodes
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _INSTALLED_SCAN
        for node in nodes
    ):
        return []  # the scanner's own module (or an unrelated namesake)
    findings: dict[tuple[int, str], Violation] = {}
    for function in index.functions():
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path, function.name) in _SINKS or not any(
            isinstance(node, ast.Call) and _terminal(node.func) == _INSTALLED_SCAN
            for node in ast.walk(function)
        ):
            continue
        scan = _SiteScan(
            path,
            function,
            frozenset(),
            rule_id,
            lines,
            universe_calls=frozenset({_INSTALLED_SCAN}),
            universe_attributes=frozenset(),
        )
        for finding in scan.run():
            findings.setdefault((finding.line, finding.message), finding)
    return list(findings.values())


def lock_row_subtraction_findings(
    path: str, nodes: Iterable[ast.AST], lines: tuple[str, ...], rule_id: str
) -> list[Violation]:
    """Reject ``lock rows - X`` (or ``.difference``) outside the owner and lock model."""
    if path == _LOCK_MODEL:
        return []
    findings: list[Violation] = []
    for node in nodes:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            operands = (node.left,)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Sub):
            operands = (node.target,)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"difference", "difference_update"}
        ):
            operands = (node.func.value,)
        else:
            continue
        if not any(_reads_lock_rows(operand) for operand in operands):
            continue
        if _exempt(node, lines):
            continue
        findings.append(
            violation(
                rule_id,
                path,
                f"Subtracting from lock rows is an orphan removal predicate; ask {_ENTRY}",
                line=node.lineno,
            )
        )
    return findings


def _reads_lock_rows(node: ast.AST) -> bool:
    """Return whether *node* is a set built from lock rows (never ``len(...)``)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in {"set", "frozenset"}:
            return False
        node = node.args[0] if node.args else node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get_package_dependencies":
            return True
        if node.func.attr in {"keys", "items", "values"}:
            node = node.func.value
    return isinstance(node, ast.Attribute) and node.attr == "dependencies"


class _SiteScan:
    """Decision-derived data flow inside one removal-site function."""

    def __init__(
        self,
        path,
        function,
        sinks,
        rule_id,
        lines,
        universe_calls=_UNIVERSE_CALLS,
        universe_attributes=_UNIVERSE_ATTRIBUTES,
    ) -> None:
        self.path = path
        self.lines = lines
        self.universe_calls = universe_calls
        self.universe_attributes = universe_attributes
        self.function = function
        self.sinks = sinks
        self.rule_id = rule_id
        self.nodes = tuple(ast.walk(function))
        self.findings: list[Violation] = []

    def _flag(self, node: ast.AST, message: str) -> None:
        if _exempt(node, self.lines):
            return
        line = getattr(node, "lineno", 1)
        self.findings.append(violation(self.rule_id, self.path, message, line=line))

    def run(self) -> list[Violation]:
        derived = self._closure(self._decision_seeds(), _calls_producer, _flows_from)
        universe = self._closure(set(), self._is_universe, _copies)
        for node in self.nodes:
            for target, value in _bindings(node):
                key = _key(target)
                if key in self.sinks and not (_constant(value) or _flows_from(value, derived)):
                    self._flag(node, f"{key} must come from the {_ENTRY} decision")
            grown = _grown_sink(node)
            if grown is not None and grown[0] in self.sinks:
                if not all(_flows_from(argument, derived) for argument in grown[1]):
                    self._flag(node, f"{grown[0]} must come from the {_ENTRY} decision")
            if (
                isinstance(node, ast.Return)
                and "return" in self.sinks
                and node.value is not None
                and not (_constant(node.value) or _flows_from(node.value, derived))
            ):
                self._flag(node, f"{self.function.name} must return the {_ENTRY} decision")
            if (
                isinstance(node, ast.Call)
                and _terminal(node.func) in _DELETERS
                and node.args
                and not _flows_from(node.args[0], derived)
            ):
                self._flag(node, f"Deleted package paths must come from the {_ENTRY} decision")
            self._check_local_predicate(node, universe, derived)
        return self.findings

    def _decision_seeds(self) -> set[str]:
        arguments = self.function.args
        return {
            argument.arg
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
            if argument.arg in _DECISION_PARAMETERS
        }

    def _closure(self, seeds: set[str], is_source, flows) -> set[str]:
        tainted = set(seeds)
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                for target, value in _bindings(node):
                    key = _key(target)
                    if key is None or key in tainted:
                        continue
                    if is_source(value) or flows(value, tainted):
                        tainted.add(key)
                        changed = True
        return tainted

    def _is_universe(self, value: ast.AST) -> bool:
        return _is_universe_source(value, self.universe_calls, self.universe_attributes)

    def _check_local_predicate(self, node: ast.AST, universe: set[str], derived: set[str]) -> None:
        """Reject subtracting or filtering an installed/locked universe at a site."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            if self._in_universe(node.left, universe):
                self._flag(node, f"Removal sites must not subtract their own sets; ask {_ENTRY}")
            return
        if isinstance(node, ast.comprehension) and self._in_universe(node.iter, universe):
            tests = list(node.ifs)
        elif (
            isinstance(node, (ast.For, ast.AsyncFor))
            and self._in_universe(node.iter, universe)
            and self._feeds_removal(node)
        ):
            tests = [
                child.test
                for statement in node.body
                for child in ast.walk(statement)
                if isinstance(child, (ast.If, ast.IfExp, ast.While))
            ]
        else:
            return
        for test in tests:
            for compare in ast.walk(test):
                if (
                    isinstance(compare, ast.Compare)
                    and any(isinstance(op, (ast.In, ast.NotIn)) for op in compare.ops)
                    and not any(
                        _reads(item, derived) or self._in_universe(item, universe)
                        for item in compare.comparators
                    )
                ):
                    self._flag(
                        compare,
                        f"Removal sites must not filter installed or locked sets; ask {_ENTRY}",
                    )

    def _feeds_removal(self, loop: ast.AST) -> bool:
        """Return whether a loop body deletes, or binds or grows a removal sink."""
        for statement in loop.body:
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and _terminal(node.func) in _DELETERS:
                    return True
                grown = _grown_sink(node)
                if grown is not None and grown[0] in self.sinks:
                    return True
                if any(_key(target) in self.sinks for target, _ in _bindings(node)):
                    return True
        return False

    def _in_universe(self, value: ast.AST, universe: set[str]) -> bool:
        return self._is_universe(value) or _copies(value, universe)


def _is_universe_source(value, universe_calls, universe_attributes) -> bool:
    """Return whether *value* is (a copy or view of) an installed or locked set."""
    pending = [value]
    while pending:
        value = pending.pop()
        while value is not None:
            if isinstance(value, ast.IfExp):
                pending.append(value.orelse)
                value = value.body
            elif (isinstance(value, ast.Call) and _terminal(value.func) in universe_calls) or (
                isinstance(value, ast.Attribute) and value.attr in universe_attributes
            ):
                return True
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in _COPY_CALLS
                and value.args
            ):
                value = value.args[0]
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in {"items", "keys", "values", "copy"}
            ):
                value = value.func.value
            elif isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                value = value.generators[0].iter
            else:
                value = None
    return False


def _grown_sink(node: ast.AST) -> tuple[str, list[ast.AST]] | None:
    """Return ``(sink, arguments)`` when *node* grows a collection in place."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _GROWTH_METHODS
    ):
        key = _key(node.func.value)
        if key is not None:
            return key, [*node.args, *(keyword.value for keyword in node.keywords)]
    return None


def _bindings(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """Return ``(target, value)`` pairs a node binds, pairing tuple unpacking."""
    pairs: list[tuple[ast.AST, ast.AST]] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            pairs.extend(_pair(target, node.value))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
        pairs.extend(_pair(node.target, node.value))
    elif isinstance(node, ast.NamedExpr):
        pairs.append((node.target, node.value))
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        pairs.extend(_pair(node.target, node.iter, unpack=False))
    return pairs


def _pair(target: ast.AST, value: ast.AST, *, unpack: bool = True):
    if isinstance(target, (ast.Tuple, ast.List)):
        if (
            unpack
            and isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
        ):
            return [
                pair for t, v in zip(target.elts, value.elts, strict=True) for pair in _pair(t, v)
            ]
        return [pair for item in target.elts for pair in _pair(item, value, unpack=False)]
    if isinstance(target, ast.Starred):
        return _pair(target.value, value, unpack=False)
    return [(target, value)]


def _key(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return ast.unparse(target)
    return None


def _exempt(node: ast.AST, lines: tuple[str, ...]) -> bool:
    first = getattr(node, "lineno", 0)
    last = getattr(node, "end_lineno", first) or first
    return any(EXEMPT_MARKER in lines[n - 1] for n in range(first, last + 1) if 0 < n <= len(lines))


def _calls_producer(value: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and _terminal(node.func) in _DECISION_PRODUCERS
        for node in ast.walk(value)
    )


def _flows_from(value: ast.AST, names: set[str]) -> bool:
    """Return whether *value* carries a decision value, not merely filters by one.

    Comprehension filters and comparisons do not carry data, except an ``in``
    test against the decision, which projects the decision onto another order.
    """
    if _calls_producer(value):
        return True
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and ast.unparse(node) in names:
            return True
        if isinstance(node, ast.comprehension):
            if any(_projects(test, names) for test in node.ifs):
                return True
            stack.extend((node.target, node.iter))
            continue
        if isinstance(node, ast.Compare):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _projects(test: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(test, ast.Compare)
        and all(isinstance(op, ast.In) for op in test.ops)
        and any(_reads(item, names) for item in test.comparators)
    )


def _reads(value: ast.AST, names: set[str]) -> bool:
    for node in ast.walk(value):
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and ast.unparse(node) in names:
            return True
    return False


_COPY_CALLS = frozenset({"set", "frozenset", "list", "sorted", "tuple", "dict", "enumerate"})


def _copies(value: ast.AST, names: set[str]) -> bool:
    """Return whether *value* is a plain copy or view of a universe name."""
    if isinstance(value, ast.Name):
        return value.id in names
    if isinstance(value, (ast.Attribute, ast.Subscript)):
        return _copies(value.value, names) or ast.unparse(value) in names
    if isinstance(value, ast.Call):
        if isinstance(value.func, ast.Name) and value.func.id in _COPY_CALLS:
            return any(_copies(argument, names) for argument in value.args)
        if isinstance(value.func, ast.Attribute) and value.func.attr in {
            "items",
            "keys",
            "values",
            "copy",
        }:
            return _copies(value.func.value, names)
    return False


def _constant(value: ast.AST) -> bool:
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        return not value.elts if not isinstance(value, ast.Dict) else not value.keys
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _CONSTANT_CALLS
        and not value.args
    )


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


__all__ = ["installed_scan_findings", "lock_row_subtraction_findings", "removal_site_findings"]
