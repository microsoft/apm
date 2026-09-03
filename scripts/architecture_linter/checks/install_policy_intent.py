"""Thin rule catalog for install policy-authority/declared-intent checks.

Owns no check-function bodies of its own: every ``check=`` reference below
resolves to a cohesive check-family module (plugin/approval, gitlab/bundle,
deployment/ref, skill/lock). Imported by
:mod:`scripts.architecture_linter.checks.install_deployment_analyzers`, which
appends :data:`EXTRA_RULES` to its ``RULES`` catalog; kept as its own module
purely to keep that catalog well under the module size budget.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_policy_deployment_and_ref import (
    RULE_DEPLOYMENT_FRAME,
    RULE_LOCAL_ANCHOR,
    RULE_MCP_REGISTRY_RESOLUTION,
    RULE_QUEUE_DEDUP,
    RULE_REF_RECHECK,
    RULE_REGISTRY_INTENT,
    check_deployment_frame_projection,
    check_local_identity_anchor,
    check_mcp_registry_resolution,
    check_ref_recheck_ownership,
    check_registry_dependency_intent,
    check_resolver_queue_dedup,
)
from scripts.architecture_linter.checks.install_policy_gitlab_and_bundle import (
    RULE_GITLAB_ADAPTER,
    RULE_GITLAB_FACADE,
    RULE_LOCAL_BUNDLE_PREFLIGHT,
    RULE_REQUIRE_HASHES,
    RULE_WINNER_SELECTION,
    check_dependency_winner_selection,
    check_gitlab_facade_orchestration,
    check_gitlab_policy_adapter,
    check_local_bundle_preflight,
    check_require_hashes_enforcement,
)
from scripts.architecture_linter.checks.install_policy_plugin_and_approval import (
    RULE_APPROVAL_OUTCOME,
    RULE_AUDIT_POLICY_DISCOVERY,
    RULE_INCOMPLETE_CHAIN,
    RULE_MANIFEST_INHERITANCE,
    RULE_PLUGIN_BIN,
    check_approval_outcome_routing,
    check_audit_policy_discovery,
    check_incomplete_chain_routing,
    check_manifest_inheritance,
    check_plugin_bin_eligibility,
)
from scripts.architecture_linter.checks.install_policy_shared import GROUP, _semantic_rule
from scripts.architecture_linter.checks.install_policy_skill_and_lock import (
    RULE_CLAUDE_SKILL,
    RULE_GIT_OBJECT_FIELDS,
    RULE_LOCKED_SUBSET,
    RULE_MARKETPLACE_LOCK,
    RULE_SKILL_SUBSET,
    RULE_UPDATE_PLAN_REFS,
    check_cached_claude_skill_metadata,
    check_git_object_field_authority,
    check_locked_skill_subset,
    check_marketplace_mutation_lock,
    check_skill_subset_tokens,
    check_update_plan_ref_annotation,
)
from scripts.architecture_linter.models import Rule

EXTRA_RULES: tuple[Rule, ...] = (
    _semantic_rule(
        RULE_PLUGIN_BIN,
        "Plugin bin deployment eligibility routes through install/exec_gate.py.",
        check_plugin_bin_eligibility,
    ),
    _semantic_rule(
        RULE_APPROVAL_OUTCOME,
        "Approval fallback outcomes use policy/outcome_routing.py, not literals.",
        check_approval_outcome_routing,
    ),
    _semantic_rule(
        RULE_AUDIT_POLICY_DISCOVERY,
        "Audit policy sources use chain-aware discovery, not discover_policy().",
        check_audit_policy_discovery,
    ),
    _semantic_rule(
        RULE_MANIFEST_INHERITANCE,
        "Manifest inheritance merges require_explicit_includes.",
        check_manifest_inheritance,
    ),
    _semantic_rule(
        RULE_INCOMPLETE_CHAIN,
        "Incomplete policy chains route through fail-closed outcome handling.",
        check_incomplete_chain_routing,
    ),
    _semantic_rule(
        RULE_GITLAB_ADAPTER,
        "GitLab policy discovery routes through policy/_gitlab.py.",
        check_gitlab_policy_adapter,
    ),
    _semantic_rule(
        RULE_GITLAB_FACADE,
        "GitLab policy cache and transport remain in policy/_gitlab.py.",
        check_gitlab_facade_orchestration,
    ),
    _semantic_rule(
        RULE_LOCAL_BUNDLE_PREFLIGHT,
        "Local bundle installs route policy through install_preflight.py.",
        check_local_bundle_preflight,
    ),
    _semantic_rule(
        RULE_REQUIRE_HASHES,
        "require_hashes enforcement routes through install/integrity.py.",
        check_require_hashes_enforcement,
    ),
    _semantic_rule(
        RULE_WINNER_SELECTION,
        "Dependency ref winner selection shares _select_dependency_winners.",
        check_dependency_winner_selection,
    ),
    _semantic_rule(
        RULE_SKILL_SUBSET,
        "Skill subset filter tokens come from models/dependency/subsets.py.",
        check_skill_subset_tokens,
    ),
    _semantic_rule(
        RULE_DEPLOYMENT_FRAME,
        "Dependency deployment-frame mapping belongs to UnifiedLinkResolver.",
        check_deployment_frame_projection,
    ),
    _semantic_rule(
        RULE_REF_RECHECK,
        "Existing-path ref rechecks use drift.py::should_force_ref_recheck.",
        check_ref_recheck_ownership,
    ),
    _semantic_rule(
        RULE_QUEUE_DEDUP,
        "Resolver queue dedup preserves ref constraints.",
        check_resolver_queue_dedup,
    ),
    _semantic_rule(
        RULE_LOCAL_ANCHOR,
        "Local identity uses its anchor and persists declaring-parent provenance.",
        check_local_identity_anchor,
    ),
    Rule(
        id=RULE_MCP_REGISTRY_RESOLUTION,
        group=GROUP,
        guard_ids=(RULE_MCP_REGISTRY_RESOLUTION,),
        description="MCP registry URL precedence routes through registry/client.py.",
        check=check_mcp_registry_resolution,
    ),
    _semantic_rule(
        RULE_REGISTRY_INTENT,
        "Resolved registry URLs and registry-sourced dependency intent survive.",
        check_registry_dependency_intent,
    ),
    _semantic_rule(
        RULE_CLAUDE_SKILL,
        "Cached/frozen Claude Skill lock metadata routes through validation.py.",
        check_cached_claude_skill_metadata,
    ),
    _semantic_rule(
        RULE_LOCKED_SUBSET,
        "LockedDependency.to_dependency_ref reconstructs locked skill_subset.",
        check_locked_skill_subset,
    ),
    _semantic_rule(
        RULE_UPDATE_PLAN_REFS,
        "Cached update planning resolves refs through the downloader owner.",
        check_update_plan_ref_annotation,
    ),
    _semantic_rule(
        RULE_GIT_OBJECT_FIELDS,
        "Object-form Git dependency fields come from the product parser.",
        check_git_object_field_authority,
    ),
    _semantic_rule(
        RULE_MARKETPLACE_LOCK,
        "Marketplace mutations lock the full load-modify-save transaction.",
        check_marketplace_mutation_lock,
    ),
)


__all__ = ["EXTRA_RULES"]
