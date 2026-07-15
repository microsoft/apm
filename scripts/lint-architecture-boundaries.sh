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
cleanup_claim_owner="src/apm_cli/install/phases/cleanup.py"
cleanup_claim_output=$(python3 scripts/check_cleanup_claim_owner.py "$cleanup_claim_owner" 2>&1)
cleanup_claim_status=$?
if [ "$cleanup_claim_status" -ne 0 ]; then
    echo "[x] Cleanup current-claim protection must use DeploymentReconciler"
    echo "$cleanup_claim_output"
    violations=$((violations + 1))
fi
check_pattern \
    "Resolver queue dedup must preserve ref constraints" \
    'queued_keys.*get_unique_key|get_unique_key.*queued_keys' \
    src/apm_cli/deps/apm_resolver.py
if ! grep -q 'DependencyReference.canonical_ado_coordinates' \
    src/apm_cli/deps/lockfile.py \
    || grep -Eq '(self\.)?repo_url\.split\(' src/apm_cli/deps/lockfile.py; then
    echo "[x] ADO lock coordinates must use DependencyReference"
    violations=$((violations + 1))
fi
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

if [ "$violations" -gt 0 ]; then
    echo "[x] $violations architecture boundary rule(s) failed"
    exit 1
fi

echo "[+] architecture boundary lint clean"
