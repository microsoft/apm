"""Plugin-bin, approval-routing, audit-policy, and manifest-inheritance
install policy analyzers.

Ports five guard-less semantic rules (AC3 policy authorities / AC4 declared
intent). RULES assembly lives in the thin catalog module
:mod:`install_policy_intent`, which imports each check function below.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _POLICY_DISCOVERY,
    _SKILL_INTEGRATOR,
    _after_context,
    _banned,
    _configured,
    _count_re,
    _first_line,
    _has_re,
    _has_text,
    _matches,
    _report,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_PLUGIN_BIN = "install-deployment-plugin-bin-eligibility"


RULE_APPROVAL_OUTCOME = "install-deployment-approval-outcome-routing"


RULE_AUDIT_POLICY_DISCOVERY = "install-deployment-audit-policy-discovery"


RULE_MANIFEST_INHERITANCE = "install-deployment-manifest-inheritance-includes"


RULE_INCOMPLETE_CHAIN = "install-deployment-incomplete-chain-routing"


_EXEC_GATE_OWNER = "src/apm_cli/install/exec_gate.py"


_INSTALL_SERVICES = "src/apm_cli/install/services.py"


_INSTALL_TREE = "src/apm_cli/install/"


_BIN_DEPLOY_OWNER_DEF = re.compile(r"^def plugin_bin_deployable\(")


_BIN_DEPLOY_ANY_DEF = re.compile(r"^def _?plugin_bin_deployable\(")


_BIN_DEPLOY_SERVICES_IMPORT = "plugin_bin_deployable as _plugin_bin_deployable"


_BIN_DEPLOY_SKILL_IMPORT = "from apm_cli.install.exec_gate import plugin_bin_deployable"


def check_plugin_bin_eligibility(provider: FactsProvider) -> tuple[Violation, ...]:
    """Plugin bin deployment eligibility must route through install/exec_gate.py."""
    rule_id = RULE_PLUGIN_BIN
    owner, owner_fail = _configured(provider, _EXEC_GATE_OWNER, rule_id)
    services, services_fail = _configured(provider, _INSTALL_SERVICES, rule_id)
    integrator, integrator_fail = _configured(provider, _SKILL_INTEGRATOR, rule_id)
    failures = [*owner_fail, *services_fail, *integrator_fail]
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    owner_defs = _count_re(owner, _BIN_DEPLOY_OWNER_DEF)
    if owner_defs != 1:
        findings.append(
            _report(
                rule_id,
                _EXEC_GATE_OWNER,
                "Plugin bin deployment eligibility must define exactly one "
                f"plugin_bin_deployable owner (found {owner_defs})",
            )
        )
    if not _has_text(services, _BIN_DEPLOY_SERVICES_IMPORT):
        findings.append(
            _report(
                rule_id,
                _INSTALL_SERVICES,
                "Install services must import the exec_gate plugin_bin_deployable owner; "
                f"missing: {_BIN_DEPLOY_SERVICES_IMPORT}",
            )
        )
    if not _has_text(integrator, _BIN_DEPLOY_SKILL_IMPORT):
        findings.append(
            _report(
                rule_id,
                _SKILL_INTEGRATOR,
                "Skill integrator must import the exec_gate plugin_bin_deployable owner; "
                f"missing: {_BIN_DEPLOY_SKILL_IMPORT}",
            )
        )
    # The legacy duplicate scan deliberately offered no exemption escape
    # hatch: a second eligibility definition is never a documented exception.
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _INSTALL_TREE, excluded=(_EXEC_GATE_OWNER,)),
            pattern=_BIN_DEPLOY_ANY_DEF,
            message=(
                "Duplicate plugin bin deployment eligibility definition; "
                "route through install/exec_gate.py"
            ),
            configured=False,
            respect_exempt=False,
        )
    )
    return tuple(findings)


_POLICY_OUTCOME_OWNER = "src/apm_cli/policy/outcome_routing.py"


_APPROVAL_ADAPTER = "src/apm_cli/commands/approve.py"


_POLICY_OUTCOME_DEF = re.compile(r"^POLICY_RESOLUTION_FAILURE_OUTCOMES = frozenset\(")


_POLICY_OUTCOME_IMPORT = "from ..policy.outcome_routing import POLICY_RESOLUTION_FAILURE_OUTCOMES"


_POLICY_OUTCOME_LITERALS = re.compile(
    r'"(cache_miss_fetch_fail|garbage_response|hash_mismatch|incomplete_chain|malformed)"'
)


def check_approval_outcome_routing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Approval fallback outcomes must use policy/outcome_routing.py."""
    rule_id = RULE_APPROVAL_OUTCOME
    owner, owner_fail = _configured(provider, _POLICY_OUTCOME_OWNER, rule_id)
    adapter, adapter_fail = _configured(provider, _APPROVAL_ADAPTER, rule_id)
    failures = [*owner_fail, *adapter_fail]
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    if not _has_re(owner, _POLICY_OUTCOME_DEF):
        findings.append(
            _report(
                rule_id,
                _POLICY_OUTCOME_OWNER,
                "Policy outcome routing must own POLICY_RESOLUTION_FAILURE_OUTCOMES "
                "as a module-level frozenset",
            )
        )
    if not _has_text(adapter, _POLICY_OUTCOME_IMPORT):
        findings.append(
            _report(
                rule_id,
                _APPROVAL_ADAPTER,
                "Approval fallback outcomes must import POLICY_RESOLUTION_FAILURE_OUTCOMES; "
                f"missing: {_POLICY_OUTCOME_IMPORT}",
            )
        )
    # The legacy literal ban ran without an exemption filter.
    for line, column in _matches(adapter, _POLICY_OUTCOME_LITERALS, respect_exempt=False):
        findings.append(
            _report(
                rule_id,
                _APPROVAL_ADAPTER,
                "Approval fallback outcomes must not restate policy outcome string "
                "literals; use POLICY_RESOLUTION_FAILURE_OUTCOMES",
                line,
                column,
            )
        )
    return tuple(findings)


