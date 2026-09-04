"""Request-default, scope-selection, source-plan, and outcome install analyzers.

Ports four owner guards recorded in
``.apm/architecture/owners/install-deployment.json``:
``install-deployment-request-defaults``,
``install-deployment-install-scope-selection``,
``install-deployment-source-plan``, and ``install-deployment-outcome``.
RULES assembly (with the guard-id constants and the ``_rule`` factory) lives in
the thin catalog module :mod:`install_deployment_analyzers`, which imports each
check function below.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _INSTALL_ADAPTER,
    _SRC_PREFIX,
    _count_re,
    _duplicate_definition_lines,
    _facts_for,
    _lines,
    _present,
    _present_re,
    _summary,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import FileFacts, Violation

_GUARD_OUTCOME = "install-deployment-outcome"


_GUARD_SOURCE_PLAN = "install-deployment-source-plan"


_GUARD_PRIMITIVE_CLASSIFICATION = "install-deployment-primitive-classification"


_GUARD_REQUEST_DEFAULTS = "install-deployment-request-defaults"


_GUARD_INSTALL_SCOPE = "install-deployment-install-scope-selection"


_REQUEST_OWNER = "src/apm_cli/install/request.py"
_MCP_COMMAND = "src/apm_cli/install/mcp/command.py"


_ALLOWED_WRAPPER_DEFAULTS = frozenset({"update_refs", "verbose", "only_packages"})


def _wrapper_default_args(index: TreeIndex) -> list[str]:
    """Return trailing defaulted args of top-level ``_install_apm_dependencies``."""
    if index.root is None:
        return []
    wrapper = next(
        (
            node
            for node in index.children(index.root)
            if isinstance(node, FUNCTION_NODES) and node.name == "_install_apm_dependencies"
        ),
        None,
    )
    if wrapper is None:
        return []
    default_count = len(wrapper.args.defaults)
    if default_count == 0:
        return []
    positional = wrapper.args.args[-default_count:]
    return [arg.arg for arg in positional if arg.arg not in _ALLOWED_WRAPPER_DEFAULTS]


def check_request_defaults(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install invocation defaults must stay owned by InstallRequest."""
    rule_id = _GUARD_REQUEST_DEFAULTS
    adapter, adapter_fail = _facts_for(provider, _INSTALL_ADAPTER, rule_id)
    owner, owner_fail = _facts_for(provider, _REQUEST_OWNER, rule_id)
    failures = list(adapter_fail) + list(owner_fail)
    if failures:
        return tuple(failures)

    index = provider.tree_index(_INSTALL_ADAPTER)
    offending = _wrapper_default_args(index) if index is not None else []
    trust_bin_pattern = re.compile(r"^[ \t]*trust_bin: bool \| None = None$")
    message = "Install invocation defaults must remain owned by InstallRequest"
    if (
        offending
        or not _present(adapter, "request = InstallRequest(")
        or not _present_re(owner, trust_bin_pattern)
    ):
        detail = message
        if offending:
            detail = f"{message}; unexpected defaulted wrapper args: {', '.join(offending)}"
        return (_summary(rule_id, _INSTALL_ADAPTER, detail),)
    return ()


_MCP_CONFLICTS = "src/apm_cli/install/mcp/conflicts.py"


