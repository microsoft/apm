#!/usr/bin/env bash
# Static architecture anti-regression guard.
#
# Legitimate exceptions must carry:
#   # architecture-authority-exempt: <owner and reason>

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

violations=0

check_pattern() {
    local label="$1"
    local pattern="$2"
    shift 2
    local hits
    hits=$(grep -En "$pattern" "$@" 2>/dev/null \
        | grep -v 'architecture-authority-exempt:' || true)
    if [ -n "$hits" ]; then
        echo "[x] $label"
        echo "$hits"
        violations=$((violations + 1))
    fi
}

echo "[*] AC1: canonical capability authorities"
check_pattern \
    "Runtime names must come from runtime/registry.py" \
    'click\.Choice\(\[.*(copilot|codex|gemini|llm)|runtime_commands = \[|return \["copilot", "codex"' \
    src/apm_cli/commands/runtime.py \
    src/apm_cli/core/script_runner.py \
    src/apm_cli/runtime/manager.py \
    src/apm_cli/workflow/runner.py
check_pattern \
    "Host backend dispatch must come from core/host_providers.py" \
    '_BACKEND_BY_KIND|only supports .gitlab.|Supported values: gitlab' \
    src/apm_cli/core/auth.py \
    src/apm_cli/deps/host_backends.py \
    src/apm_cli/models/dependency/reference.py
check_pattern \
    "Manifest target consumers must use canonical_targets" \
    '(package|apm_package)\.(target|targets)\b' \
    src/apm_cli/bundle/packer.py \
    src/apm_cli/install/mcp/integration.py \
    src/apm_cli/commands/uninstall/engine.py
check_pattern \
    "Install orchestration must not branch on native locator target names" \
    'name == "copilot-(app|cowork)"|name in \{.*copilot-(app|cowork)' \
    src/apm_cli/install/deployed_paths.py \
    src/apm_cli/install/manifest_reconcile.py

echo "[*] AC2: validate-before-mutate boundaries"
compiled_write_hits=$(
    grep -rEn \
        'write_text_lf|atomic_write_text|\.write_text\(|open\([^)]*["'\'']w' \
        src/apm_cli/compilation/ --include='*.py' \
        | grep -v 'src/apm_cli/compilation/output_writer.py' \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if [ -n "$compiled_write_hits" ]; then
    echo "[x] Compiled output writes must use CompiledOutputWriter"
    echo "$compiled_write_hits"
    violations=$((violations + 1))
fi
hook_file="src/apm_cli/integration/hook_integrator.py"
validation_line=$(grep -n 'if not validation\.valid:' "$hook_file" | tail -1 | cut -d: -f1)
continue_line=$(awk -v start="$validation_line" 'NR > start && /continue/ {print NR; exit}' "$hook_file")
write_line=$(grep -n 'with open(target_path, "w"' "$hook_file" | tail -1 | cut -d: -f1)
if [ -z "$validation_line" ] || [ -z "$continue_line" ] || [ -z "$write_line" ] \
    || [ "$continue_line" -gt "$write_line" ]; then
    echo "[x] Hook payload validation must continue before the native payload write"
    violations=$((violations + 1))
fi
hook_scope_owner_count=$(grep -Ec \
    '^    def _deploy_root_for_hook_rewrite\(' "$hook_file" || true)
hook_scope_duplicate_hits=$(
    grep -REn --include='*hook_integrator.py' \
        'deploy_root_for_rewrite[[:space:]]*=.*user_scope' \
        src/apm_cli/integration \
        | grep -v "^${hook_file}:" \
        | grep -v 'integrator\._deploy_root_for_hook_rewrite' \
        || true
)
if [ "$hook_scope_owner_count" -ne 1 ] \
    || ! grep -q \
        'deploy_root_for_rewrite = integrator\._deploy_root_for_hook_rewrite' \
        src/apm_cli/integration/kiro_hook_integrator.py \
    || [ -n "$hook_scope_duplicate_hits" ]; then
    echo "[x] Hook rewrite scope must route through HookIntegrator"
    [ -n "$hook_scope_duplicate_hits" ] && echo "$hook_scope_duplicate_hits"
    violations=$((violations + 1))
fi
check_pattern \
    "Lockfile supported-version authority belongs in deps/lockfile.py" \
    'SUPPORTED_LOCKFILE_VERSIONS|lockfile_version[[:space:]]+(==|!=|in)' \
    $(find src/apm_cli -name '*.py' ! -path 'src/apm_cli/deps/lockfile.py')

echo "[*] AC3: outcome and policy enforcement authorities"
check_pattern \
    "Install adapters must not classify diagnostics" \
    'classify_post_install_result' \
    src/apm_cli/commands/install.py
approval_file="src/apm_cli/commands/approve.py"
policy_outcome_owner="src/apm_cli/policy/outcome_routing.py"
if ! grep -q '^POLICY_RESOLUTION_FAILURE_OUTCOMES = frozenset(' \
    "$policy_outcome_owner" \
    || ! grep -q \
        'from ..policy.outcome_routing import POLICY_RESOLUTION_FAILURE_OUTCOMES' \
        "$approval_file" \
    || grep -Eq \
        '"(cache_miss_fetch_fail|garbage_response|hash_mismatch|incomplete_chain|malformed)"' \
        "$approval_file"; then
    echo "[x] Approval fallback outcomes must use policy/outcome_routing.py"
    violations=$((violations + 1))
fi
check_pattern \
    "Audit policy sources must use chain-aware discovery" \
    'discover_policy\(' \
    src/apm_cli/commands/audit.py
if ! grep -A20 'def _merge_manifest' src/apm_cli/policy/inheritance.py \
    | grep -q 'require_explicit_includes'; then
    echo "[x] Manifest inheritance must merge require_explicit_includes"
    violations=$((violations + 1))
fi
if ! grep -q 'incomplete_chain' src/apm_cli/policy/discovery.py \
    || ! grep -q 'incomplete_chain' src/apm_cli/policy/outcome_routing.py; then
    echo "[x] Incomplete policy chains must route through fail-closed outcome handling"
    violations=$((violations + 1))
fi
policy_file="src/apm_cli/policy/discovery.py"
policy_named_defs=$(grep -Ec \
    '^[[:space:]]*def [[:alnum:]_]*(policy_to_dict|serialize_policy)[[:alnum:]_]*\(' \
    "$policy_file" || true)
