"""Integrate-phase, install/uninstall-command, and hook-reconciliation
projection-boundary checks (second third).

Part of the facts-only port of
``scripts/check_agent_plugin_projection_boundary.py`` (legacy bundle-format
subcheck **B20**); see :mod:`agent_plugin_projection` for the entry point
that composes every ``_check_*(ctx)`` boundary check here.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.agent_plugin_boundary_checks_a import _check_ordered_gate
from scripts.architecture_linter.checks.agent_plugin_scan_primitives import (
    _Boundary,
    _call_lines,
    _call_name,
    _function_calls,
    _is_call_statement,
    _lines_for,
    _named_functions,
)
from scripts.architecture_linter.checks.agent_plugin_shared import (
    _BOUNDARY_OWNER,
    _SURVIVOR_PREFLIGHT,
    CI_CHECKS,
    HOOK_INTEGRATOR,
    INSTALL_COMMAND,
    INTEGRATE_PHASE,
    LOCAL_BUNDLE_HANDLER,
    PRUNE_COMMAND,
    UNINSTALL_CLI,
    UNINSTALL_ENGINE,
)
from scripts.architecture_linter.models import Violation


def _check_integrate_phase(ctx: _Boundary) -> tuple[Violation, ...]:
    """install/phases/integrate.py: batch preflight precedes the first integration."""
    return _check_ordered_gate(
        ctx,
        path=INTEGRATE_PHASE,
        owner="run",
        gate="preflight_agent_plugin_materializations",
        followers=("run_integration_template",),
        single_owner_message="integration phase must have one run owner",
        order_message="native batch preflight must run before the first package integration",
    )


def _check_install_command(ctx: _Boundary) -> tuple[Violation, ...]:
    """commands/install.py: dry-run preflight ordering and typed failure rendering."""
    index = ctx.index(INSTALL_COMMAND)
    findings: list[Violation] = []

    findings.extend(
        _check_ordered_gate(
            ctx,
            path=INSTALL_COMMAND,
            owner="_install_apm_packages",
            gate="preflight_agent_plugin_dry_run",
            followers=("render_and_exit",),
            single_owner_message="install planning must have one dependency owner",
            order_message="dry-run native preflight must run before rendering success",
        )
    )

    install_commands = _named_functions(index, "install")
    typed_bundle_failure = False
    if len(install_commands) == 1:
        for node in index.walk(install_commands[0]):
            if not isinstance(node, ast.Try):
                continue
            typed_handlers = [
                handler
                for handler in node.handlers
                if isinstance(handler.type, ast.Name) and handler.type.id == "AgentPluginError"
            ]
            generic_handlers = [
                handler
                for handler in node.handlers
                if isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
            ]
            if len(typed_handlers) != 1 or not generic_handlers:
                continue
            typed_handler = typed_handlers[0]
            typed_bundle_failure = (
                typed_handler.lineno < min(handler.lineno for handler in generic_handlers)
                and "logger.error" in _function_calls(index, typed_handler)
                and any(
                    isinstance(item, ast.Attribute) and item.attr == "FAILED"
                    for item in index.walk(typed_handler)
                )
            )
            if typed_bundle_failure:
                break
    if not typed_bundle_failure:
        findings.append(
            ctx.report(
                INSTALL_COMMAND,
                "typed native bundle failures must render through logger.error before "
                "the generic exception handler",
            )
        )
    return tuple(findings)


def _check_local_bundle_handler(ctx: _Boundary) -> tuple[Violation, ...]:
    """install/local_bundle_handler.py: the boundary runs unconditionally, first."""
    index = ctx.index(LOCAL_BUNDLE_HANDLER)
    handlers = _named_functions(index, "install_local_bundle")
    if len(handlers) != 1:
        return (
            ctx.report(LOCAL_BUNDLE_HANDLER, "local bundle handler must have one install owner"),
        )

    handler = handlers[0]
    calls = _call_lines(index, handler)
    gates = _lines_for(calls, (_BOUNDARY_OWNER,))
    gate_tries = [
        statement
        for statement in handler.body
        if isinstance(statement, ast.Try) and _BOUNDARY_OWNER in _function_calls(index, statement)
    ]
    unconditional_gate = (
        len(gate_tries) == 1
        and bool(gate_tries[0].body)
        and _is_call_statement(gate_tries[0].body[0], _BOUNDARY_OWNER)
    )
    side_effects = _lines_for(
        calls, ("resolve_targets", "run_policy_preflight", "integrate_local_bundle")
    )
    if (
        len(gates) != 1
        or not unconditional_gate
        or not side_effects
        or gates[0] >= min(side_effects)
    ):
        return (
            ctx.report(
                LOCAL_BUNDLE_HANDLER,
                "native local bundles must fail before resolution or deployment",
                line=handler.lineno,
            ),
        )
    return ()


def _check_drift_translation(ctx: _Boundary) -> tuple[Violation, ...]:
    """policy/ci_checks.py: a boundary failure becomes a failed CheckResult."""
    index = ctx.index(CI_CHECKS)
    drift_defs = _named_functions(index, "_check_drift")
    if len(drift_defs) != 1:
        return (ctx.report(CI_CHECKS, "drift check must have one structured owner"),)

    typed_handlers = [
        node
        for node in index.walk(drift_defs[0])
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "AgentPluginDeploymentBoundaryError"
    ]
    structured_failure = bool(typed_handlers) and any(
        isinstance(node, ast.keyword)
        and node.arg == "passed"
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        for node in index.walk(typed_handlers[0])
    )
    if len(typed_handlers) != 1 or not structured_failure:
        return (
            ctx.report(
                CI_CHECKS,
                "drift must translate native deployment failures into a failed CheckResult",
                line=drift_defs[0].lineno,
            ),
        )
    return ()


def _check_uninstall_survivor_preflight(ctx: _Boundary) -> tuple[Violation, ...]:
    """uninstall/engine.py: survivors preflight through the boundary owner."""
    index = ctx.index(UNINSTALL_ENGINE)
    preflights = _named_functions(index, "_preflight_uninstall_survivors")
    installed_survivor_gate = False
    declared_source_gate = False
    if len(preflights) == 1:
        preflight = preflights[0]
        calls = _function_calls(index, preflight)
        installed_survivor_gate = _SURVIVOR_PREFLIGHT in calls
        for node in index.walk(preflight):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) == _BOUNDARY_OWNER and node.args:
                declared_source_gate |= (
                    isinstance(node.args[0], ast.Call) and _call_name(node.args[0]) == "PackageInfo"
                )
        declared_source_gate &= "validate_apm_package" in calls
    if len(preflights) != 1 or not installed_survivor_gate or not declared_source_gate:
        return (
            ctx.report(
                UNINSTALL_ENGINE,
                "uninstall survivor preflight must use the native deployment boundary "
                "owner against declared local sources",
            ),
        )
    return ()


def _check_prune_survivor_preflight(ctx: _Boundary) -> tuple[Violation, ...]:
    """commands/prune.py: survivors preflight before any destructive mutation."""
    index = ctx.index(PRUNE_COMMAND)
    prune_preflights = _named_functions(index, "_preflight_prune_survivors")
    prune_defs = _named_functions(index, "prune")
    prune_helper_uses_owner = len(prune_preflights) == 1 and _SURVIVOR_PREFLIGHT in (
        _function_calls(index, prune_preflights[0])
    )
    prune_precedes_mutation = False
    if len(prune_defs) == 1:
        calls = _call_lines(index, prune_defs[0])
        gates = _lines_for(calls, ("_preflight_prune_survivors",))
        mutations = _lines_for(
            calls,
            (
                "safe_rmtree",
                "remove_stale_deployed_files",
                "DeploymentLedgerCodec.reconcile_owner_references",
                "lockfile.write",
                "HookIntegrator().reconcile_after_removal",
            ),
        )
        prune_precedes_mutation = bool(len(gates) == 1 and mutations and gates[0] < min(mutations))
    if not prune_helper_uses_owner or not prune_precedes_mutation:
        return (
            ctx.report(
                PRUNE_COMMAND,
                "prune must preflight survivors through the native deployment boundary "
                "before mutation",
            ),
        )
    return ()


def _check_hook_reconciliation(ctx: _Boundary) -> tuple[Violation, ...]:
    """integration/hook_integrator.py: direct reconciliation preflights first."""
    index = ctx.index(HOOK_INTEGRATOR)
    reconciles = _named_functions(index, "reconcile_after_removal")
    hook_reconcile_is_guarded = False
    if len(reconciles) == 1:
        calls = _call_lines(index, reconciles[0])
        gates = _lines_for(calls, (_SURVIVOR_PREFLIGHT,))
        mutations = _lines_for(calls, ("self.sync_integration", "self.integrate_hooks_for_target"))
        hook_reconcile_is_guarded = bool(
            len(gates) == 1 and mutations and gates[0] < min(mutations)
        )
    if not hook_reconcile_is_guarded:
        return (
            ctx.report(
                HOOK_INTEGRATOR,
                "direct hook survivor reconciliation must preflight before mutation",
            ),
        )
    return ()


def _check_uninstall_command(ctx: _Boundary) -> tuple[Violation, ...]:
    """uninstall/cli.py + engine.py: preflight precedes scripts, staging, and sync."""
    findings: list[Violation] = []
    findings.extend(
        _check_ordered_gate(
            ctx,
            path=UNINSTALL_CLI,
            owner="uninstall",
            gate="_preflight_uninstall_survivors",
            followers=(
                "_fire_uninstall_scripts",
                "_stage_shared_local_survivors",
                "dump_yaml_roundtrip",
                "_remove_packages_from_disk",
                "_sync_integrations_after_uninstall",
            ),
            single_owner_message="uninstall command must have one owner",
            order_message=(
                "uninstall survivor preflight must run before scripts, staging, or "
                "destructive reconciliation"
            ),
            exact_gate=False,
        )
    )
    findings.extend(
        _check_ordered_gate(
            ctx,
            path=UNINSTALL_ENGINE,
            owner="_sync_integrations_after_uninstall",
            gate="_preflight_uninstall_survivors",
            followers=(
                "clear_discovery_cache",
                "sync_for_target",
                "sync_integration",
                "integrate_package_skill",
            ),
            single_owner_message="uninstall integration sync must have one owner",
            order_message=(
                "direct uninstall sync must preflight survivors before integration mutation"
            ),
            exact_gate=False,
        )
    )
    return tuple(findings)
