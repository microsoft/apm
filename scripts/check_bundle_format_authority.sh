#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-.}"
duplicates=$(
    grep -rEn --include='*.py' \
        '^(class BundleFormat|def resolve_bundle_format|def agent_plugin_warning)' \
        "$repo_root/src/apm_cli/bundle" \
        | grep -v '/src/apm_cli/bundle/formats.py:' \
        || true
)
if [ -n "$duplicates" ]; then
    echo "[x] Bundle format authority must live in src/apm_cli/bundle/formats.py"
    echo "$duplicates"
    exit 1
fi

agent_plugin_owner="$repo_root/src/apm_cli/agent_plugins/loader.py"
if [ -f "$agent_plugin_owner" ]; then
    agent_plugin_duplicates=$(
        grep -rEn --include='*.py' \
            '^(class AgentPlugin:|def (detect|load)_agent_plugin\()' \
            "$repo_root/src/apm_cli" \
            | grep -v '/src/apm_cli/agent_plugins/loader.py:.*def \(detect\|load\)_agent_plugin(' \
            | grep -v '/src/apm_cli/agent_plugins/ir.py:.*class AgentPlugin:' \
            || true
    )
    if [ -n "$agent_plugin_duplicates" ]; then
        echo "[x] Agent Plugin interpretation must live in src/apm_cli/agent_plugins/loader.py"
        echo "$agent_plugin_duplicates"
        exit 1
    fi
    if ! grep -q '^def detect_agent_plugin(' "$agent_plugin_owner" \
        || ! grep -q '^def load_agent_plugin(' "$agent_plugin_owner" \
        || ! grep -q '^def _read_admissible_root_manifest(' "$agent_plugin_owner" \
        || ! grep -q 'read_json_document(manifest_path, reject_duplicate_schema=True)' "$agent_plugin_owner" \
        || ! grep -q '^def _load_apm_configuration(' "$agent_plugin_owner"; then
        echo "[x] Agent Plugin loader must own admissibility, detection, loading, and manifest authority"
        exit 1
    fi

    model_validation="$repo_root/src/apm_cli/models/validation.py"
    format_detection="$repo_root/src/apm_cli/models/format_detection.py"
    legacy_parser="$repo_root/src/apm_cli/deps/plugin_parser.py"
    agent_validation_body=$(
        awk '
            /^def _validate_agent_plugin\(/ { capture = 1 }
            capture && /^def / && !/^def _validate_agent_plugin\(/ { exit }
            capture { print }
        ' "$model_validation"
    )
    if printf '%s\n' "$agent_validation_body" \
        | grep -Eq 'normalize_plugin_directory|synthesize_apm_yml_from_plugin' \
        || ! grep -q 'detect_agent_plugin(package_path)' "$format_detection" \
        || ! grep -q 'admit_legacy_plugin_manifest(package_path)' "$format_detection" \
        || ! grep -q 'admit_legacy_plugin_manifest(plugin_path)' "$legacy_parser"; then
        echo "[x] Agent Plugin classification must route through its loader, not Claude normalization"
        exit 1
    fi
fi