def _named_calls(nodes: tuple[ast.AST, ...], name: str) -> list[ast.Call]:
    """Return direct name calls from one precomputed function scope."""
    return [
        node
        for node in nodes
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _attribute_calls(nodes: tuple[ast.AST, ...], name: str) -> list[ast.Call]:
    """Return attribute calls with the requested method name."""
    return [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def _has_name_keyword(call: ast.Call, keyword: str, name: str) -> bool:
    """Return whether a call forwards one keyword from the named local."""
    return any(
        item.arg == keyword and isinstance(item.value, ast.Name) and item.value.id == name
        for item in call.keywords
    )


def _has_scope_projection_keyword(call: ast.Call, keyword: str) -> bool:
    """Return whether a keyword projects user scope through the canonical helper."""
    for item in call.keywords:
        value = item.value
        if (
            item.arg == keyword
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "is_user_scope"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "scope"
        ):
            return True
    return False


def _canonical_scope_assignment(node: ast.Assign) -> bool:
    """Return whether an assignment owns the global-to-install-scope mapping."""
    value = node.value
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "scope"
        and isinstance(value, ast.IfExp)
        and isinstance(value.test, ast.Name)
        and value.test.id == "global_"
        and isinstance(value.body, ast.Attribute)
        and isinstance(value.body.value, ast.Name)
        and value.body.value.id == "InstallScope"
        and value.body.attr == "USER"
        and isinstance(value.orelse, ast.Attribute)
        and isinstance(value.orelse.value, ast.Name)
        and value.orelse.value.id == "InstallScope"
        and value.orelse.attr == "PROJECT"
    )


def _takes_argument(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Return whether a function declares the named positional or keyword argument."""
    arguments = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    return any(argument.arg == name for argument in arguments)


def _takes_scope(call: ast.Call) -> bool:
    """Return whether a scope helper consumes the canonical local value."""
    return (
        len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "scope"
        and not call.keywords
    )


def _takes_deploy_root(call: ast.Call) -> bool:
    """Return whether discovery consumes the canonical deploy-root projection."""
    if len(call.args) != 1:
        return False
    value = call.args[0]
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "get_deploy_root"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "scope"
        and _has_name_keyword(call, "exclude", "exclude")
    )


def check_install_scope_selection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Direct MCP installs must consume the install command's scope decision."""
    rule_id = _GUARD_INSTALL_SCOPE
    _adapter, adapter_fail = _facts_for(provider, _INSTALL_ADAPTER, rule_id)
    _conflicts, conflicts_fail = _facts_for(provider, _MCP_CONFLICTS, rule_id)
    _command, command_fail = _facts_for(provider, _MCP_COMMAND, rule_id)
    failures = list(adapter_fail) + list(conflicts_fail) + list(command_fail)
    if failures:
        return tuple(failures)

    adapter_index = provider.tree_index(_INSTALL_ADAPTER)
    conflicts_index = provider.tree_index(_MCP_CONFLICTS)
    command_index = provider.tree_index(_MCP_COMMAND)
    if adapter_index is None or conflicts_index is None or command_index is None:
        return (
            _summary(
                rule_id,
                _INSTALL_ADAPTER,
                "Direct MCP installs must consume the install command scope",
            ),
        )

    install = adapter_index.function("install")
    handler = adapter_index.function("_handle_mcp_install")
    validator = conflicts_index.function("validate_mcp_conflicts")
    command = command_index.function("run_mcp_install")
    if (
        not isinstance(install, (ast.FunctionDef, ast.AsyncFunctionDef))
        or not isinstance(handler, (ast.FunctionDef, ast.AsyncFunctionDef))
        or not isinstance(validator, (ast.FunctionDef, ast.AsyncFunctionDef))
        or not isinstance(command, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        return (
            _summary(
                rule_id,
                _INSTALL_ADAPTER,
                "Direct MCP installs must consume the install command scope",
            ),
        )

    install_nodes = adapter_index.own_scope(install)
    handler_nodes = adapter_index.own_scope(handler)
    command_nodes = command_index.own_scope(command)
    assignments = [
        node
        for node in install_nodes
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "scope" for target in node.targets)
    ]
    handler_calls = _named_calls(install_nodes, "_handle_mcp_install")
    run_calls = _named_calls(handler_nodes, "_run_mcp_install")
    target_calls = _named_calls(handler_nodes, "resolve_manifest_target_decision")
    scope_partition_calls = _named_calls(handler_nodes, "partition_user_scope_runtimes")
    scope_discovery_calls = _named_calls(handler_nodes, "discover_user_scope_mcp_runtimes")
    exclusion_calls = _named_calls(handler_nodes, "filter_excluded_mcp_runtimes")
    dry_run_validation_calls = _named_calls(handler_nodes, "_validate_mcp_dry_run_entry")
    registry_validation_calls = _attribute_calls(command_nodes, "prevalidate_registry_dependencies")
    bootstrap_calls = _named_calls(command_nodes, "_create_minimal_apm_yml")
    manifest_write_calls = _named_calls(command_nodes, "add_mcp_to_apm_yml")
    manifest_calls = _named_calls(handler_nodes, "get_manifest_path")
    apm_dir_calls = _named_calls(handler_nodes, "get_apm_dir")
    validator_args = (
        *validator.args.posonlyargs,
        *validator.args.args,
        *validator.args.kwonlyargs,
    )

    valid = (
        len(assignments) == 1
        and _canonical_scope_assignment(assignments[0])
        and _takes_argument(handler, "scope")
        and len(handler_calls) == 1
        and _has_name_keyword(handler_calls[0], "scope", "scope")
        and len(run_calls) == 1
        and _has_name_keyword(run_calls[0], "scope", "scope")
        and _has_name_keyword(run_calls[0], "initial_manifest_config", "initial_manifest_config")
        and len(target_calls) == 1
        and _has_scope_projection_keyword(target_calls[0], "user_scope")
        and len(scope_partition_calls) == 1
        and len(scope_discovery_calls) == 1
        and _takes_deploy_root(scope_discovery_calls[0])
        and len(exclusion_calls) == 1
        and len(registry_validation_calls) == 1
        and len(bootstrap_calls) == 1
        and len(manifest_write_calls) == 1
        and len(dry_run_validation_calls) == 1
        and target_calls[0].lineno < run_calls[0].lineno
        and scope_partition_calls[0].lineno < run_calls[0].lineno
        and scope_discovery_calls[0].lineno < run_calls[0].lineno
        and exclusion_calls[0].lineno < run_calls[0].lineno
        and dry_run_validation_calls[0].lineno < run_calls[0].lineno
        and registry_validation_calls[0].lineno < bootstrap_calls[0].lineno
        and bootstrap_calls[0].lineno < manifest_write_calls[0].lineno
        and len(manifest_calls) == 1
        and _takes_scope(manifest_calls[0])
        and len(apm_dir_calls) == 1
        and _takes_scope(apm_dir_calls[0])
        and all(argument.arg != "global_" for argument in validator_args)
    )
    if valid:
        return ()
    return (
        _summary(
            rule_id,
            _INSTALL_ADAPTER,
            "Direct MCP installs must consume the install command scope",
        ),
    )


_SOURCE_PLAN_OWNER = "src/apm_cli/install/deployable_source_plan.py"


_SOURCE_PLAN_CONSUMERS = (
    "src/apm_cli/integration/prompt_integrator.py",
    "src/apm_cli/integration/agent_integrator.py",
    "src/apm_cli/integration/command_integrator.py",
    "src/apm_cli/integration/instruction_integrator.py",
    "src/apm_cli/integration/hook_integrator.py",
    "src/apm_cli/integration/hook_bundle.py",
    "src/apm_cli/integration/kiro_hook_integrator.py",
    "src/apm_cli/integration/canvas_integrator.py",
)


def check_source_plan(provider: FactsProvider) -> tuple[Violation, ...]:
    """Deployable hook paths must route through the shared source selector."""
    rule_id = _GUARD_SOURCE_PLAN
    findings: list[Violation] = []
    owner, owner_fail = _facts_for(provider, _SOURCE_PLAN_OWNER, rule_id)
    services, services_fail = _facts_for(provider, "src/apm_cli/install/services.py", rule_id)
    scan, scan_fail = _facts_for(provider, "src/apm_cli/install/helpers/security_scan.py", rule_id)
    engine, engine_fail = _facts_for(provider, "src/apm_cli/commands/uninstall/engine.py", rule_id)
    skill, skill_fail = _facts_for(provider, "src/apm_cli/integration/skill_integrator.py", rule_id)
    hook, hook_fail = _facts_for(provider, "src/apm_cli/integration/hook_integrator.py", rule_id)
    kiro, kiro_fail = _facts_for(
        provider, "src/apm_cli/integration/kiro_hook_integrator.py", rule_id
    )
    failures = (
        list(owner_fail)
        + list(services_fail)
        + list(scan_fail)
        + list(engine_fail)
        + list(skill_fail)
        + list(hook_fail)
        + list(kiro_fail)
    )
    if failures:
        return tuple(failures)

    class_pattern = re.compile(r"^class DeployableSourcePlan:")
    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=class_pattern,
        owner=_SOURCE_PLAN_OWNER,
        message="Deployable hook paths must route through the shared target-aware source selector",
        respect_exempt=False,
    )
    block_failed = (
        _count_re(owner, class_pattern) != 1
        or not _present(services, "source_plan = DeployableSourcePlan.create(")
        or not _present(scan, "source_plan.scan_security(")
        or not _present(owner, "paths=self.paths")
        or not _present(services, "source_plan=source_plan")
        or not _present(engine, "integrate_package_primitives(")
        or _present(engine, "integrate_package_skill(")
        or not _present(skill, "source_plan = DeployableSourcePlan.create(")
        or not _present(owner, "HookIntegrator.select_deployable_hook_sources")
        or not _present(hook, "selected_bundle_files=hook_sources.bundle_for")
        or not _present(kiro, "selected_bundle_files=selected_bundle_files")
        or not _present(owner, "CanvasIntegrator.find_canvas_bundles")
        or bool(duplicates)
    )
    if block_failed:
        findings.append(
            _summary(
                rule_id,
                _SOURCE_PLAN_OWNER,
                "Deployable hook paths must route through the shared target-aware source selector",
            )
        )
        findings.extend(duplicates)

    for consumer in _SOURCE_PLAN_CONSUMERS:
        facts, fail = _facts_for(provider, consumer, rule_id)
        if fail:
            findings.extend(fail)
            continue
        if not _present(facts, "source_plan"):
            findings.append(
                _summary(
                    rule_id,
                    consumer,
                    "Primitive materializers must consume the canonical deployable source plan",
                )
            )
    return tuple(findings)


_PRIMITIVE_CLASSIFICATION_OWNER = "src/apm_cli/install/primitive_classification.py"
_PRIMITIVE_CLASSIFICATION_CONSUMERS = (
    "src/apm_cli/agent_plugins/loader.py",
    "src/apm_cli/bundle/local_bundle.py",
    "src/apm_cli/deps/plugin_parser.py",
    "src/apm_cli/install/sources.py",
    "src/apm_cli/integration/agent_integrator.py",
)
_SCHEMA_REJECTION_STRINGS = (
    "Unsupported schema-bearing plugin manifest",
    "Unsupported Agent Plugins manifest schema",
)


def check_primitive_classification(provider: FactsProvider) -> tuple[Violation, ...]:
    """Artifact kind decisions must route through primitive_classification.py."""
    rule_id = _GUARD_PRIMITIVE_CLASSIFICATION
    findings: list[Violation] = []
    owner, owner_fail = _facts_for(provider, _PRIMITIVE_CLASSIFICATION_OWNER, rule_id)
    if owner_fail:
        return tuple(owner_fail)

    required_owner_fragments = (
        "def classify_plugin_manifest(",
        "def classify_plugin_manifest_schema(",
        "def plugin_manifest_schema_warning(",
        "def classify_agent_source_file(",
        "Unknown ``$schema`` values are identification misses",
    )
    missing = [fragment for fragment in required_owner_fragments if not _present(owner, fragment)]
    if missing:
        findings.append(
            _summary(
                rule_id,
                _PRIMITIVE_CLASSIFICATION_OWNER,
                "Primitive classification owner is missing required declaration-first APIs",
            )
        )

    for consumer in _PRIMITIVE_CLASSIFICATION_CONSUMERS:
        facts, fail = _facts_for(provider, consumer, rule_id)
        if fail:
            findings.extend(fail)
            continue
        text = "\n".join(_lines(facts))
        for rejection in _SCHEMA_REJECTION_STRINGS:
            if rejection in text:
                findings.append(
                    _summary(
                        rule_id,
                        consumer,
                        "Plugin schema classification misses must not be local fatal rejections",
                    )
                )
        if consumer == "src/apm_cli/integration/agent_integrator.py" and not _present(
            facts,
            "classify_agent_source_file(",
        ):
            findings.append(
                _summary(
                    rule_id,
                    consumer,
                    "AgentIntegrator must route agent source classification through the owner",
                )
            )
        if consumer != _PRIMITIVE_CLASSIFICATION_OWNER and "plugin_manifest" in text:
            if (
                consumer
                in {
                    "src/apm_cli/agent_plugins/loader.py",
                    "src/apm_cli/bundle/local_bundle.py",
                    "src/apm_cli/deps/plugin_parser.py",
                    "src/apm_cli/install/sources.py",
                }
                and "primitive_classification" not in text
            ):
                findings.append(
                    _summary(
                        rule_id,
                        consumer,
                        "Plugin manifest route consumers must import primitive_classification",
                    )
                )
    return tuple(findings)


_OUTCOME_OWNER = "src/apm_cli/install/outcome.py"
_OUTCOME_ADAPTERS = (
    "src/apm_cli/commands/install.py",
    "src/apm_cli/commands/update.py",
    "src/apm_cli/commands/lock.py",
)


def check_outcome(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install adapters must route post-install outcome through install/outcome.py."""
    rule_id = _GUARD_OUTCOME
    findings: list[Violation] = []
    owner, owner_fail = _facts_for(provider, _OUTCOME_OWNER, rule_id)
    adapter_facts: dict[str, FileFacts] = {}
    adapter_failures: list[Violation] = []
    for adapter_path in _OUTCOME_ADAPTERS:
        adapter, adapter_fail = _facts_for(provider, adapter_path, rule_id)
        adapter_facts[adapter_path] = adapter
        adapter_failures.extend(adapter_fail)
    if adapter_failures or owner_fail:
        return tuple(adapter_failures + list(owner_fail))

    install_adapter = adapter_facts[_INSTALL_ADAPTER]
    for number, line in enumerate(_lines(install_adapter), start=1):
        if EXEMPT_MARKER in line:
            continue
        column = line.find("classify_post_install_result")
        if column >= 0:
            findings.append(
                violation(
                    rule_id,
                    _INSTALL_ADAPTER,
                    "Install adapters must not classify diagnostics; route through install/outcome.py",
                    line=number,
                    column=column + 1,
                )
            )
    for adapter_path, adapter in adapter_facts.items():
        if not (
            _present(adapter, "apply_install_command_outcome(")
            or _present(adapter, "render_install_result_failure_summary(")
            or _present(adapter, "exit_unless_install_result_allows_success(")
        ):
            findings.append(
                _summary(
                    rule_id,
                    adapter_path,
                    "Install command adapters must route exit and success-summary gating through install/outcome.py",
                )
            )
    if not (
        _present(owner, "def result_from_install_context(")
        and _present(owner, "def finalize_install_result(")
        and _present(owner, "def apply_install_command_outcome(")
    ):
        findings.append(
            _summary(
                rule_id,
                _OUTCOME_OWNER,
                "Install outcome owner must define result_from_install_context, finalize_install_result, and apply_install_command_outcome",
            )
        )
    return tuple(findings)
