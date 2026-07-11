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

if [ "$violations" -gt 0 ]; then
    echo "[x] $violations architecture boundary rule(s) failed"
    exit 1
fi

echo "[+] architecture boundary lint clean"
