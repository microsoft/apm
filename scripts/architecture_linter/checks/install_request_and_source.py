"""Request-default, source-plan, and outcome-routing install analyzers.

Ports three owner guards recorded in
``.apm/architecture/owners/install-deployment.json``:
``install-deployment-request-defaults``, ``install-deployment-source-plan``,
and ``install-deployment-outcome``. RULES assembly (with the guard-id
constants and the ``_rule`` factory) lives in the thin catalog module
:mod:`install_deployment_analyzers`, which imports each check function below.
"""

from __future__ import annotations

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
from scripts.architecture_linter.models import Violation

_GUARD_OUTCOME = "install-deployment-outcome"


_GUARD_SOURCE_PLAN = "install-deployment-source-plan"


_GUARD_REQUEST_DEFAULTS = "install-deployment-request-defaults"


_REQUEST_OWNER = "src/apm_cli/install/request.py"


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


_OUTCOME_OWNER = "src/apm_cli/install/outcome.py"


def check_outcome(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install adapters must route post-install outcome through install/outcome.py."""
    rule_id = _GUARD_OUTCOME
    findings: list[Violation] = []
    adapter, adapter_fail = _facts_for(provider, _INSTALL_ADAPTER, rule_id)
    owner, owner_fail = _facts_for(provider, _OUTCOME_OWNER, rule_id)
    if adapter_fail or owner_fail:
        return tuple(list(adapter_fail) + list(owner_fail))

    for number, line in enumerate(_lines(adapter), start=1):
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
    if not (
        _present(owner, "def result_from_install_context(")
        and _present(owner, "def finalize_install_result(")
    ):
        findings.append(
            _summary(
                rule_id,
                _OUTCOME_OWNER,
                "Install outcome owner must define result_from_install_context and finalize_install_result",
            )
        )
    return tuple(findings)
