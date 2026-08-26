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


def _first_executable_statement(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.stmt | None:
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def _is_call_statement(node: ast.AST | None, name: str) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == name
    )


def _named_functions(
    tree: ast.Module,
    name: str,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in _functions(tree) if node.name == name]


def _is_native_package_predicate(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "package_type"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Attribute)
        and node.comparators[0].attr == "AGENT_PLUGIN"
        and isinstance(node.comparators[0].value, ast.Name)
        and node.comparators[0].value.id == "PackageType"
    )


def _raise_name(node: ast.Raise) -> str:
    if not isinstance(node.exc, ast.Call):
        return ""
    return _call_name(node.exc)


def _assigns_subscript_value(
    node: ast.AST,
    *,
    owner: str,
    key: str,
    value: object,
) -> bool:
    return any(
        isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Subscript)
        and isinstance(item.targets[0].value, ast.Name)
        and item.targets[0].value.id == owner
        and isinstance(item.targets[0].slice, ast.Constant)
        and item.targets[0].slice.value == key
        and isinstance(item.value, ast.Constant)
        and item.value.value == value
        for item in ast.walk(node)
    )


def check(root: Path) -> list[str]:  # noqa: C901, PLR0912, PLR0915
    """Return projection-boundary violations under one repository root."""
    source_root = root / "src" / "apm_cli"
    projection_path = source_root / "agent_plugins" / "projection.py"
    package_path = source_root / "models" / "apm_package.py"
    validation_path = source_root / "models" / "validation.py"
    resolver_path = source_root / "deps" / "apm_resolver.py"
    errors_path = source_root / "agent_plugins" / "errors.py"
    services_path = source_root / "install" / "services.py"
    template_path = source_root / "install" / "template.py"
    integrate_phase_path = source_root / "install" / "phases" / "integrate.py"
    local_bundle_handler_path = source_root / "install" / "local_bundle_handler.py"
    ci_checks_path = source_root / "policy" / "ci_checks.py"
    uninstall_cli_path = source_root / "commands" / "uninstall" / "cli.py"
    uninstall_engine_path = source_root / "commands" / "uninstall" / "engine.py"
    install_command_path = source_root / "commands" / "install.py"
    prune_command_path = source_root / "commands" / "prune.py"
    hook_integrator_path = source_root / "integration" / "hook_integrator.py"
    skill_integrator_path = source_root / "integration" / "skill_integrator.py"
    skill_routing_path = source_root / "integration" / "skill_package_routing.py"
    required = (
        projection_path,
        package_path,
        validation_path,
        resolver_path,
        errors_path,
        services_path,
        template_path,
        integrate_phase_path,
        local_bundle_handler_path,
        ci_checks_path,
        uninstall_cli_path,
        uninstall_engine_path,
        install_command_path,
        prune_command_path,
        hook_integrator_path,
        skill_integrator_path,
        skill_routing_path,
    )
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
    errors_tree = parsed.get(errors_path)
    services_tree = parsed.get(services_path)
    template_tree = parsed.get(template_path)
    integrate_phase_tree = parsed.get(integrate_phase_path)
    local_bundle_handler_tree = parsed.get(local_bundle_handler_path)
    ci_checks_tree = parsed.get(ci_checks_path)
    uninstall_cli_tree = parsed.get(uninstall_cli_path)
    uninstall_engine_tree = parsed.get(uninstall_engine_path)
    install_command_tree = parsed.get(install_command_path)
    prune_command_tree = parsed.get(prune_command_path)
    hook_integrator_tree = parsed.get(hook_integrator_path)
    skill_integrator_tree = parsed.get(skill_integrator_path)
    skill_routing_tree = parsed.get(skill_routing_path)
    if (
        projection_tree is None
        or package_tree is None
        or validation_tree is None
        or resolver_tree is None
        or errors_tree is None
        or services_tree is None
        or template_tree is None
        or integrate_phase_tree is None
        or local_bundle_handler_tree is None
        or ci_checks_tree is None
        or uninstall_cli_tree is None
        or uninstall_engine_tree is None
        or install_command_tree is None
        or prune_command_tree is None
        or hook_integrator_tree is None
        or skill_integrator_tree is None
        or skill_routing_tree is None
    ):
        return violations

    deployment_errors = [
        node
        for node in errors_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "AgentPluginDeploymentBoundaryError"
        and len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and node.bases[0].id == "AgentPluginError"
    ]
    if len(deployment_errors) != 1:
        violations.append(
            f"{errors_path}: AgentPluginDeploymentBoundaryError must extend AgentPluginError"
        )

    boundary_defs = _named_functions(
        errors_tree,
        "enforce_agent_plugin_deployment_boundary",
    )
    if len(boundary_defs) != 1:
        violations.append(
            f"{errors_path}: native deployment boundary must have exactly one definition"
        )
    else:
        boundary_def = boundary_defs[0]
        typed_raises = [
            node
            for node in ast.walk(boundary_def)
            if isinstance(node, ast.Raise)
            and _raise_name(node) == "AgentPluginDeploymentBoundaryError"
        ]
        native_package_guards = [
            node
            for node in ast.walk(boundary_def)
            if isinstance(node, ast.If) and _is_native_package_predicate(node.test)
        ]
        has_attached_ir_check = any(
            isinstance(node, ast.Constant) and node.value == "agent_plugin"
            for node in ast.walk(boundary_def)
        )
        has_native_bundle_check = any(
            isinstance(node, ast.Attribute)
            and node.attr == "AGENT_PLUGIN"
            and isinstance(node.value, ast.Name)
            and node.value.id == "BundleFormat"
            for node in ast.walk(boundary_def)
        )
        native_bundle_guards = [
            node
            for node in ast.walk(boundary_def)
            if isinstance(node, ast.If)
            and any(
                isinstance(item, ast.Attribute)
                and item.attr == "AGENT_PLUGIN"
                and isinstance(item.value, ast.Name)
                and item.value.id == "BundleFormat"
                for item in ast.walk(node.test)
            )
        ]
        bundle_fails_closed = len(native_bundle_guards) == 1 and any(
            isinstance(node, ast.Raise)
            and _raise_name(node) == "AgentPluginDeploymentBoundaryError"
            for node in ast.walk(native_bundle_guards[0])
        )
        package_fails_closed = (
            bool(boundary_def.body)
            and isinstance(boundary_def.body[-1], ast.Raise)
            and _raise_name(boundary_def.body[-1]) == "AgentPluginDeploymentBoundaryError"
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
        ):
            violations.append(
                f"{errors_path}: native deployment boundary must fail closed on "
                "AGENT_PLUGIN packages, bundles, and missing canonical IR"
            )

    integration_defs = _named_functions(services_tree, "integrate_package_primitives")
    if len(integration_defs) != 1 or not _is_call_statement(
        _first_executable_statement(integration_defs[0]) if integration_defs else None,
        "enforce_agent_plugin_deployment_boundary",
    ):
        violations.append(
            f"{services_path}: native deployment gate must be the first integration action"
        )

    survivor_preflights = _named_functions(
        errors_tree,
        "preflight_reintegration_survivors",
    )
    if len(survivor_preflights) != 1:
        violations.append(f"{errors_path}: survivor reintegration must have one preflight owner")
    else:
        survivor_calls = _function_calls(survivor_preflights[0])
        if (
            "build_installed_package_info" not in survivor_calls
            or "enforce_agent_plugin_deployment_boundary" not in survivor_calls
        ):
            violations.append(
                f"{errors_path}: survivor reintegration preflight must use "
                "the native deployment boundary owner"
            )

    template_defs = _named_functions(template_tree, "_integrate_materialization")
    if len(template_defs) != 1:
        violations.append(
            f"{template_path}: _integrate_materialization must have exactly one definition"
        )
    else:
        template_calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(template_defs[0])
            if isinstance(node, ast.Call)
        ]
        gate_lines = [
            line
            for name, line in template_calls
            if name == "enforce_agent_plugin_deployment_boundary"
        ]
        later_boundary_calls = [
            line
            for name, line in template_calls
            if name
            in {
                "SkillIntegrator.available_skill_names",
                "_pre_deploy_security_scan",
                "integrate_package_primitives",
            }
        ]
        if (
            len(gate_lines) != 1
            or not later_boundary_calls
            or gate_lines[0] >= min(later_boundary_calls)
        ):
            violations.append(
                f"{template_path}: native deployment gate must precede selective and "
                "generic integration"
            )
    batch_preflight_defs = _named_functions(
        template_tree,
        "preflight_agent_plugin_materializations",
    )
    if len(batch_preflight_defs) != 1 or (
        "enforce_agent_plugin_deployment_boundary" not in _function_calls(batch_preflight_defs[0])
    ):
        violations.append(
            f"{template_path}: native batch preflight must use the deployment boundary owner"
        )
    diagnostic_recorders = _named_functions(
        template_tree,
        "_record_agent_plugin_boundary_diagnostic",
    )
    failure_recorders = _named_functions(template_tree, "_record_agent_plugin_boundary_failure")
    if len(diagnostic_recorders) != 1 or len(failure_recorders) != 1:
        violations.append(
            f"{template_path}: native deployment failure must have one diagnostic owner"
        )
    else:
        diagnostic_recorder = diagnostic_recorders[0]
        recorder = failure_recorders[0]
        calls = _function_calls(recorder)
        failure_is_recorded = (
            "diagnostics.error" in _function_calls(diagnostic_recorder)
            and "_record_agent_plugin_boundary_diagnostic" in calls
            and _assigns_subscript_value(
                recorder,
                owner="deltas",
                key="installed",
                value=0,
            )
            and any(
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Attribute)
                and node.targets[0].value.attr == "package_deployed_files"
                and isinstance(node.value, ast.List)
                and not node.value.elts
                for node in ast.walk(recorder)
            )
        )
        if not failure_is_recorded:
            violations.append(
                f"{template_path}: native deployment failure must remain a recorded "
                "non-success outcome"
            )
    dry_run_preflights = _named_functions(template_tree, "preflight_agent_plugin_dry_run")
    dry_run_fails_closed = False
    if len(dry_run_preflights) == 1:
        dry_run_preflight = dry_run_preflights[0]
        calls = _function_calls(dry_run_preflight)
        dry_run_fails_closed = (
            "route_agent_plugin_package" in calls
            and "validate_apm_package" in calls
            and "enforce_agent_plugin_deployment_boundary" in calls
            and "normalize_plugin_directory" not in calls
        )
    if not dry_run_fails_closed:
        violations.append(
            f"{template_path}: dry-run must route schema admission and fail at "
            "the native deployment boundary"
        )

    integrate_runs = _named_functions(integrate_phase_tree, "run")
    if len(integrate_runs) != 1:
        violations.append(f"{integrate_phase_path}: integration phase must have one run owner")
    else:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(integrate_runs[0])
            if isinstance(node, ast.Call)
        ]
        batch_gates = [
            line for name, line in calls if name == "preflight_agent_plugin_materializations"
        ]
        integration_calls = [line for name, line in calls if name == "run_integration_template"]
        if (
            len(batch_gates) != 1
            or not integration_calls
            or batch_gates[0] >= min(integration_calls)
        ):
            violations.append(
                f"{integrate_phase_path}: native batch preflight must run before "
                "the first package integration"
            )

    install_helpers = _named_functions(install_command_tree, "_install_apm_packages")
    if len(install_helpers) != 1:
        violations.append(
            f"{install_command_path}: install planning must have one dependency owner"
        )
    else:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(install_helpers[0])
            if isinstance(node, ast.Call)
        ]
        dry_run_gates = [line for name, line in calls if name == "preflight_agent_plugin_dry_run"]
        dry_run_exits = [line for name, line in calls if name == "render_and_exit"]
        if len(dry_run_gates) != 1 or not dry_run_exits or dry_run_gates[0] >= min(dry_run_exits):
            violations.append(
                f"{install_command_path}: dry-run native preflight must run before "
                "rendering success"
            )

    install_commands = _named_functions(install_command_tree, "install")
    typed_bundle_failure = False
    if len(install_commands) == 1:
        for node in ast.walk(install_commands[0]):
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
                and "logger.error" in _function_calls(typed_handler)
                and any(
                    isinstance(item, ast.Attribute) and item.attr == "FAILED"
                    for item in ast.walk(typed_handler)
                )
            )
            if typed_bundle_failure:
                break
    if not typed_bundle_failure:
        violations.append(
            f"{install_command_path}: typed native bundle failures must render through "
            "logger.error before the generic exception handler"
        )

    local_bundle_defs = _named_functions(services_tree, "integrate_local_bundle")
    if len(local_bundle_defs) != 1 or not _is_call_statement(
        _first_executable_statement(local_bundle_defs[0]) if local_bundle_defs else None,
        "enforce_agent_plugin_deployment_boundary",
    ):
        violations.append(
            f"{services_path}: opaque local bundle deployment must start at the native boundary"
        )

    local_bundle_handlers = _named_functions(local_bundle_handler_tree, "install_local_bundle")
    if len(local_bundle_handlers) != 1:
        violations.append(
            f"{local_bundle_handler_path}: local bundle handler must have one install owner"
        )
    else:
        handler = local_bundle_handlers[0]
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
        ]
        gates = [line for name, line in calls if name == "enforce_agent_plugin_deployment_boundary"]
        gate_tries = [
            statement
            for statement in handler.body
            if isinstance(statement, ast.Try)
            and "enforce_agent_plugin_deployment_boundary" in _function_calls(statement)
        ]
        unconditional_gate = (
            len(gate_tries) == 1
            and bool(gate_tries[0].body)
            and _is_call_statement(
                gate_tries[0].body[0],
                "enforce_agent_plugin_deployment_boundary",
            )
        )
        side_effects = [
            line
            for name, line in calls
            if name
            in {
                "resolve_targets",
                "run_policy_preflight",
                "integrate_local_bundle",
            }
        ]
        if (
            len(gates) != 1
            or not unconditional_gate
            or not side_effects
            or gates[0] >= min(side_effects)
        ):
            violations.append(
                f"{local_bundle_handler_path}: native local bundles must fail before "
                "resolution or deployment"
            )

    drift_defs = _named_functions(ci_checks_tree, "_check_drift")
    if len(drift_defs) != 1:
        violations.append(f"{ci_checks_path}: drift check must have one structured owner")
    else:
        typed_handlers = [
            node
            for node in ast.walk(drift_defs[0])
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "AgentPluginDeploymentBoundaryError"
        ]
        structured_failure = bool(typed_handlers) and any(
            isinstance(node, ast.keyword)
            and node.arg == "passed"
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
            for node in ast.walk(typed_handlers[0])
        )
        if len(typed_handlers) != 1 or not structured_failure:
            violations.append(
                f"{ci_checks_path}: drift must translate native deployment failures "
                "into a failed CheckResult"
            )

    uninstall_preflights = _named_functions(
        uninstall_engine_tree,
        "_preflight_uninstall_survivors",
    )
    installed_survivor_gate = False
    declared_source_gate = False
    if len(uninstall_preflights) == 1:
        preflight = uninstall_preflights[0]
        installed_survivor_gate = "preflight_reintegration_survivors" in _function_calls(preflight)
        for node in ast.walk(preflight):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) == "enforce_agent_plugin_deployment_boundary" and node.args:
                declared_source_gate |= (
                    isinstance(node.args[0], ast.Call) and _call_name(node.args[0]) == "PackageInfo"
                )
        declared_source_gate &= "validate_apm_package" in _function_calls(preflight)
    if len(uninstall_preflights) != 1 or not installed_survivor_gate or not declared_source_gate:
        violations.append(
            f"{uninstall_engine_path}: uninstall survivor preflight must use "
            "the native deployment boundary owner against declared local sources"
        )

    prune_preflights = _named_functions(prune_command_tree, "_preflight_prune_survivors")
    prune_defs = _named_functions(prune_command_tree, "prune")
    prune_helper_uses_owner = len(
        prune_preflights
    ) == 1 and "preflight_reintegration_survivors" in _function_calls(prune_preflights[0])
    prune_precedes_mutation = False
    if len(prune_defs) == 1:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(prune_defs[0])
            if isinstance(node, ast.Call)
        ]
        gates = [line for name, line in calls if name == "_preflight_prune_survivors"]
        mutations = [
            line
            for name, line in calls
            if name
            in {
                "safe_rmtree",
                "remove_stale_deployed_files",
                "DeploymentLedgerCodec.reconcile_owner_references",
                "lockfile.write",
                "HookIntegrator().reconcile_after_removal",
            }
        ]
        prune_precedes_mutation = bool(len(gates) == 1 and mutations and gates[0] < min(mutations))
    if not prune_helper_uses_owner or not prune_precedes_mutation:
        violations.append(
            f"{prune_command_path}: prune must preflight survivors through the native "
            "deployment boundary before mutation"
        )

    hook_reconciles = _named_functions(hook_integrator_tree, "reconcile_after_removal")
    hook_reconcile_is_guarded = False
    if len(hook_reconciles) == 1:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(hook_reconciles[0])
            if isinstance(node, ast.Call)
        ]
        gates = [line for name, line in calls if name == "preflight_reintegration_survivors"]
        mutations = [
            line
            for name, line in calls
            if name in {"self.sync_integration", "self.integrate_hooks_for_target"}
        ]
        hook_reconcile_is_guarded = bool(
            len(gates) == 1 and mutations and gates[0] < min(mutations)
        )
    if not hook_reconcile_is_guarded:
        violations.append(
            f"{hook_integrator_path}: direct hook survivor reconciliation must preflight "
            "before mutation"
        )

    uninstall_defs = _named_functions(uninstall_cli_tree, "uninstall")
    if len(uninstall_defs) != 1:
        violations.append(f"{uninstall_cli_path}: uninstall command must have one owner")
    else:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(uninstall_defs[0])
            if isinstance(node, ast.Call)
        ]
        gates = [line for name, line in calls if name == "_preflight_uninstall_survivors"]
        side_effects = [
            line
            for name, line in calls
            if name
            in {
                "_fire_uninstall_scripts",
                "_stage_shared_local_survivors",
                "dump_yaml_roundtrip",
                "_remove_packages_from_disk",
                "_sync_integrations_after_uninstall",
            }
        ]
        if not gates or not side_effects or min(gates) >= min(side_effects):
            violations.append(
                f"{uninstall_cli_path}: uninstall survivor preflight must run before "
                "scripts, staging, or destructive reconciliation"
            )

    sync_defs = _named_functions(uninstall_engine_tree, "_sync_integrations_after_uninstall")
    if len(sync_defs) != 1:
        violations.append(
            f"{uninstall_engine_path}: uninstall integration sync must have one owner"
        )
    else:
        calls = [
            (_call_name(node), node.lineno)
            for node in ast.walk(sync_defs[0])
            if isinstance(node, ast.Call)
        ]
        gates = [line for name, line in calls if name == "_preflight_uninstall_survivors"]
        mutations = [
            line
            for name, line in calls
            if name
            in {
                "clear_discovery_cache",
                "sync_for_target",
                "sync_integration",
                "integrate_package_skill",
            }
        ]
        if not gates or not mutations or min(gates) >= min(mutations):
            violations.append(
                f"{uninstall_engine_path}: direct uninstall sync must preflight "
                "survivors before integration mutation"
            )

    native_skill_references = [
        node
        for tree in (skill_integrator_tree, skill_routing_tree)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "AGENT_PLUGIN"
    ]
    if native_skill_references:
        violations.append(
            f"{skill_integrator_path}: SkillIntegrator must not route AGENT_PLUGIN content"
        )
    for function_name in ("available_skill_names", "integrate_package_skill"):
        definitions = _named_functions(skill_integrator_tree, function_name)
        if len(definitions) != 1 or not _is_call_statement(
            _first_executable_statement(definitions[0]) if definitions else None,
            "enforce_agent_plugin_deployment_boundary",
        ):
            violations.append(
                f"{skill_integrator_path}: {function_name} must reject native packages "
                "through the deployment boundary owner"
            )

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
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "native_detection"
        ]
        native_branch_preserves_package = (
            len(native_branches) == 1
            and bool(native_branches[0].body)
            and isinstance(native_branches[0].body[-1], ast.Return)
            and _is_validation_package(native_branches[0].body[-1].value)
        )
        if (
            "route_agent_plugin_package" not in resolver_calls
            or not native_branch_preserves_package
        ):
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
            legacy_normalization_owner = (
                relative == "src/apm_cli/models/validation.py"
                and function.name == "_validate_marketplace_plugin"
            ) or (
                relative == "src/apm_cli/install/drift.py"
                and function.name == "_normalize_legacy_local_plugin_for_replay"
                and "detect_agent_plugin" in calls
            )
            if "normalize_plugin_directory" in calls and not legacy_normalization_owner:
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
