"""Skill-integration, projection-owner, package, and resolver
projection-boundary checks (final third).

Part of the facts-only port of
``scripts/check_agent_plugin_projection_boundary.py`` (legacy bundle-format
subcheck **B20**); see :mod:`agent_plugin_projection` for the entry point
that composes every ``_check_*(ctx)`` boundary check here.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.agent_plugin_boundary_checks_a import (
    _check_first_action_gate,
)
from scripts.architecture_linter.checks.agent_plugin_scan_primitives import (
    _Boundary,
    _calls_public_configuration_thaw,
    _function_calls,
    _is_named_assignment,
    _is_validation_package,
    _module_functions,
    _named_functions,
    _stored_name_count,
)
from scripts.architecture_linter.checks.agent_plugin_shared import (
    PACKAGE,
    PROJECTION,
    RESOLVER,
    SKILL_INTEGRATOR,
    SKILL_ROUTING,
    VALIDATION,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.models import Violation

_ALLOWED_PROJECTION_CALLS = frozenset(
    {
        "APMPackage.from_mapping",
        "AgentPluginManifestAuthorityError",
        "_project_apm_configuration",
        "data.update",
        "dict",
        "get",
        "isinstance",
        "thaw_frozen_json",
    }
)


def _check_skill_integration(ctx: _Boundary) -> tuple[Violation, ...]:
    """SkillIntegrator never routes native content and always defers to the boundary."""
    findings: list[Violation] = []
    native_skill_references = [
        node
        for path in (SKILL_INTEGRATOR, SKILL_ROUTING)
        for node in ctx.index(path).nodes
        if isinstance(node, ast.Attribute) and node.attr == "AGENT_PLUGIN"
    ]
    if native_skill_references:
        findings.append(
            ctx.report(SKILL_INTEGRATOR, "SkillIntegrator must not route AGENT_PLUGIN content")
        )
    for function_name in ("available_skill_names", "integrate_package_skill"):
        findings.extend(
            _check_first_action_gate(
                ctx,
                SKILL_INTEGRATOR,
                function_name,
                f"{function_name} must reject native packages through the deployment "
                "boundary owner",
            )
        )
    return tuple(findings)


def _check_projection_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """agent_plugins/projection.py: one typed, pure, IR-thawing projection owner."""
    index = ctx.index(PROJECTION)
    findings: list[Violation] = []

    projection_defs = _module_functions(index, "project_agent_plugin_package")
    if len(projection_defs) != 1:
        findings.append(
            ctx.report(PROJECTION, "project_agent_plugin_package must have exactly one definition")
        )
    elif "APMPackage.from_mapping" not in _function_calls(index, projection_defs[0]):
        findings.append(
            ctx.report(
                PROJECTION,
                "projection must call APMPackage.from_mapping",
                line=projection_defs[0].lineno,
            )
        )
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
            findings.append(
                ctx.report(
                    PROJECTION,
                    "projection must retain its typed AgentPlugin-to-APMPackage public contract",
                    line=projection_defs[0].lineno,
                )
            )

    root = index.root
    projection_calls = _function_calls(index, root) if root is not None else set()
    unexpected = sorted(projection_calls - _ALLOWED_PROJECTION_CALLS)
    if unexpected:
        findings.append(
            ctx.report(
                PROJECTION,
                "projection call surface must remain pure; unexpected calls: "
                + ", ".join(unexpected),
            )
        )
    findings.extend(_check_projection_thaw(ctx, index))
    if "APMPackage" in projection_calls:
        findings.append(ctx.report(PROJECTION, "projection must not call APMPackage directly"))
    return tuple(findings)


def _check_projection_thaw(ctx: _Boundary, index: TreeIndex) -> tuple[Violation, ...]:
    """The projection thaws the canonical FrozenJson through one unrebound import."""
    thaw_bindings: list[tuple[ast.AST, ast.alias]] = []
    for node in index.nodes:
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
        for node in index.nodes
    )
    configuration_defs = _module_functions(index, "_project_apm_configuration")
    thaw_assignment_is_preserved = False
    if len(configuration_defs) == 1:
        configuration_def = configuration_defs[0]
        thaw_assignments = [
            node
            for node in index.walk(configuration_def)
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
            and _stored_name_count(index, configuration_def, "projected") == 1
            and bool(configuration_def.body)
            and isinstance(configuration_def.body[-1], ast.Return)
            and isinstance(configuration_def.body[-1].value, ast.Name)
            and configuration_def.body[-1].value.id == "projected"
        )
    if (
        not thaw_imported
        or thaw_rebound
        or len(configuration_defs) != 1
        or not _calls_public_configuration_thaw(index, configuration_defs[0])
        or not thaw_assignment_is_preserved
    ):
        return (ctx.report(PROJECTION, "projection must thaw canonical FrozenJson"),)
    return ()


def _check_package_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """models/apm_package.py: file loading routes through the from_mapping owner."""
    index = ctx.index(PACKAGE)
    findings: list[Violation] = []

    mapping_defs = _named_functions(index, "from_mapping")
    if len(mapping_defs) != 1:
        findings.append(ctx.report(PACKAGE, "APMPackage.from_mapping must have one definition"))

    file_loader_defs = _named_functions(index, "from_apm_yml")
    file_loader_preserves_owner = False
    if len(file_loader_defs) == 1:
        file_loader = file_loader_defs[0]
        owner_assignments = [
            node
            for node in index.walk(file_loader)
            if _is_named_assignment(node, "result", "cls.from_mapping")
        ]
        file_loader_preserves_owner = (
            len(owner_assignments) == 1
            and _stored_name_count(index, file_loader, "result") == 1
            and any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Name)
                and node.value.id == "result"
                for node in file_loader.body
            )
        )
    if not file_loader_preserves_owner:
        findings.append(
            ctx.report(PACKAGE, "APMPackage file loading must route through from_mapping owner")
        )
    return tuple(findings)


def _check_validation_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """models/validation.py: native validation projects, never normalizes."""
    index = ctx.index(VALIDATION)
    validation_defs = _module_functions(index, "_validate_agent_plugin")
    if len(validation_defs) != 1:
        return (ctx.report(VALIDATION, "_validate_agent_plugin must have one definition"),)

    findings: list[Violation] = []
    validation_def = validation_defs[0]
    calls = _function_calls(index, validation_def)
    if "project_agent_plugin_package" not in calls:
        findings.append(
            ctx.report(
                VALIDATION,
                "native validation must call project_agent_plugin_package",
                line=validation_def.lineno,
            )
        )
    package_assignments = [
        node
        for node in index.walk(validation_def)
        if _is_named_assignment(node, "package", "project_agent_plugin_package")
    ]
    result_package_assignments = [
        node
        for node in index.walk(validation_def)
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
        or _stored_name_count(index, validation_def, "package") != 1
        or len(result_package_assignments) != 1
    ):
        findings.append(
            ctx.report(
                VALIDATION,
                "native validation bypasses projection or enters normalization",
                line=validation_def.lineno,
            )
        )
    return tuple(findings)


def _check_resolver_owner(ctx: _Boundary) -> tuple[Violation, ...]:
    """deps/apm_resolver.py: dependency loading preserves the projected package."""
    index = ctx.index(RESOLVER)
    root = index.root
    resolver_defs = [
        item
        for node in (index.children(root) if root is not None else [])
        if isinstance(node, ast.ClassDef) and node.name == "APMDependencyResolver"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "_try_load_dependency_package"
    ]
    if len(resolver_defs) != 1:
        return (
            ctx.report(RESOLVER, "_try_load_dependency_package must have exactly one definition"),
        )

    findings: list[Violation] = []
    message = "Agent Plugin dependency loading must preserve the projected package"
    resolver_calls = _function_calls(index, resolver_defs[0])
    if "validate_apm_package" not in resolver_calls:
        findings.append(ctx.report(RESOLVER, message, line=resolver_defs[0].lineno))
    native_branches = [
        node
        for node in index.walk(resolver_defs[0])
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
    if "route_agent_plugin_package" not in resolver_calls or not native_branch_preserves_package:
        findings.append(ctx.report(RESOLVER, message, line=resolver_defs[0].lineno))
    return tuple(findings)
