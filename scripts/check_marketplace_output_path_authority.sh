#!/usr/bin/env bash
# Verify marketplace consumers delegate output-path selection to its owner.

set -euo pipefail

repo_root="${1:-.}"
cd "$repo_root"

marketplace_output_path_owner="src/apm_cli/marketplace/output_profiles.py"
marketplace_output_path_consumers=(
    "src/apm_cli/core/build_orchestrator.py"
    "src/apm_cli/marketplace/builder.py"
    "src/apm_cli/marketplace/drift_check.py"
)
marketplace_output_path_parallel_hits=$(
    grep -En \
        'project_root[[:space:]]*/[[:space:]]*config\.[A-Za-z_]+\.output|getattr\(config,[[:space:]]*profile\.config_attr|config\.output_specs' \
        "${marketplace_output_path_consumers[@]}" \
        || true
)
if [ "$(grep -Ec '^def resolve_effective_output_path\(' "$marketplace_output_path_owner" || true)" -ne 1 ] \
    || ! grep -q 'resolve_effective_output_path(' src/apm_cli/core/build_orchestrator.py \
    || ! grep -q 'resolve_effective_output_path(' src/apm_cli/marketplace/builder.py \
    || ! grep -q 'resolve_effective_output_path(' src/apm_cli/marketplace/drift_check.py \
    || [ -n "$marketplace_output_path_parallel_hits" ]; then
    echo "[x] Marketplace output paths must route through marketplace/output_profiles.py"
    [ -n "$marketplace_output_path_parallel_hits" ] && echo "$marketplace_output_path_parallel_hits"
    exit 1
fi