_AUDIT_ADAPTER = "src/apm_cli/commands/audit.py"


_DISCOVER_POLICY_CALL = re.compile(r"discover_policy\(")


def check_audit_policy_discovery(provider: FactsProvider) -> tuple[Violation, ...]:
    """Audit policy sources must use chain-aware discovery, not discover_policy()."""
    return tuple(
        _banned(
            provider,
            rule_id=RULE_AUDIT_POLICY_DISCOVERY,
            paths=(_AUDIT_ADAPTER,),
            pattern=_DISCOVER_POLICY_CALL,
            message="Audit policy sources must use chain-aware discovery, not discover_policy(",
            respect_exempt=True,
        )
    )


_POLICY_INHERITANCE = "src/apm_cli/policy/inheritance.py"


_MERGE_MANIFEST_ANCHOR = "def _merge_manifest"


_MERGE_MANIFEST_CONTEXT = 20


_REQUIRE_EXPLICIT_INCLUDES = "require_explicit_includes"


def check_manifest_inheritance(provider: FactsProvider) -> tuple[Violation, ...]:
    """Manifest inheritance must merge require_explicit_includes."""
    rule_id = RULE_MANIFEST_INHERITANCE
    lines, failures = _configured(provider, _POLICY_INHERITANCE, rule_id)
    if failures:
        return failures

    window = _after_context(lines, _MERGE_MANIFEST_ANCHOR, _MERGE_MANIFEST_CONTEXT)
    if _has_text(window, _REQUIRE_EXPLICIT_INCLUDES):
        return ()
    return (
        _report(
            rule_id,
            _POLICY_INHERITANCE,
            "Manifest inheritance must merge require_explicit_includes in _merge_manifest",
            _first_line(lines, _MERGE_MANIFEST_ANCHOR),
        ),
    )


_INCOMPLETE_CHAIN = "incomplete_chain"


def check_incomplete_chain_routing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Incomplete policy chains must route through fail-closed outcome handling."""
    rule_id = RULE_INCOMPLETE_CHAIN
    findings: list[Violation] = []
    for path in (_POLICY_DISCOVERY, _POLICY_OUTCOME_OWNER):
        lines, failures = _configured(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        if not _has_text(lines, _INCOMPLETE_CHAIN):
            findings.append(
                _report(
                    rule_id,
                    path,
                    "Incomplete policy chains must route through fail-closed outcome "
                    f"handling; missing: {_INCOMPLETE_CHAIN}",
                )
            )
    return tuple(findings)
