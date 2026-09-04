"""Thin rule catalog for install/deployment architecture checks.

Owns no check-function bodies of its own: every ``check=`` reference below
resolves to a cohesive check-family module (request/source, frozen/audit,
base-integrator/contraction, uninstall/resolution, package-target-
authorization) or to :mod:`scripts.architecture_linter.checks.install_policy_intent`'s
``EXTRA_RULES`` splice (AC3 policy authorities, AC4 declared intent, AC7
mutation locking). Kept as its own module -- rather than folded into any one
check-family file -- purely so this rule catalog's own historical, non-
physical ordering (a rule's registration position here does not follow its
check function's definition order) stays visible and auditable in one place.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_base_integrator_and_contraction import (
    _GUARD_BASE_INTEGRATOR,
    _GUARD_PROVENANCE,
    _GUARD_TARGET_CONTRACTION,
    check_base_integrator,
    check_provenance_state,
    check_target_file_contraction,
)
from scripts.architecture_linter.checks.install_deployment_shared import _rule
from scripts.architecture_linter.checks.install_dry_run_plan import (
    _GUARD_DRY_RUN_PLAN,
    check_prospective_dry_run_plan,
)
from scripts.architecture_linter.checks.install_frozen_and_audit import (
    _GUARD_AUDIT_REPLAY,
    _GUARD_FROZEN,
    _GUARD_LIFECYCLE_SERIALIZATION,
    _GUARD_MCP_OWNERSHIP,
    _GUARD_UNINSTALL_REACHABILITY,
    check_audit_replay,
    check_frozen,
    check_lifecycle_serialization,
    check_mcp_ownership_migration,
    check_uninstall_reachability,
)
from scripts.architecture_linter.checks.install_lsp_plugin import (
    GUARD_EXECUTABLE_TRUST,
    GUARD_LSP_LIFECYCLE,
    GUARD_LSP_TARGET_CONTRACT,
    check_executable_trust_context,
    check_lsp_lifecycle,
    check_lsp_target_contract,
)
from scripts.architecture_linter.checks.install_package_target_authorization import (
    _GUARD_PACKAGE_TARGET,
    check_package_target_authorization,
)
from scripts.architecture_linter.checks.install_policy_intent import EXTRA_RULES
from scripts.architecture_linter.checks.install_request_and_source import (
    _GUARD_INSTALL_SCOPE,
    _GUARD_OUTCOME,
    _GUARD_PRIMITIVE_CLASSIFICATION,
    _GUARD_REQUEST_DEFAULTS,
    _GUARD_SOURCE_PLAN,
    check_install_scope_selection,
    check_outcome,
    check_primitive_classification,
    check_request_defaults,
    check_source_plan,
)
from scripts.architecture_linter.checks.install_uninstall_and_resolution import (
    _GUARD_RESOLUTION_REPLACEMENT,
    _GUARD_UNINSTALL_SELECTION,
    check_resolution_replacement,
    check_uninstall_selection,
)
from scripts.architecture_linter.models import Rule

RULES: tuple[Rule, ...] = (
    _rule(
        _GUARD_PACKAGE_TARGET,
        "Restriction-only package target authorization has one owner (install/target_filter.py).",
        check_package_target_authorization,
    ),
    _rule(
        _GUARD_MCP_OWNERSHIP,
        "Legacy MCP runtime ownership-key migration stays owned by install/mcp/ownership.py.",
        check_mcp_ownership_migration,
    ),
    _rule(
        _GUARD_PROVENANCE,
        "Deployment provenance/state mutates only through DeploymentLedgerCodec owners.",
        check_provenance_state,
    ),
    _rule(
        _GUARD_TARGET_CONTRACTION,
        "Target-scoped deployed-file contraction stays owned by install/manifest_reconcile.py.",
        check_target_file_contraction,
    ),
    _rule(
        _GUARD_OUTCOME,
        "Install success/failure outcome routes through install/outcome.py, not adapters.",
        check_outcome,
    ),
    _rule(
        _GUARD_RESOLUTION_REPLACEMENT,
        "Resolution replacement activation stays owned by install/resolution_staging.py.",
        check_resolution_replacement,
    ),
    _rule(
        _GUARD_FROZEN,
        "Frozen install mutation eligibility routes through InstallService before mutation.",
        check_frozen,
    ),
    _rule(
        _GUARD_DRY_RUN_PLAN,
        "Prospective dry-run state and selection stay owned by ProspectiveInstallPlan.",
        check_prospective_dry_run_plan,
    ),
    _rule(
        _GUARD_SOURCE_PLAN,
        "Authorized deployable source paths come from install/deployable_source_plan.py.",
        check_source_plan,
    ),
    _rule(
        _GUARD_PRIMITIVE_CLASSIFICATION,
        "Primitive kind classification is declaration-first and has one owner.",
        check_primitive_classification,
    ),
    _rule(
        _GUARD_REQUEST_DEFAULTS,
        "Install invocation option defaults stay owned by install/request.py.",
        check_request_defaults,
    ),
    _rule(
        _GUARD_INSTALL_SCOPE,
        "Direct MCP installs consume the install command's single scope decision.",
        check_install_scope_selection,
    ),
    _rule(
        _GUARD_BASE_INTEGRATOR,
        "File-level deploy/sync/cleanup stays owned by BaseIntegrator.",
        check_base_integrator,
    ),
    _rule(
        GUARD_EXECUTABLE_TRUST,
        "Install and update consume one effective executable-trust owner.",
        check_executable_trust_context,
    ),
    _rule(
        GUARD_LSP_TARGET_CONTRACT,
        "LSP target shape and deployment paths route through LSPIntegrator.",
        check_lsp_target_contract,
    ),
    _rule(
        GUARD_LSP_LIFECYCLE,
        "LSP collection and reconciliation route through install/lsp/integration.py.",
        check_lsp_lifecycle,
    ),
    _rule(
        _GUARD_UNINSTALL_REACHABILITY,
        "Post-uninstall dependency reachability routes through deps/reachability.py.",
        check_uninstall_reachability,
    ),
    _rule(
        _GUARD_AUDIT_REPLAY,
        "CI audit scratch materialization routes through install/audit_replay.py.",
        check_audit_replay,
    ),
    _rule(
        _GUARD_LIFECYCLE_SERIALIZATION,
        "Lifecycle mutators route through install/locking.py.",
        check_lifecycle_serialization,
    ),
    _rule(
        _GUARD_UNINSTALL_SELECTION,
        "Dependency CLI parsing + uninstall selection route through dependency/selection.py.",
        check_uninstall_selection,
    ),
    *EXTRA_RULES,
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