policy_serializer_body=$(awk '
    /^def _serialize_policy\(/ {flag=1}
    flag && /^def / && !/^def _serialize_policy\(/ {exit}
    flag {print}
' "$policy_file")
policy_cache_write_body=$(awk '
    /^def _write_cache\(/ {flag=1}
    flag && /^def / && !/^def _write_cache\(/ {exit}
    flag {print}
' "$policy_file")
policy_duplicate_hits=$(
    grep -rEn --include='*.py' \
        '^[[:space:]]*def [[:alnum:]_]*(policy_to_dict|serialize_policy)[[:alnum:]_]*\(' \
        src/apm_cli/policy \
        | grep -v "^${policy_file}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if [ "$policy_named_defs" -ne 2 ] \
    || ! printf '%s\n' "$policy_serializer_body" \
        | grep -Eq '^[[:space:]]*[^#]*_policy_to_dict\(policy\)' \
    || ! printf '%s\n' "$policy_cache_write_body" \
        | grep -Eq '^[[:space:]]*serialized[[:space:]]*=[[:space:]]*_serialize_policy\(policy\)' \
    || [ -n "$policy_duplicate_hits" ]; then
    echo "[x] Cached policy shape must route through policy/discovery.py::_policy_to_dict"
    [ -n "$policy_duplicate_hits" ] && echo "$policy_duplicate_hits"
    violations=$((violations + 1))
fi
local_bundle_handler="src/apm_cli/install/local_bundle_handler.py"
if ! grep -q \
    'from ..policy.install_preflight import run_policy_preflight' \
    "$local_bundle_handler" \
    || ! grep -q 'policy_fetch, _enforcement_active = run_policy_preflight(' \
        "$local_bundle_handler" \
    || ! grep -q 'cache_only=True' "$local_bundle_handler" \
    || ! grep -q 'mcp_deps=bundle_mcp_deps' "$local_bundle_handler"; then
    echo "[x] Local bundle installs must route policy through install_preflight.py"
    violations=$((violations + 1))
fi
check_pattern \
    "require_hashes enforcement must route through install/integrity.py" \
    'policy(\.security\.integrity)?\.require_hashes' \
    src/apm_cli/install/pipeline.py \
    src/apm_cli/install/local_bundle_handler.py \
    src/apm_cli/policy/policy_checks.py

echo "[*] AC4: declared-intent preservation"
check_pattern \
    "Deployment claim handoff belongs to DeploymentReconciler" \
    'def reconcile_cross_package_deployed_files|all_current_deployed|other_current' \
    src/apm_cli/install/phases/lockfile.py
if ! grep -q 'DeploymentReconciler.reconcile_package_claims' \
    src/apm_cli/install/phases/lockfile.py; then
    echo "[x] LockfileBuilder must consume DeploymentReconciler package claims"
    violations=$((violations + 1))
fi
check_pattern \
    "Dependency ref winner selection must use one helper" \
    'download_winners|level_winners|seen_keys|nodes_at_depth\.sort' \
    src/apm_cli/deps/apm_resolver.py
winner_selector_calls=$(grep -c '_select_dependency_winners(' src/apm_cli/deps/apm_resolver.py)
if [ "$winner_selector_calls" -ne 3 ]; then
    echo "[x] Dependency dispatch and flattening must share _select_dependency_winners"
    violations=$((violations + 1))
fi
# Skill subset filter tokens: two layers of defense. The cheap lexical grep
# catches the exact retired shape (literal helper name / pattern); it is kept
# as defense in depth even though it is not sufficient on its own -- a
# renamed helper reimplementing the same normalization algorithm evades a
# grep by construction. The AST checker (scripts/check_skill_subset_owner.py)
# is the semantic detector: it flags ANY local function, in these same two
# files, that combines slash normalization + path-leaf extraction +
# token-set collection, regardless of naming. Both feed one label and
# increment violations at most once.
skill_subset_files=(
    src/apm_cli/integration/skill_integrator.py
    src/apm_cli/bundle/plugin_exporter.py
)
skill_subset_lexical_hits=$(grep -En \
    'def _skill_subset_name_filter|set\(dep\.skill_subset\)|Path\(normalized_path\)\.name' \
    "${skill_subset_files[@]}" 2>/dev/null \
    | grep -v 'architecture-authority-exempt:' || true)
skill_subset_ast_hits=$(python3 scripts/check_skill_subset_owner.py "${skill_subset_files[@]}" 2>&1)
skill_subset_ast_status=$?
if [ -n "$skill_subset_lexical_hits" ] || [ "$skill_subset_ast_status" -ne 0 ]; then
    echo "[x] Skill subset filter tokens must come from models/dependency/subsets.py"
    [ -n "$skill_subset_lexical_hits" ] && echo "$skill_subset_lexical_hits"
    [ "$skill_subset_ast_status" -ne 0 ] && echo "$skill_subset_ast_hits"
    violations=$((violations + 1))
fi
check_pattern \
    "Dependency deployment-frame mapping belongs to UnifiedLinkResolver" \
    'deployment_package_root' \
    $(find src/apm_cli -name '*.py' \
        ! -path 'src/apm_cli/models/apm_package.py' \
        ! -path 'src/apm_cli/integration/base_integrator.py' \
        ! -path 'src/apm_cli/compilation/link_resolver.py' \
        ! -path 'src/apm_cli/install/drift.py')
if ! grep -q \
    'candidate_in_deployment = ctx.deployment_package_root / package_relative' \
    src/apm_cli/compilation/link_resolver.py; then
    echo "[x] UnifiedLinkResolver must project source assets into the deployment frame"
    violations=$((violations + 1))
fi
ref_recheck_owner="src/apm_cli/drift.py"
ref_recheck_consumers=(
    src/apm_cli/deps/apm_resolver.py
    src/apm_cli/install/phases/resolve.py
)
if ! grep -q '^def should_force_ref_recheck(' "$ref_recheck_owner" \
    || ! grep -q 'should_force_ref_recheck(' "${ref_recheck_consumers[0]}" \
    || ! grep -q 'should_force_ref_recheck(' "${ref_recheck_consumers[1]}" \
    || grep -Eq '_force_semver_resolve|def should_force_ref_recheck' \
        "${ref_recheck_consumers[@]}" \
    || grep -rEq --include='*.py' --exclude='test_architecture_authorities.py' \
        'def _force_semver_resolve|def should_force_ref_recheck' tests; then
    echo "[x] Existing-path ref rechecks must use drift.py::should_force_ref_recheck"
    violations=$((violations + 1))
fi
ref_freshness_owner="src/apm_cli/deps/tiered_ref_resolver.py"
ref_freshness_consumers=(
    src/apm_cli/install/phases/resolve.py
    src/apm_cli/install/helpers/ref_seed.py
    src/apm_cli/commands/outdated.py
)
ref_freshness_duplicate_hits=$(
    grep -rEn --include='*.py' \
        'ctx\.update_refs[[:space:]]+or[[:space:]]+ctx\.refresh|def [[:alnum:]_]*ref_freshness|class [[:alnum:]_]*RefFreshness' \
        src/apm_cli \
        | grep -v "^${ref_freshness_owner}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if ! grep -q '^class RefFreshnessPolicy(Enum):' "$ref_freshness_owner" \
    || ! grep -q '^def ref_freshness_policy_for_install(' "$ref_freshness_owner" \
    || ! grep -q '^    if freshness_policy\.allows_bare_cache:' \
        "$ref_freshness_owner" \
    || ! grep -q 'ref_freshness_policy_for_install(ctx)' "${ref_freshness_consumers[0]}" \
    || ! grep -q 'ref_freshness_policy_for_install(ctx)' "${ref_freshness_consumers[1]}" \
    || ! grep -q 'freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE' \
        "${ref_freshness_consumers[2]}" \
    || grep -rEq --include='*.py' --exclude='tiered_ref_resolver.py' \
        'L2BareRevParse' src/apm_cli \
    || [ -n "$ref_freshness_duplicate_hits" ]; then
    echo "[x] Git ref freshness must route through RefFreshnessPolicy"
    [ -n "$ref_freshness_duplicate_hits" ] && echo "$ref_freshness_duplicate_hits"
    violations=$((violations + 1))
fi
cleanup_claim_owner="src/apm_cli/install/phases/cleanup.py"
cleanup_claim_output=$(python3 scripts/check_cleanup_claim_owner.py "$cleanup_claim_owner" 2>&1)
cleanup_claim_status=$?
if [ "$cleanup_claim_status" -ne 0 ]; then
    echo "[x] Cleanup current-claim protection must use DeploymentReconciler"
    echo "$cleanup_claim_output"
    violations=$((violations + 1))
fi
shared_target_contraction="src/apm_cli/install/manifest_reconcile.py"
shared_target_output=$(python3 scripts/check_shared_target_contraction_owner.py \
    "$shared_target_contraction" 2>&1)
shared_target_status=$?
if [ "$shared_target_status" -ne 0 ]; then
    echo "[x] Shared target contraction must use DeploymentReconciler"
    echo "$shared_target_output"
    violations=$((violations + 1))
fi
merge_hook_membership_body=$(awk '
    /^def merge_hook_config_paths\(/ {flag=1}
    flag && /^def / && !/^def merge_hook_config_paths\(/ {exit}
    flag {print}
' src/apm_cli/install/manifest_reconcile.py)
if ! printf '%s\n' "$merge_hook_membership_body" | grep -q '_MERGE_HOOK_TARGETS' \
    || ! printf '%s\n' "$merge_hook_membership_body" | grep -q '_APM_HOOKS_SIDECAR' \
    || printf '%s\n' "$merge_hook_membership_body" \
        | grep -Eq 'settings\.json|hooks\.json|apm-hooks\.json'; then
    echo "[x] Drift hook membership exemptions must derive from HookIntegrator registries"
    violations=$((violations + 1))
fi
check_pattern \
    "Resolver queue dedup must preserve ref constraints" \
    'queued_keys.*get_unique_key|get_unique_key.*queued_keys' \
    src/apm_cli/deps/apm_resolver.py
if ! grep -A12 'if source == "local"' src/apm_cli/models/dependency/identity.py \
    | grep -q 'anchored_local_path' \
    || ! grep -q 'declaring_parent' src/apm_cli/deps/lockfile.py; then
    echo "[x] Local identity must use its anchor and persist declaring-parent provenance"
    violations=$((violations + 1))
fi
check_pattern \
    "MCP commands must pass the resolved URL into RegistryIntegration" \
    'RegistryIntegration\(\)' \
    src/apm_cli/commands/mcp.py
if ! grep -A25 'if plugin.registry:' src/apm_cli/marketplace/resolver.py \
    | grep -q 'source="registry"'; then
    echo "[x] Marketplace registry intent must create a registry dependency"
    violations=$((violations + 1))
fi
claude_skill_metadata_owner="src/apm_cli/models/validation.py"
claude_skill_metadata_consumer="src/apm_cli/install/sources.py"
claude_skill_owner_body=$(awk '
    /^def _validate_claude_skill\(/ {flag=1}
    flag && /^def / && !/^def _validate_claude_skill\(/ {exit}
    flag {print}
' "$claude_skill_metadata_owner")
claude_skill_cached_body=$(awk '
    /^class CachedDependencySource\(/ {flag=1}
    /^class FreshDependencySource\(/ {flag=0}
    flag {print}
' "$claude_skill_metadata_consumer")
claude_skill_cached_branch=$(printf '%s\n' "$claude_skill_cached_body" | awk '
    /elif pkg_type == PackageType.CLAUDE_SKILL:/ {flag=1}
    flag && /^        else:/ {exit}
    flag {print}
')
if ! printf '%s\n' "$claude_skill_owner_body" | grep -q 'load_frontmatter' \
    || ! printf '%s\n' "$claude_skill_owner_body" | grep -q 'version="unknown"' \
    || ! printf '%s\n' "$claude_skill_cached_body" \
        | grep -q 'pkg_type == PackageType.CLAUDE_SKILL' \
    || ! printf '%s\n' "$claude_skill_cached_branch" \
        | grep -q 'validate_apm_package(install_path)' \
    || ! printf '%s\n' "$claude_skill_cached_branch" \
        | grep -q 'not validation_result.is_valid or validation_result.package is None' \
    || ! printf '%s\n' "$claude_skill_cached_branch" \
        | grep -q 'Cached Claude Skill is invalid' \
    || printf '%s\n' "$claude_skill_cached_branch" \
        | grep -Eq 'APMPackage\(|repo_url\.split'; then
    echo "[x] Cached/frozen Claude Skill lock metadata must route through validation.py"
    violations=$((violations + 1))
fi
lockfile_to_ref_body=$(awk '
    /^    def to_dependency_ref\(/ {flag=1}
    flag && /^    def / && !/to_dependency_ref/ {exit}
    flag && /^class / {exit}
    flag {print}
' src/apm_cli/deps/lockfile.py)
# Checked as two separate function-scoped greps (rather than requiring both
# the keyword and the owner attribute on one physical line) so that ruff/
# manual formatting wrapping the ``skill_subset=`` expression across lines
# does not produce a false positive.
if ! echo "$lockfile_to_ref_body" | grep -q 'DependencyReference(' \
    || ! echo "$lockfile_to_ref_body" | grep -q 'skill_subset=' \
    || ! echo "$lockfile_to_ref_body" | grep -q 'self\.skill_subset'; then
    echo "[x] LockedDependency.to_dependency_ref must reconstruct skill_subset from self.skill_subset"
    violations=$((violations + 1))
fi
run_replay_body=$(awk '
    /^def run_replay\(/ {flag=1}
    flag && /^def / && !/run_replay/ {exit}
    flag {print}
' src/apm_cli/install/drift.py)
# Same rationale as the lockfile guard above: keyword and owner attribute
# are checked as independent function-scoped greps so multiline formatting
# of the ``skill_subset=`` expression is still accepted.
if ! echo "$run_replay_body" | grep -q 'integrate_package_primitives(' \
    || ! echo "$run_replay_body" | grep -q 'skill_subset=' \
    || ! echo "$run_replay_body" | grep -q 'package_info\.dependency_ref\.skill_subset'; then
    echo "[x] Audit replay must preserve locked skill subset intent"
    violations=$((violations + 1))
fi
audit_ci_gate_body=$(awk '
    /^def _audit_ci_gate\(/ {flag=1}
    flag && /^def / && !/^def _audit_ci_gate\(/ {exit}
    flag {print}
' src/apm_cli/commands/audit.py)
config_consistency_body=$(awk '
    /^def _check_config_consistency\(/ {flag=1}
    flag && /^def / && !/^def _check_config_consistency\(/ {exit}
    flag {print}
' src/apm_cli/policy/ci_checks.py)
if ! grep -q '^def prepare_ci_audit_replay(' src/apm_cli/install/audit_replay.py \
    || ! printf '%s\n' "$audit_ci_gate_body" | grep -q 'prepare_ci_audit_replay' \
    || printf '%s\n' "$audit_ci_gate_body" | grep -q 'run_replay(' \
    || ! printf '%s\n' "$config_consistency_body" | grep -q 'prepared_replay\.modules_root'; then
    echo "[x] CI audit scratch materialization must route through install/audit_replay.py"
    violations=$((violations + 1))
fi
local_bundle_marker_hits=$(
    grep -rEn --include='*.py' \
        "_LOCAL_BUNDLE_OWNER|active_owner.*[\"']local-bundle[\"']|[\"']local-bundle[\"'].*active_owner|owners.*[\"']local-bundle[\"']" \
        src/apm_cli \
        | grep -v '^src/apm_cli/core/deployment_ledger.py:' \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if ! grep -q 'DeploymentLedgerCodec.record_local_bundle_files' \
    src/apm_cli/install/local_bundle_handler.py \
    || ! grep -q 'DeploymentLedgerCodec.local_bundle_paths' \
    src/apm_cli/install/drift.py \
    || [ -n "$local_bundle_marker_hits" ]; then
    echo "[x] Local-bundle replay provenance must route through DeploymentLedgerCodec"
    [ -n "$local_bundle_marker_hits" ] && echo "$local_bundle_marker_hits"
    violations=$((violations + 1))
fi
drift_membership_body=$(awk '
    /^def _collect_tracked_files\(/ {flag=1}
    flag && /^def / && !/^def _collect_tracked_files\(/ {exit}
    flag {print}
' src/apm_cli/install/drift.py)
drift_hash_shape_body=$(awk '
    /^def _collect_hashed_files\(/ {flag=1}
    flag && /^def / && !/^def _collect_hashed_files\(/ {exit}
    flag {print}
' src/apm_cli/install/drift.py)
if ! printf '%s\n' "$drift_membership_body" \
        | grep -q 'DeploymentLedgerCodec.legacy_deployed_file_claims' \
    || ! printf '%s\n' "$drift_hash_shape_body" \
        | grep -q 'DeploymentLedgerCodec.legacy_deployed_file_hash_paths' \
    || printf '%s\n%s\n' "$drift_membership_body" "$drift_hash_shape_body" \
        | grep -Eq 'lockfile\.dependencies|local_deployed_files|deployed_file_hashes'; then
    echo "[x] Drift deployment membership must route through DeploymentLedgerCodec"
    violations=$((violations + 1))
fi
scanner_membership_body=$(awk '
    /^def scan_lockfile_packages\(/ {flag=1}
    flag && /^def / && !/^def scan_lockfile_packages\(/ {exit}
    flag {print}
' src/apm_cli/security/file_scanner.py)
if ! printf '%s\n' "$scanner_membership_body" \
        | grep -q 'DeploymentLedgerCodec.legacy_deployed_file_claims' \
    || printf '%s\n' "$scanner_membership_body" \
        | grep -Eq 'lock\.dependencies|dep\.deployed_files'; then
    echo "[x] Hidden-Unicode membership must route through DeploymentLedgerCodec"
    violations=$((violations + 1))
fi
membership_owner_body=$(awk '
    /^    def legacy_deployed_file_claims\(/ {flag=1}
    flag && /^    def / && !/legacy_deployed_file_claims/ {exit}
    flag {print}
' src/apm_cli/core/deployment_ledger.py)
if ! printf '%s\n' "$membership_owner_body" | grep -q 'dependency\.deployed_files' \
    || ! printf '%s\n' "$membership_owner_body" | grep -q 'lockfile\.local_deployed_files' \
    || printf '%s\n' "$membership_owner_body" | grep -q 'from_lockfile'; then
    echo "[x] Legacy deployed-file membership projection belongs to DeploymentLedgerCodec"
    violations=$((violations + 1))
fi
update_plan_ref_body=$(awk '
    /^def annotate_update_plan_refs\(/ {flag=1}
    flag && /^def / && !/annotate_update_plan_refs/ {exit}
    flag {print}
' src/apm_cli/install/helpers/ref_reuse.py)
if ! echo "$update_plan_ref_body" | grep -q 'downloader\.resolve_git_reference(dep_ref)' \
    || ! echo "$update_plan_ref_body" | grep -q 'dep_ref\.resolved_reference = resolved'; then
    echo "[x] Cached update planning must resolve refs through the downloader owner"
    violations=$((violations + 1))
fi
dependency_field_owner="src/apm_cli/models/dependency/object_fields.py"
dependency_parser="src/apm_cli/models/dependency/reference.py"
dependency_field_duplicate_hits=$(
    grep -rEn --include='*.py' \
        'def reject_unknown_git_fields|_(REMOTE|PARENT)_GIT_DEPENDENCY_FIELDS' \
        src tests \
        | grep -v "^${dependency_field_owner}:" \
        | grep -v '^tests/integration/test_architecture_authorities.py:' \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
fixture_dependency_field_hits=$(
    grep -En \
        'reject_unknown_fields|_(REMOTE|PARENT)?_?GIT_DEPENDENCY_FIELDS' \
        tests/utils/local_package.py \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if ! grep -q 'reject_unknown_git_fields(entry, parent=True)' "$dependency_parser" \
    || ! grep -q 'reject_unknown_git_fields(entry, parent=False)' "$dependency_parser" \
    || [ -n "$dependency_field_duplicate_hits" ] \
    || [ -n "$fixture_dependency_field_hits" ]; then
    echo "[x] Object-form Git dependency fields must come from the product parser"
    [ -n "$dependency_field_duplicate_hits" ] && echo "$dependency_field_duplicate_hits"
    [ -n "$fixture_dependency_field_hits" ] && echo "$fixture_dependency_field_hits"
    violations=$((violations + 1))
fi

echo "[*] AC5: process-wide I/O boundaries"
check_pattern \
    "Machine-output routing belongs at the root CLI" \
    'set_console_stderr' \
    $(find src/apm_cli/commands -name '*.py')
check_pattern \
    "Secret redaction must attach to handlers, not package loggers" \
    'apm_logger\.addFilter|logging\.getLogger\("apm_cli"\)\.addFilter' \
    src/apm_cli/cli.py
if ! grep -q 'detect_output_mode' src/apm_cli/cli.py \
    || ! grep -q 'handler.addFilter' src/apm_cli/cli.py; then
    echo "[x] Root CLI must establish machine mode and handler-level redaction"
    violations=$((violations + 1))
fi
if ! grep -q '_clear_git_auth_env(env)' src/apm_cli/core/auth.py; then
    echo "[x] AuthResolver must scrub inherited Git authorization state"
    violations=$((violations + 1))
fi
check_pattern \
    "TLS trust injection belongs to canonical owners" \
    'truststore\.inject_into_ssl\(' \
    $(find src/apm_cli -name '*.py' \
        ! -path 'src/apm_cli/core/tls_trust.py' \
        ! -path 'src/apm_cli/core/_child_tls/_apm_tls_bootstrap.py')

echo "[*] AC6: neutral IR and schema contracts"
check_pattern \
    "Neutral hook IR must not contain native harness vocabulary" \
    'copilot|gemini|antigravity|timeoutSec|powershell|_apm_source|["'\'']hooks["'\'']' \
    src/apm_cli/integration/hook_ir.py
hook_routing_gate_hits=$(python3 scripts/check_hook_file_routing_owner.py 2>&1)
hook_routing_gate_status=$?
if [ "$hook_routing_gate_status" -ne 0 ]; then
    echo "[x] Per-file hook routing must not be gated by dep_targets_active"
    echo "$hook_routing_gate_hits"
    violations=$((violations + 1))
fi
check_pattern \
    "Manifest schema negotiation belongs in manifest_contract.py" \
    'get\\(["'\'']\\$schema["'\'']\\)' \
    $(find src/apm_cli -name '*.py' ! -path 'src/apm_cli/models/manifest_contract.py')
if ! grep -q 'does not run aggregate' docs/src/content/docs/concepts/lifecycle.md; then
    echo "[x] Lifecycle docs must keep aggregate compilation explicit"
    violations=$((violations + 1))
fi

echo "[*] AC7: concurrency and deadline safety"
check_pattern \
    "Runtime adapters must reuse the deadline-aware base streamer" \
    'subprocess\.Popen' \
    $(find src/apm_cli/runtime -name '*_runtime.py')
if ! grep -q 'time.monotonic' src/apm_cli/runtime/base.py \
    || ! grep -q '_terminate_and_reap' src/apm_cli/runtime/base.py; then
    echo "[x] Runtime streaming must enforce and reap on a wall-clock deadline"
    violations=$((violations + 1))
fi
if ! grep -A8 'def add_marketplace' src/apm_cli/marketplace/registry.py \
    | grep -q '_marketplace_mutation' \
    || ! grep -A12 'def remove_marketplace' src/apm_cli/marketplace/registry.py \
    | grep -q '_marketplace_mutation'; then
    echo "[x] Marketplace mutations must lock the full load-modify-save transaction"
    violations=$((violations + 1))
fi

echo "[*] AC8: Windows installer authorities"
# Owner presence + duplicate-derivation scanning both live in the single
# canonical checker so this guard and the architecture test suite cannot
# drift apart. See scripts/check_windows_stable_path_owner.py.
windows_owner_output=$(python3 scripts/check_windows_stable_path_owner.py --root "$ROOT" 2>&1)
windows_owner_status=$?
if [ "$windows_owner_status" -ne 0 ]; then
    echo "[x] Windows stable executable path belongs to install.ps1"
    echo "$windows_owner_output"
    violations=$((violations + 1))
fi

echo "[*] AC9: executable test contract authorities"
test_contract_output=$(python3 scripts/check_test_contract_authorities.py --root "$ROOT" 2>&1)
test_contract_status=$?
if [ "$test_contract_status" -ne 0 ]; then
    echo "[x] Integration binary selection and rendered CLI parity require canonical owners"
    echo "$test_contract_output"
    violations=$((violations + 1))
fi

echo "[*] AC10: marketplace source parsing authority"
packed_source_body=$(awk '
    /^def _dependency_reference_from_packed_source\(/ {flag=1}
    flag && /^def / && !/^def _dependency_reference_from_packed_source\(/ {exit}
    flag {print}
' src/apm_cli/marketplace/resolver.py)
packed_source_parallel_hits=$(printf '%s\n' "$packed_source_body" \
    | grep -En 'urlparse\(|urllib\.parse|DependencyReference\(' \
    | grep -v 'DependencyReference\.parse_from_dict' \
    | grep -v 'architecture-authority-exempt:' || true)
if ! printf '%s\n' "$packed_source_body" \
        | grep -Fq 'entry: dict[str, object] = {"git": remote.strip()}' \
    || ! printf '%s\n' "$packed_source_body" \
        | grep -Fq 'entry["path"] = path' \
    || ! printf '%s\n' "$packed_source_body" \
        | grep -Fq 'entry["ref"] = declared_ref' \
    || ! printf '%s\n' "$packed_source_body" \
        | grep -Fq 'dependency = DependencyReference.parse_from_dict(entry)' \
    || ! printf '%s\n' "$packed_source_body" \
        | grep -Fq 'if dependency.is_local:' \
    || [ -n "$packed_source_parallel_hits" ]; then
    echo "[x] Packed marketplace sources must use DependencyReference.parse_from_dict"
    [ -n "$packed_source_parallel_hits" ] && echo "$packed_source_parallel_hits"
    violations=$((violations + 1))
fi

echo "[*] AC11: Git repository cache identity authority"
cache_identity_output=$(python3 scripts/check_repository_cache_identity_owner.py \
    --root "$ROOT" 2>&1)
cache_identity_status=$?
if [ "$cache_identity_status" -ne 0 ]; then
    echo "[x] Git repository cache identity must route through canonical owners"
    echo "$cache_identity_output"
    violations=$((violations + 1))
fi
if ! grep -q 'repository = normalize_repo_url(repository_url)' \
    src/apm_cli/deps/shared_clone_cache.py; then
    echo "[x] SharedCloneCache must normalize the complete repository URL"
    violations=$((violations + 1))
fi
if ! grep -q 'repository_url = dep_ref.to_github_url()' \
    src/apm_cli/deps/github_downloader.py; then
    echo "[x] Downloader cache consumers must pass the complete canonical Git URL"
    violations=$((violations + 1))
fi
if ! grep -q 'cache_shard_key(dep_ref.to_github_url())' \
    src/apm_cli/deps/tiered_ref_resolver.py; then
    echo "[x] Tiered ref resolution must reuse the persistent Git cache identity"
    violations=$((violations + 1))
fi
if [ "$(grep -c '_repository_cache_identity(dep_ref)' \
    src/apm_cli/deps/tiered_ref_resolver.py)" -lt 2 ]; then
    echo "[x] Per-run ref resolution must reuse the full repository cache identity"
    violations=$((violations + 1))
fi
if ! grep -q 'return normalize_repo_url(dep_ref.to_github_url())' \
    src/apm_cli/deps/tiered_ref_resolver.py; then
    echo "[x] Per-run ref cache identity must retain host and complete path"
    violations=$((violations + 1))
fi
check_pattern \
    "Repository cache identity must not truncate repository paths" \
    'cache_(host|owner|repo)|_canonical_url[[:space:]]*=[[:space:]]*f?"https://' \
    src/apm_cli/deps/github_downloader.py
check_pattern \
    "Tiered ref resolution must not derive cache shards from repo_url" \
    'cache_shard_key\(dep_ref\.repo_url\)' \
    src/apm_cli/deps
check_pattern \
    "Per-run ref resolution must not key caches by bare repo_url" \
    'cache\.(get|put)\(dep_ref\.repo_url|key[[:space:]]*=[[:space:]]*\(dep_ref\.repo_url' \
    src/apm_cli/deps/tiered_ref_resolver.py
check_pattern \
    "Repository cache keys must stay owned by cache/url_normalize.py" \
    'to_repository_cache_url' \
    src/apm_cli

echo "[*] AC12: diagnostic printable-ASCII authority"
diagnostic_ascii_output=$(python3 scripts/check_diagnostic_ascii_owner.py --root "$ROOT" 2>&1)
diagnostic_ascii_status=$?
if [ "$diagnostic_ascii_status" -ne 0 ]; then
    echo "[x] Agent diagnostic names must use utils/diagnostics.py::printable_ascii_text"
    echo "$diagnostic_ascii_output"
    violations=$((violations + 1))
fi

echo "[*] AC13: Git ref transport selection authority"
semver_transport_router="src/apm_cli/install/helpers/ref_reuse.py"
semver_transport_executor="src/apm_cli/marketplace/ref_resolver.py"
git_ref_transport_consumer="src/apm_cli/deps/git_reference_resolver.py"
if ! grep -q 'transport_plan = transport_selector.select(' "$semver_transport_router" \
    || ! grep -q \
        'transport_scheme = "ssh" if selected_scheme == "ssh" else "https"' \
        "$semver_transport_router" \
    || ! grep -q 'transport_scheme=transport_scheme' "$semver_transport_router" \
    || ! grep -q 'build_ssh_url(' "$semver_transport_executor" \
    || grep -Eq \
        'from .*transport_selection import|TransportSelector\(' \
        "$semver_transport_executor" \
    || ! grep -q \
        'transport_plan = host._transport_selector.select(' \
        "$git_ref_transport_consumer"; then
    echo "[x] Git ref transport must route through TransportSelector into RefResolver"
    violations=$((violations + 1))
fi

echo "[*] AC14: ADO lock-coordinate authority"
if ! grep -q 'with_derived_provider_coordinates' \
    src/apm_cli/deps/lockfile.py \
    || grep -Eq 'ado_(organization|project|repo)' src/apm_cli/deps/lockfile.py \
    || ! grep -q 'DependencyReference.canonical_ado_coordinates' \
        src/apm_cli/marketplace/ref_resolver.py \
    || grep -Eq '(self\.)?repo_url\.split\(' src/apm_cli/deps/lockfile.py \
    || grep -Eq 'owner_repo\.split\(' src/apm_cli/marketplace/ref_resolver.py; then
    echo "[x] ADO coordinates must be derived by DependencyReference, never persisted"
    violations=$((violations + 1))
fi

echo "[*] AC15: hook target-contraction cleanup authority"
check_pattern \
    "Prune/uninstall must stay outside target-contraction hook cleanup (#2250 scope)" \
    'reconcile_dropped_merge_hook_targets\(|reconcile_dropped_targets\(' \
    src/apm_cli/commands/prune.py \
    src/apm_cli/commands/uninstall/*.py
hook_config_write_output=$(python3 scripts/check_hook_config_write_owner.py --root "$ROOT" 2>&1)
hook_config_write_status=$?
if [ "$hook_config_write_status" -ne 0 ]; then
    echo "[x] Merge-hook config/sidecar writes must stay owned by HookIntegrator"
    echo "$hook_config_write_output"
    violations=$((violations + 1))
fi

echo "[*] AC15a: target-specific instruction contraction authority"
target_instruction_contraction_output=$(python3 scripts/check_target_instruction_contraction_owner.py \
    --root "$ROOT" 2>&1)
target_instruction_contraction_status=$?
if [ "$target_instruction_contraction_status" -ne 0 ]; then
    echo "[x] Target-specific instruction contraction must route through manifest_reconcile.py"
    echo "$target_instruction_contraction_output"
    violations=$((violations + 1))
fi

echo "[*] AC15b: effective package target authorization authority"
package_target_output=$(python3 scripts/check_package_target_authority.py --root "$ROOT" 2>&1)
package_target_status=$?
if [ "$package_target_status" -ne 0 ]; then
    echo "[x] Effective package target authorization must route through install/target_filter.py"
    echo "$package_target_output"
    violations=$((violations + 1))
fi

echo "[*] AC15c: merged-hook ownership marker authority"
hook_ownership_owner="src/apm_cli/integration/hook_ownership.py"
hook_ownership_consumer="src/apm_cli/integration/hook_integrator.py"
if ! grep -q '^def dependency_hook_source_marker(' "$hook_ownership_owner" \
    || ! grep -q '^def dependency_hook_sources(' "$hook_ownership_owner" \
    || ! grep -q 'from apm_cli.integration.hook_ownership import (' \
        "$hook_ownership_consumer" \
    || grep -q '^    def _dependency_hook_source' "$hook_ownership_consumer"; then
    echo "[x] Merged-hook ownership markers must route through integration/hook_ownership.py"
    violations=$((violations + 1))
fi

echo "[*] AC16: post-uninstall reachability owner authority"
if ! grep -Eq 'reachability\.compute_forward_reachable_keys|from \.\.\.deps\.reachability import|from apm_cli\.deps\.reachability import' \
    src/apm_cli/commands/uninstall/engine.py; then
    echo "[x] Uninstall engine must call deps/reachability.py's compute_forward_reachable_keys"
    violations=$((violations + 1))
fi
check_pattern \
    "Only deps/reachability.py may walk an installed package's own manifest dependencies" \
    'get_apm_dependencies' \
    $(find src/apm_cli/commands/uninstall -name '*.py')
check_pattern \
    "Uninstall must not re-derive a parallel local-anchor reachability walk" \
    'resolve_local_dep_dir' \
    $(find src/apm_cli/commands/uninstall -name '*.py')

echo "[*] AC17: GitHub API throttle classification authority"
github_throttle_owner="src/apm_cli/deps/github_rate_limit.py"
github_throttle_duplicate_hits=$(
    grep -rEn --include='*.py' \
        'X-RateLimit-Remaining|Retry-After' \
        src/apm_cli \
        | grep -v "^${github_throttle_owner}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if ! grep -q '^def classify_github_throttle(' "$github_throttle_owner" \
    || ! grep -q '^class GitHubThrottleError' "$github_throttle_owner" \
    || [ -n "$github_throttle_duplicate_hits" ]; then
    echo "[x] GitHub throttle signals must be classified only by deps/github_rate_limit.py"
    [ -n "$github_throttle_duplicate_hits" ] && echo "$github_throttle_duplicate_hits"
    violations=$((violations + 1))
fi

echo "[*] AC18: deployment owner and cleanup authority"
deployment_owner_output=$(python3 scripts/check_deployment_owner_boundaries.py \
    src/apm_cli/commands/prune.py \
    src/apm_cli/commands/audit.py \
    src/apm_cli/policy/ci_checks.py 2>&1)
deployment_owner_status=$?
if [ "$deployment_owner_status" -ne 0 ]; then
    echo "[x] Deployment ownership must route through DeploymentLedgerCodec"
    echo "$deployment_owner_output"
    violations=$((violations + 1))
fi
if ! grep -q '^_LEGACY_USER_TARGET_PREFIXES = {' src/apm_cli/core/deployment_ledger.py \
    || ! grep -q '".copilot/": "copilot"' src/apm_cli/core/deployment_ledger.py \
    || ! grep -q '^    def legacy_scope(' src/apm_cli/core/deployment_ledger.py \
    || ! grep -q \
        'scope=DeploymentLedgerCodec.legacy_scope(path)' \
        src/apm_cli/install/manifest_reconcile.py \
    || ! grep -q \
        'if targets is None and user_scope and t.user_root_dir is not None:' \
        src/apm_cli/integration/targets.py; then
    echo "[x] Legacy user deployment scope must route through DeploymentLedgerCodec"
    violations=$((violations + 1))
fi

deployment_state_output=$(python3 scripts/check_deployment_state_mutations.py \
    src/apm_cli 2>&1)
deployment_state_status=$?
if [ "$deployment_state_status" -ne 0 ]; then
    echo "[x] Deployment compatibility state must mutate only through canonical owners"
    echo "$deployment_state_output"
    violations=$((violations + 1))
fi

echo "[*] AC19: git-subprocess auth-header injection authority"
# #2368: build_authorization_header_git_env / build_ado_bearer_git_env return
# an overlay with a hardcoded GIT_CONFIG_COUNT="1". Dict-merging that overlay
# onto an env that already carries indexed GIT_CONFIG_* entries resets the
# count and clobbers index 0, silently dropping inherited git hardening
# (safe.bareRepository, http.sslCAInfo, credential.interactive). The single
# owner for injecting an Authorization header into a git-subprocess env is
# set_authorization_header_git_env / set_ado_bearer_git_env (in-place
# rewrite); dict-merging the build_* overlay onto a populated env is the
# exact defect those setters exist to prevent from recurring.
auth_header_dictmerge_hits=$(
    grep -rEn --include='*.py' \
        '\.update\(\s*build_(authorization_header_git_env|ado_bearer_git_env)\(|\{\*\*[A-Za-z_][A-Za-z0-9_.]*,\s*\*\*build_(authorization_header_git_env|ado_bearer_git_env)\(' \
        src/apm_cli \
        | grep -vE ':[0-9]+:[[:space:]]*#' \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if [ -n "$auth_header_dictmerge_hits" ]; then
    echo "[x] Git-subprocess Authorization-header injection must use set_authorization_header_git_env / set_ado_bearer_git_env (in-place); dict-merging the build_* overlay onto a populated env re-introduces the #2368 clobber bug"
    echo "$auth_header_dictmerge_hits"
    violations=$((violations + 1))
fi

echo "[*] AC20: public github.com anonymous-first auth authority"
public_github_auth_owner="src/apm_cli/core/auth.py"
public_github_auth_duplicate_defs=$(
    grep -rEn --include='*.py' \
        '^[[:space:]]*def uses_public_github_anonymous_first\(' \
        src/apm_cli \
        | grep -v "^${public_github_auth_owner}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
public_github_auth_consumers="
src/apm_cli/deps/clone_engine.py
src/apm_cli/deps/download_strategies.py
src/apm_cli/deps/git_reference_resolver.py
src/apm_cli/deps/github_downloader.py
src/apm_cli/deps/github_downloader_validation.py
"
public_github_auth_missing_consumers=""
for consumer in $public_github_auth_consumers; do
    if ! grep -q 'uses_public_github_anonymous_first(' "$consumer"; then
        public_github_auth_missing_consumers="${public_github_auth_missing_consumers}
${consumer}"
    fi
done
if ! grep -q '^    def uses_public_github_anonymous_first(' "$public_github_auth_owner" \
    || ! grep -q '^    def build_public_github_anonymous_git_env(' "$public_github_auth_owner" \
    || ! grep -q 'lazy_public_github' "$public_github_auth_owner" \
    || [ -n "$public_github_auth_duplicate_defs" ] \
    || [ -n "$public_github_auth_missing_consumers" ]; then
    echo "[x] Public github.com anonymous-first auth ordering must stay owned by AuthResolver"
    [ -n "$public_github_auth_duplicate_defs" ] && echo "$public_github_auth_duplicate_defs"
    [ -n "$public_github_auth_missing_consumers" ] \
        && echo "Missing owner routing:${public_github_auth_missing_consumers}"
    violations=$((violations + 1))
fi

echo "[*] AC21: MCP manifest target precedence authority"
mcp_manifest_adapter=$(
    awk '
        /^def _declared_manifest_target_runtimes\(/ { capture = 1 }
        /^def _resolve_target_runtimes\(/ { capture = 0 }
        capture { print }
    ' src/apm_cli/integration/mcp_integrator_install.py
)
mcp_target_resolver=$(
    awk '
        /^def _resolve_target_runtimes\(/ { capture = 1 }
        /^def _install_self_defined_deps\(/ { capture = 0 }
        capture { print }
    ' src/apm_cli/integration/mcp_integrator_install.py
)
mcp_manifest_selection_line=$(
    grep -n '_declared_manifest_target_runtimes(apm_config)' \
        <<<"$mcp_target_resolver" \
        | head -1 \
        | cut -d: -f1
)
mcp_discovery_line=$(
    grep -n '_discover_installed_runtimes(' \
        <<<"$mcp_target_resolver" \
        | head -1 \
        | cut -d: -f1
)
mcp_integration_validation=$(
    awk '
        /^def run_mcp_integration\(/ { capture = 1 }
        capture { print }
    ' src/apm_cli/install/mcp/integration.py
)
mcp_validation_line=$(
    grep -n 'parse_targets_field(mcp_apm_config)' \
        <<<"$mcp_integration_validation" \
        | head -1 \
        | cut -d: -f1
)
mcp_install_line=$(
    grep -n 'MCPIntegrator.install(' \
        <<<"$mcp_integration_validation" \
        | head -1 \
        | cut -d: -f1
)
mcp_target_projection=$(
    awk '
        /^def canonical_package_target_config\(/ { capture = 1 }
        /^def package_target_selection\(/ { capture = 0 }
        capture { print }
    ' src/apm_cli/models/apm_package.py
)
if ! grep -q 'parse_targets_field(apm_config)' <<<"$mcp_manifest_adapter" \
    || grep -Eq \
        'TARGET_CAPABILITIES|CANONICAL_TARGETS|KNOWN_TARGETS|\[[^]]*(copilot|claude|cursor|codex|gemini|opencode|windsurf|kiro)' \
        <<<"$mcp_manifest_adapter" \
    || [ -z "$mcp_manifest_selection_line" ] \
    || [ -z "$mcp_discovery_line" ] \
    || [ "$mcp_manifest_selection_line" -ge "$mcp_discovery_line" ] \
    || grep -q 'parse_targets_field(' <<<"$mcp_target_resolver" \
    || [ -z "$mcp_validation_line" ] \
    || [ -z "$mcp_install_line" ] \
    || [ "$mcp_validation_line" -ge "$mcp_install_line" ] \
    || ! grep -q 'return {"target": singular, "targets": list(plural)}' \
        <<<"$mcp_target_projection"; then
    echo "[x] MCP target precedence must route through the canonical manifest adapter before discovery"
    violations=$((violations + 1))
fi

echo "[*] AC22: module-level behavioral test taxonomy authority"
taxonomy_plugin="tests/quality/taxonomy_inventory_plugin.py"
taxonomy_contract="tests/quality/test_test_taxonomy.py"
taxonomy_parallel_hits=$(
    grep -En \
        '(^|[^A-Za-z_])(MANIFEST|_manifest_modules|tracked_python_inventory)|behavioral markers outside critical manifest|len\(modules\)[[:space:]]*==' \
        "$taxonomy_contract" \
        || true
)
if ! grep -q 'getattr(module, "pytestmark"' "$taxonomy_plugin" \
    || ! grep -q '"modules": modules' "$taxonomy_plugin" \
    || ! grep -q '"nodes": nodes' "$taxonomy_plugin" \
    || ! grep -q '^def _assert_marker_only_taxonomy(' "$taxonomy_contract" \
    || ! grep -q '^def test_tm003_multiple_node_classifications_fail(' "$taxonomy_contract" \
    || ! grep -q '^def test_tm003_mixed_module_classifications_fail(' "$taxonomy_contract" \
    || ! grep -q '^def test_tm004_new_module_classification_needs_no_whitelist(' \
        "$taxonomy_contract" \
    || [ -n "$taxonomy_parallel_hits" ]; then
    echo "[x] Behavioral test taxonomy must stay owned by module-level pytestmark"
    [ -n "$taxonomy_parallel_hits" ] && echo "$taxonomy_parallel_hits"
    violations=$((violations + 1))
fi

lifecycle_topology_contract="tests/quality/test_ci_topology.py"
lifecycle_membership_hits=$(
    grep -En \
        'LIFECYCLE_SMOKE_(FULL_COUNT|MERGE_GROUP_COUNT|REQUIRED_COUNT|MERGE_GROUP_NODES)|expected_(full_count|merge_group_nodes|required_count)' \
        "$lifecycle_topology_contract" \
        || true
)
if ! grep -q '^def _validated_lifecycle_node_set(' "$lifecycle_topology_contract" \
    || ! grep -q '^def _assert_lifecycle_partition_sets(' "$lifecycle_topology_contract" \
    || ! grep -q 'merge_group < full' "$lifecycle_topology_contract" \
    || ! grep -q 'required == full - merge_group' "$lifecycle_topology_contract" \
    || [ -n "$lifecycle_membership_hits" ]; then
    echo "[x] Lifecycle marker partitions must be collection-derived, never count/list pinned"
    [ -n "$lifecycle_membership_hits" ] && echo "$lifecycle_membership_hits"
    violations=$((violations + 1))
fi

echo "[*] AC23: self-update release selection authority"
self_update_owner="src/apm_cli/commands/self_update.py"
self_update_owner_defs=$(grep -Ec \
    '^class _ResolvedSelfUpdateRelease:|^def _resolve_self_update_release\(' \
    "$self_update_owner" || true)
self_update_duplicate_defs=$(
    grep -rEn --include='*.py' \
        '^class _ResolvedSelfUpdateRelease:|^def _resolve_self_update_release\(' \
        src/apm_cli \
        | grep -v "^${self_update_owner}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if [ "$self_update_owner_defs" -ne 2 ] \
    || [ -n "$self_update_duplicate_defs" ] \
    || ! grep -q \
        'release = _resolve_self_update_release(latest_version)' \
        "$self_update_owner" \
    || ! grep -q \
        'resolved_ref = release.tag if release is not None else _INSTALL_SCRIPT_REF' \
        "$self_update_owner" \
    || ! grep -q 'env\[_ENV_VERSION\] = release.tag' "$self_update_owner" \
    || ! grep -q '_get_update_installer_url(release)' "$self_update_owner" \
    || ! grep -q '_build_self_update_installer_env(release)' "$self_update_owner" \
    || ! grep -q 'return _normalize_release_tag(pinned)' \
        src/apm_cli/utils/version_checker.py; then
    echo "[x] Self-update installer URL and VERSION must share _ResolvedSelfUpdateRelease"
    [ -n "$self_update_duplicate_defs" ] && echo "$self_update_duplicate_defs"
    violations=$((violations + 1))
fi

echo "[*] AC24: frozen install decision authority"
frozen_owner="src/apm_cli/install/service.py"
frozen_adapter="src/apm_cli/commands/install.py"
frozen_preflight_line=$(grep -n 'InstallService\.enforce_frozen(' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
frozen_migration_line=$(grep -n 'migrate_lockfile_if_needed(ctx\.apm_dir)' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
frozen_add_guard_line=$(grep -n 'InstallService\.reject_frozen_mutation(' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
frozen_root_guard_line=$(grep -n 'InstallService\.reject_missing_frozen_root(' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
root_redirect_line=$(grep -n '_root_redirect = install_root_redirect(' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
dedicated_mcp_line=$(grep -n '^[[:space:]]*_handle_mcp_install(' "$frozen_adapter" \
    | tail -1 | cut -d: -f1)
local_bundle_line=$(grep -n 'if len(packages) == 1 and not mcp_name' "$frozen_adapter" \
    | head -1 | cut -d: -f1)
frozen_duplicate_hits=$(
    grep -rEn --include='*.py' 'raise FrozenInstallError' src/apm_cli \
        | grep -v "^${frozen_owner}:" \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if ! grep -q '^    def enforce_frozen(' "$frozen_owner" \
    || ! grep -q '^    def reject_frozen_mutation(' "$frozen_owner" \
    || ! grep -q '^    def reject_missing_frozen_root(' "$frozen_owner" \
    || [ -z "$frozen_preflight_line" ] \
    || [ -z "$frozen_migration_line" ] \
    || [ "$frozen_preflight_line" -ge "$frozen_migration_line" ] \
    || [ -z "$frozen_add_guard_line" ] \
    || [ -z "$frozen_root_guard_line" ] \
    || [ -z "$root_redirect_line" ] \
    || [ "$frozen_root_guard_line" -ge "$root_redirect_line" ] \
    || [ -z "$dedicated_mcp_line" ] \
    || [ -z "$local_bundle_line" ] \
    || [ "$frozen_add_guard_line" -ge "$dedicated_mcp_line" ] \
    || [ "$frozen_add_guard_line" -ge "$local_bundle_line" ] \
    || [ -n "$frozen_duplicate_hits" ]; then
    echo "[x] Frozen install decisions must route through InstallService before mutation"
    [ -n "$frozen_duplicate_hits" ] && echo "$frozen_duplicate_hits"
    violations=$((violations + 1))
fi

echo "[*] AC25: root vs dependency MCP declaration-scope authority"
mcp_scope_owner="src/apm_cli/integration/mcp_config_view.py"
mcp_root_scope_body=$(awk '
    /^    def derive\(/ {flag=1}
    flag && /^    def / && !/^    def derive\(/ {exit}
    flag {print}
' "$mcp_scope_owner")
mcp_locked_scope_body=$(awk '
    /^def _collect_locked_dependencies\(/ {flag=1}
    flag && /^def / && !/^def _collect_locked_dependencies\(/ {exit}
    flag {print}
' "$mcp_scope_owner")
mcp_unlocked_scope_body=$(awk '
    /^def _collect_unlocked_compat\(/ {flag=1}
    flag && /^def / && !/^def _collect_unlocked_compat\(/ {exit}
    flag {print}
' "$mcp_scope_owner")
if [ "$(printf '%s\n' "$mcp_root_scope_body" \
        | grep -c 'root\.get_all_mcp_dependencies()')" -ne 1 ] \
    || printf '%s\n%s\n' "$mcp_locked_scope_body" "$mcp_unlocked_scope_body" \
        | grep -q 'get_all_mcp_dependencies()' \
    || [ "$(printf '%s\n' "$mcp_locked_scope_body" \
        | grep -c 'package\.get_mcp_dependencies()')" -ne 1 ] \
    || [ "$(printf '%s\n' "$mcp_unlocked_scope_body" \
        | grep -c 'package\.get_mcp_dependencies()')" -ne 1 ]; then
    echo "[x] Transitive MCP dependency scope must use production-only collection"
    violations=$((violations + 1))
fi

echo "[*] AC26: MCP container launcher authority"
mcp_container_owner="src/apm_cli/adapters/client/base.py"
mcp_container_consumers=(
    src/apm_cli/adapters/client/copilot.py
    src/apm_cli/adapters/client/codex.py
    src/apm_cli/adapters/client/gemini.py
    src/apm_cli/adapters/client/vscode.py
)
mcp_image_owner_defs=$(grep -rEc \
    '^[[:space:]]*def _ensure_docker_image_arg\(' \
    src/apm_cli/adapters/client --include='*.py' \
    | awk -F: '{sum += $2} END {print sum + 0}')
mcp_container_missing_consumers=$(grep -L \
    '_ensure_docker_image_arg(' "${mcp_container_consumers[@]}" || true)
if ! grep -q '_REGISTRY_TYPE_ALIASES = {"oci": "docker"}' "$mcp_container_owner" \
    || [ "$mcp_image_owner_defs" -ne 1 ] \
    || [ -n "$mcp_container_missing_consumers" ]; then
    echo "[x] MCP container launcher decisions must route through MCPClientAdapter"
    violations=$((violations + 1))
fi

if [ "$violations" -gt 0 ]; then
    echo "[x] $violations architecture boundary rule(s) failed"
    exit 1
fi

echo "[+] architecture boundary lint clean"
