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
lockfile_to_ref_body=$(awk '
    /^    def to_dependency_ref\(/ {flag=1}
    flag && /^    def / && !/to_dependency_ref/ {exit}
    flag && /^class / {exit}
    flag {print}
' src/apm_cli/deps/lockfile.py)
if ! echo "$lockfile_to_ref_body" | grep -q 'DependencyReference(' \
    || ! echo "$lockfile_to_ref_body" | grep 'skill_subset=' | grep -q 'self\.skill_subset'; then
    echo "[x] LockedDependency.to_dependency_ref must reconstruct skill_subset from self.skill_subset"
    violations=$((violations + 1))
fi
run_replay_body=$(awk '
    /^def run_replay\(/ {flag=1}
    flag && /^def / && !/run_replay/ {exit}
    flag {print}
' src/apm_cli/install/drift.py)
if ! echo "$run_replay_body" | grep -q 'integrate_package_primitives(' \
    || ! echo "$run_replay_body" | grep 'skill_subset=' \
        | grep -q 'package_info\.dependency_ref\.skill_subset'; then
    echo "[x] Audit replay must preserve locked skill subset intent"
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

if [ "$violations" -gt 0 ]; then
    echo "[x] $violations architecture boundary rule(s) failed"
    exit 1
fi

echo "[+] architecture boundary lint clean"
