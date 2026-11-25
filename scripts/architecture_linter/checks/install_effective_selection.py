"""Guard the single effective deploy-selection owner (#3760).

Every package deployment must deploy the dependency's effective skill and
target selection. ``effective_dependency_selection`` in
``install/package_resolution.py`` owns that decision; install preflight,
install deploy, audit replay and uninstall survivor re-integration route
through it, and so do the lockfile record of the deployed subset and every
target resolution (``resolve_effective_package_targets``). ``skill_subset`` is
a required keyword of the deploy functions, so an omitted selection is a
``TypeError`` instead of "deploy every skill". Aliased imports of a deploy
function are resolved, so renaming a call does not bypass the guard.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_EFFECTIVE_SELECTION = "install-deployment-effective-selection"
_OWNER = "src/apm_cli/install/package_resolution.py"
_OWNER_FUNCTION = "effective_dependency_selection"
_DEPLOY_FUNCTIONS = frozenset({"integrate_package_primitives", "prepare_package_deployment"})
_DEPLOY_METHODS = frozenset({"DeployableSourcePlan.create"})
_TARGET_FUNCTION = "resolve_effective_package_targets"
# The union of persisted and CLI skills is computed only inside the owner.
_OWNER_INTERNAL = "effective_deploy_skill_subset"
# Deploy functions whose ``skill_subset`` keyword must stay required.
_REQUIRED_KEYWORD = (
    ("src/apm_cli/install/services.py", "integrate_package_primitives"),
    ("src/apm_cli/install/deployment_plan.py", "prepare_package_deployment"),
    ("src/apm_cli/install/deployable_source_plan.py", "DeployableSourcePlan.create"),
)
# (path, enclosing function) -> parameter forwarded unchanged to the next stage.
_FORWARDERS: dict[tuple[str, str], str] = {
    ("src/apm_cli/install/services.py", "integrate_package_primitives"): "selection parameter",
    ("src/apm_cli/install/deployment_plan.py", "prepare_package_deployment"): "selection parameter",
}
# (path, enclosing function) -> target-subset parameter forwarded unchanged.
_TARGET_FORWARDERS: dict[tuple[str, str], str] = {
    ("src/apm_cli/install/deployment_plan.py", "prepare_package_deployment"): "dep_target_subset",
}
# (path, enclosing function) -> why an explicit ``skill_subset=None`` is correct.
_EXPLICIT_NONE: dict[tuple[str, str], str] = {
    (
        "src/apm_cli/integration/hook_reintegration.py",
        "build_hook_reintegration_source_plan",
    ): "hooks-only source plan; it never deploys skills",
    (
        "src/apm_cli/integration/skill_integrator.py",
        "copy_skill_to_target",
    ): "direct-caller fallback when no authorized package plan exists",
}
# (path, enclosing function) -> why the selection is copied from an owner plan.
_PLAN_DERIVED: dict[tuple[str, str], str] = {
    (
        "src/apm_cli/integration/skill_adoption.py",
        "LockedSkillSources._materialize",
    ): "mirrors the already-authorized plan's selected_skill_names for a locked source",
}
_SELECTION_FIELDS = {"skill_subset": "skill_subset", "dep_target_subset": "target_subset"}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(index: TreeIndex, tracked: frozenset[str]) -> dict[str, str]:
    """Map every local name bound to a tracked function onto that function."""
    names = {name: name for name in tracked}
    for node in index.walk(index.root):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in tracked and alias.asname:
                    names[alias.asname] = alias.name
    for node in index.module_children():
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            original = names.get(node.value.id)
            if original is not None:
                names.update({t.id: original for t in node.targets if isinstance(t, ast.Name)})
    return names


def _called(node: ast.AST, names: dict[str, str]) -> str | None:
    """Return the tracked function `node` calls, through any local alias."""
    if not isinstance(node, ast.Call):
        return None
    dotted = _dotted(node.func)
    if dotted in _DEPLOY_METHODS:
        return dotted
    return names.get(dotted.split(".")[-1])


def _is_owner_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] == _OWNER_FUNCTION


def _enclosing_function(index: TreeIndex, node: ast.AST) -> ast.AST | None:
    current = index.parent(node)
    while current is not None and not isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
        current = index.parent(current)
    return current


def _qualname(index: TreeIndex, function: ast.AST | None) -> str:
    if function is None:
        return ""
    try:
        return index.function_qualname(function)
    except RuntimeError:
        return getattr(function, "name", "")


def _owner_bound_names(index: TreeIndex, function: ast.AST | None) -> set[str]:
    """Return local names bound directly from an owner call in `function`."""
    if function is None:
        return set()
    names: set[str] = set()
    for node in index.own_scope(function):
        if isinstance(node, ast.Assign) and _is_owner_call(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _is_owner_call(node.value) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _from_owner(value: ast.AST, field: str, owner_names: set[str]) -> bool:
    """Return whether `value` reads `field` of an owner-produced selection."""
    if not isinstance(value, ast.Attribute) or value.attr != field:
        return False
    source = value.value
    return _is_owner_call(source) or (isinstance(source, ast.Name) and source.id in owner_names)


def _report(path: str, call: ast.AST, message: str) -> Violation:
    return violation(
        GUARD_EFFECTIVE_SELECTION,
        path,
        message,
        line=call.lineno,
        column=call.col_offset + 1,
    )


def _check_target_call(path: str, index: TreeIndex, call: ast.Call) -> list[Violation]:
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    value = keywords.get("dep_target_subset", call.args[1] if len(call.args) > 1 else None)
    function = _enclosing_function(index, call)
    site = (path, _qualname(index, function))
    if value is not None and _from_owner(
        value, "target_subset", _owner_bound_names(index, function)
    ):
        return []
    forwarded = _TARGET_FORWARDERS.get(site)
    if forwarded and isinstance(value, ast.Name) and value.id == forwarded:
        return []
    return [
        _report(
            path,
            call,
            f"{_TARGET_FUNCTION} must take the dependency target subset from "
            f"{_OWNER_FUNCTION}(...).target_subset ({_OWNER})",
        )
    ]


def _check_call(
    path: str,
    index: TreeIndex,
    call: ast.Call,
) -> list[Violation]:
    function = _enclosing_function(index, call)
    site = (path, _qualname(index, function))
    owner_names = _owner_bound_names(index, function)
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    findings: list[Violation] = []

    def report(message: str) -> None:
        findings.append(_report(path, call, message))

    if None in keywords:
        report("Deploy calls must pass skill_subset= explicitly, not through **kwargs")
        return findings
    if "skill_subset" not in keywords:
        report(f"Deploy call omits skill_subset=; route it through {_OWNER_FUNCTION} ({_OWNER})")
        return findings
    for keyword, field in _SELECTION_FIELDS.items():
        value = keywords.get(keyword)
        if value is None:
            continue
        if _from_owner(value, field, owner_names):
            continue
        if site in _FORWARDERS and isinstance(value, ast.Name) and value.id == keyword:
            continue
        if keyword == "skill_subset" and site in _PLAN_DERIVED:
            continue
        if (
            keyword == "skill_subset"
            and site in _EXPLICIT_NONE
            and isinstance(value, ast.Constant)
            and value.value is None
        ):
            continue
        report(
            f"Deploy call computes {keyword}= locally; use "
            f"{_OWNER_FUNCTION}(...).{field} ({_OWNER})"
        )
    return findings


def _required_keyword_failures(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path, qualname in _REQUIRED_KEYWORD:
        index = provider.tree_index(path)
        function = index.function(qualname) if index is not None else None
        args = getattr(function, "args", None)
        names = [arg.arg for arg in args.kwonlyargs] if args is not None else []
        required = "skill_subset" in names and args.kw_defaults[names.index("skill_subset")] is None
        if not required:
            findings.append(
                _summary(
                    GUARD_EFFECTIVE_SELECTION,
                    path,
                    f"{qualname} must take skill_subset as a required keyword-only argument",
                )
            )
    return findings


def check_effective_selection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every deploy call takes its selection from the one owner."""
    findings = _required_keyword_failures(provider)
    owner_definitions: list[str] = []
    tracked = _DEPLOY_FUNCTIONS | {_TARGET_FUNCTION, _OWNER_INTERNAL}
    for path in _python_paths(provider, _SRC_PREFIX):
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        names = _aliases(index, tracked)
        for node in index.walk(index.root):
            if isinstance(node, ast.FunctionDef) and node.name == _OWNER_FUNCTION:
                owner_definitions.append(path)
            called = _called(node, names)
            if called is None:
                continue
            if called == _TARGET_FUNCTION:
                findings.extend(_check_target_call(path, index, node))
            elif called == _OWNER_INTERNAL:
                if path != _OWNER:
                    findings.append(
                        _report(
                            path,
                            node,
                            f"{_OWNER_INTERNAL} is internal to {_OWNER}; "
                            f"call {_OWNER_FUNCTION}(...) instead",
                        )
                    )
            else:
                findings.extend(_check_call(path, index, node))
    if owner_definitions != [_OWNER]:
        findings.append(
            _summary(
                GUARD_EFFECTIVE_SELECTION,
                _OWNER,
                f"{_OWNER_FUNCTION} must be defined exactly once, in {_OWNER}",
            )
        )
    return tuple(findings)


__all__ = ["GUARD_EFFECTIVE_SELECTION", "check_effective_selection"]
