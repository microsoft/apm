"""Deployment-boundary, survivor-preflight, and integration-template
projection-boundary checks (first third).

Part of the facts-only port of
``scripts/check_agent_plugin_projection_boundary.py`` (legacy bundle-format
subcheck **B20**); see :mod:`agent_plugin_projection` for the entry point
that composes every ``_check_*(ctx)`` boundary check here.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.agent_plugin_scan_primitives import (
    _assigns_subscript_value,
    _Boundary,
    _bundle_format_attributes,
    _call_lines,
    _first_executable_statement,
    _function_calls,
    _is_call_statement,
    _is_native_package_predicate,
    _lines_for,
    _named_functions,
    _raise_name,
)
from scripts.architecture_linter.checks.agent_plugin_shared import (
    _BOUNDARY_OWNER,
    _SURVIVOR_PREFLIGHT,
    ERRORS,
    TEMPLATE,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.models import Violation

_FAIL_CLOSED_BOUNDARY_RAISES = (
    "AgentPluginDeploymentBoundaryError",
    "AgentPluginTargetExcludedError",
)


def _check_deployment_boundary_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """Native deployment boundary: typed error, one owner, fail-closed shape."""
    index = ctx.index(ERRORS)
    root = index.root
    findings: list[Violation] = []

    deployment_errors = [
        node
        for node in (index.children(root) if root is not None else [])
        if isinstance(node, ast.ClassDef)
        and node.name == "AgentPluginDeploymentBoundaryError"
        and len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and node.bases[0].id == "AgentPluginError"
    ]
    if len(deployment_errors) != 1:
        findings.append(
            ctx.report(ERRORS, "AgentPluginDeploymentBoundaryError must extend AgentPluginError")
        )

    boundary_defs = _named_functions(index, _BOUNDARY_OWNER)
    if len(boundary_defs) != 1:
        findings.append(
            ctx.report(ERRORS, "native deployment boundary must have exactly one definition")
        )
    else:
        findings.extend(_check_boundary_body(ctx, index, boundary_defs[0]))
    return tuple(findings)


def _check_boundary_body(
    ctx: _Boundary, index: TreeIndex, boundary_def: ast.FunctionDef | ast.AsyncFunctionDef
) -> tuple[Violation, ...]:
    """The nine fail-closed conditions the boundary body must satisfy at once."""
    typed_raises = [
        node
        for node in index.walk(boundary_def)
        if isinstance(node, ast.Raise) and _raise_name(node) == "AgentPluginDeploymentBoundaryError"
    ]
    native_package_guards = [
        node
        for node in index.walk(boundary_def)
        if isinstance(node, ast.If) and _is_native_package_predicate(node.test)
    ]
    has_attached_ir_check = any(
        isinstance(node, ast.Constant) and node.value == "agent_plugin"
        for node in index.walk(boundary_def)
    )
    has_native_bundle_check = _bundle_format_attributes(index, boundary_def)
    native_bundle_guards = [
        node
        for node in index.walk(boundary_def)
        if isinstance(node, ast.If) and _bundle_format_attributes(index, node.test)
    ]
    bundle_fails_closed = len(native_bundle_guards) == 1 and any(
        isinstance(node, ast.Raise) and _raise_name(node) == "AgentPluginDeploymentBoundaryError"
        for node in index.walk(native_bundle_guards[0])
    )
    package_fails_closed = (
        bool(boundary_def.body)
        and isinstance(boundary_def.body[-1], ast.Raise)
        and _raise_name(boundary_def.body[-1]) in _FAIL_CLOSED_BOUNDARY_RAISES
    )
    admission_guards = [
        node
        for node in index.walk(boundary_def)
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Attribute) and item.attr == "supported"
            for item in index.walk(node.test)
        )
    ]
    # Native admission must be a conjunction of "a capability was published"
    # AND "it is supported". A disjunction would admit every native package
    # whenever no capability was resolved -- the exact fail-open shape this
    # boundary exists to prevent.
    admission_is_conjunctive = len(admission_guards) == 1 and all(
        isinstance(guard.test, ast.BoolOp) and isinstance(guard.test.op, ast.And)
        for guard in admission_guards
    )
    consults_capability_owner = "current_native_registration" in _function_calls(
        index, boundary_def
    )
    if (
        len(native_package_guards) != 1
        or not native_package_guards[0].body
        or not isinstance(native_package_guards[0].body[0], ast.Return)
        or not typed_raises
        or not has_attached_ir_check
        or not has_native_bundle_check
        or not bundle_fails_closed
        or not package_fails_closed
        or not admission_is_conjunctive
        or not consults_capability_owner
    ):
        return (
            ctx.report(
                ERRORS,
                "native deployment boundary must fail closed on AGENT_PLUGIN packages, "
                "bundles, and missing canonical IR",
                line=boundary_def.lineno,
            ),
        )
    return ()


def _check_survivor_preflight_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """Survivor reintegration preflight has one owner and uses the boundary."""
    index = ctx.index(ERRORS)
    survivor_preflights = _named_functions(index, _SURVIVOR_PREFLIGHT)
    if len(survivor_preflights) != 1:
        return (ctx.report(ERRORS, "survivor reintegration must have one preflight owner"),)
    survivor_calls = _function_calls(index, survivor_preflights[0])
    if (
        "build_installed_package_info" not in survivor_calls
        or _BOUNDARY_OWNER not in survivor_calls
    ):
        return (
            ctx.report(
                ERRORS,
                "survivor reintegration preflight must use the native deployment boundary owner",
                line=survivor_preflights[0].lineno,
            ),
        )
    return ()


def _check_first_action_gate(
    ctx: _Boundary, path: str, function: str, message: str
) -> tuple[Violation, ...]:
    """One function must open with a bare call to the deployment boundary owner."""
    index = ctx.index(path)
    definitions = _named_functions(index, function)
    if len(definitions) != 1 or not _is_call_statement(
        _first_executable_statement(definitions[0]) if definitions else None,
        _BOUNDARY_OWNER,
    ):
        line = definitions[0].lineno if len(definitions) == 1 else 1
        return (ctx.report(path, message, line=line),)
    return ()


def _check_ordered_gate(
    ctx: _Boundary,
    *,
    path: str,
    owner: str,
    gate: str,
    followers: Iterable[str],
    single_owner_message: str,
    order_message: str,
    exact_gate: bool = True,
) -> tuple[Violation, ...]:
    """`gate` must be called inside `owner` strictly before any of `followers`."""
    index = ctx.index(path)
    definitions = _named_functions(index, owner)
    if len(definitions) != 1:
        return (ctx.report(path, single_owner_message),)
    calls = _call_lines(index, definitions[0])
    gates = _lines_for(calls, (gate,))
    later = _lines_for(calls, followers)
    gate_count_ok = len(gates) == 1 if exact_gate else bool(gates)
    if not gate_count_ok or not later or min(gates) >= min(later):
        return (ctx.report(path, order_message, line=definitions[0].lineno),)
    return ()


def _check_integration_template(ctx: _Boundary) -> tuple[Violation, ...]:
    """install/template.py: gate ordering, batch preflight, recorded failure, dry run."""
    index = ctx.index(TEMPLATE)
    findings: list[Violation] = []

    findings.extend(
        _check_ordered_gate(
            ctx,
            path=TEMPLATE,
            owner="_integrate_materialization",
            gate=_BOUNDARY_OWNER,
            followers=(
                "SkillIntegrator.available_skill_names",
                "_pre_deploy_security_scan",
                "integrate_package_primitives",
            ),
            single_owner_message="_integrate_materialization must have exactly one definition",
            order_message=("native deployment gate must precede selective and generic integration"),
        )
    )

    batch_preflight_defs = _named_functions(index, "preflight_agent_plugin_materializations")
    if len(batch_preflight_defs) != 1 or (
        _BOUNDARY_OWNER not in _function_calls(index, batch_preflight_defs[0])
    ):
        findings.append(
            ctx.report(TEMPLATE, "native batch preflight must use the deployment boundary owner")
        )

    findings.extend(_check_boundary_failure_recording(ctx, index))

    dry_run_preflights = _named_functions(index, "preflight_agent_plugin_dry_run")
    dry_run_fails_closed = False
    if len(dry_run_preflights) == 1:
        calls = _function_calls(index, dry_run_preflights[0])
        dry_run_fails_closed = (
            "route_agent_plugin_package" in calls
            and "validate_apm_package" in calls
            and _BOUNDARY_OWNER in calls
            and "normalize_plugin_directory" not in calls
        )
    if not dry_run_fails_closed:
        findings.append(
            ctx.report(
                TEMPLATE,
                "dry-run must route schema admission and fail at the native deployment boundary",
            )
        )
    return tuple(findings)


def _check_boundary_failure_recording(ctx: _Boundary, index: TreeIndex) -> tuple[Violation, ...]:
    """A boundary failure stays one owned diagnostic and a recorded non-success."""
    diagnostic_recorders = _named_functions(index, "_record_agent_plugin_boundary_diagnostic")
    failure_recorders = _named_functions(index, "_record_agent_plugin_boundary_failure")
    if len(diagnostic_recorders) != 1 or len(failure_recorders) != 1:
        return (ctx.report(TEMPLATE, "native deployment failure must have one diagnostic owner"),)

    diagnostic_recorder = diagnostic_recorders[0]
    recorder = failure_recorders[0]
    calls = _function_calls(index, recorder)
    failure_is_recorded = (
        "diagnostics.error" in _function_calls(index, diagnostic_recorder)
        and "_record_agent_plugin_boundary_diagnostic" in calls
        and _assigns_subscript_value(index, recorder, owner="deltas", key="installed", value=0)
        and any(
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Attribute)
            and node.targets[0].value.attr == "package_deployed_files"
            and isinstance(node.value, ast.List)
            and not node.value.elts
            for node in index.walk(recorder)
        )
    )
    if not failure_is_recorded:
        return (
            ctx.report(
                TEMPLATE,
                "native deployment failure must remain a recorded non-success outcome",
                line=recorder.lineno,
            ),
        )
    return ()
