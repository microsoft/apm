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

format_owner="$repo_root/src/apm_cli/bundle/formats.py"
if ! grep -q '^PREFERRED_PLUGIN_FORMAT = BundleFormat.CLAUDE_PLUGIN$' "$format_owner"; then
    echo "[x] Agent Plugin preferred-default flip is reserved for T10 after G3"
    exit 1
fi
if ! grep -q '^    "plugin": BundleFormat.CLAUDE_PLUGIN,$' "$format_owner"; then
    echo "[x] The plugin format token must remain Claude-compatible for apm-action@v1"
    exit 1
fi
if ! grep -q '^    if len(selections) > 1:$' "$format_owner" \
    || ! grep -q '^    return PREFERRED_PLUGIN_FORMAT$' "$format_owner"; then
    echo "[x] Bundle selectors and no-flag behavior must route through the canonical format seam"
    exit 1
fi
for command in \
    "$repo_root/src/apm_cli/commands/pack.py" \
    "$repo_root/src/apm_cli/commands/plugin/init.py"; do
    if grep -Eq "^[[:space:]]*([\"']--plugin[\"'],|@click\\.option\\([\"']--plugin[\"'])" \
        "$command"; then
        echo "[x] Portable Agent Plugins must use --format agent-plugin, not --plugin"
        echo "$command"
        exit 1
    fi
done

agent_plugin_exporter="$repo_root/src/apm_cli/bundle/agent_plugin_exporter.py"
if [ -f "$agent_plugin_exporter" ] \
    && grep -Eq 'validate_(plugin_manifest|mcp_config|lsp_extension)_(document|file)' \
        "$agent_plugin_exporter"; then
    echo "[x] Agent Plugin producer must not duplicate canonical loader validation"
    exit 1
fi
if [ -f "$agent_plugin_exporter" ]; then
    portable_gate_line=$(
        grep -n '^    _require_portable_agent_plugin(dropped_surfaces)$' \
            "$agent_plugin_exporter" \
            | cut -d: -f1 \
            || true
    )
    dry_run_line=$(
        grep -n '^    if dry_run:$' "$agent_plugin_exporter" \
            | head -n 1 \
            | cut -d: -f1 \
            || true
    )
    output_mutation_line=$(
        grep -n '^    output_dir.mkdir(' "$agent_plugin_exporter" \
            | head -n 1 \
            | cut -d: -f1 \
            || true
    )
    if [ "$(grep -Ec '^def _require_portable_agent_plugin\(' "$agent_plugin_exporter")" -ne 1 ] \
        || [ -z "$portable_gate_line" ] \
        || [ -z "$dry_run_line" ] \
        || [ -z "$output_mutation_line" ] \
        || [ "$portable_gate_line" -ge "$dry_run_line" ] \
        || [ "$portable_gate_line" -ge "$output_mutation_line" ]; then
        echo "[x] Agent Plugin portable-surface admission must fail before output projection"
        exit 1
    fi
fi

reproducible_archive="$repo_root/src/apm_cli/bundle/reproducible_archive.py"
if [ -f "$reproducible_archive" ] \
    && { grep -q '\.read_bytes(' "$reproducible_archive" \
        || grep -q 'source\.read(' "$reproducible_archive" \
        || grep -q 'BytesIO' "$reproducible_archive" \
        || ! grep -q 'shutil.copyfileobj(source, member)' "$reproducible_archive" \
        || ! grep -q 'archive.addfile(info, source)' "$reproducible_archive"; }; then
    echo "[x] Reproducible archives must stream file payloads without full-file buffering"
    exit 1
fi

init_owner="$repo_root/src/apm_cli/commands/init.py"
if [ -f "$init_owner" ] \
    && { ! grep -q 'PREFERRED_PLUGIN_FORMAT is BundleFormat.AGENT_PLUGIN' "$init_owner" \
        || ! grep -q 'plugin = load_agent_plugin(staged_root)' "$init_owner"; }; then
    echo "[x] Plugin scaffolding must share the preferred-format seam and canonical reload"
    exit 1
fi

agent_plugin_owner="$repo_root/src/apm_cli/agent_plugins/loader.py"
if [ -f "$agent_plugin_owner" ]; then
    schema_router="$repo_root/src/apm_cli/bundle/local_bundle.py"
    if [ -f "$schema_router" ]; then
        if ! grep -q '^class PluginSchemaRoute(Enum):' "$schema_router" \
            || ! grep -q '^def classify_plugin_manifest_schema(' "$schema_router" \
            || ! grep -q '^def route_agent_plugin_package(' "$schema_router" \
            || ! grep -q 'if schema_id == PLUGIN_SCHEMA_ID:' "$schema_router" \
            || grep -Eq 'is_agent_plugin_schema_id|supports_plugin_schema_id|validate_plugin_manifest_document' \
                "$schema_router"; then
            echo "[x] Plugin schema routing must live in bundle/local_bundle.py and select exact IDs"
            exit 1
        fi
        if grep -Eq 'agent_plugin_(runtime|state)|install\.mcp|security\.executables|lockfile.*v3' \
            "$schema_router"; then
            echo "[x] Plugin schema routing must not depend on deployment or runtime state"
            exit 1
        fi
    fi
    if [ "$(grep -c 'classify_plugin_manifest_schema' "$agent_plugin_owner")" -lt 4 ]; then
        echo "[x] Agent Plugin loading and legacy admission must share the schema router"
        exit 1
    fi

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
    package_owner="$repo_root/src/apm_cli/models/apm_package.py"
    projection_owner="$repo_root/src/apm_cli/agent_plugins/projection.py"
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
        || ! grep -q 'admit_legacy_plugin_manifest(plugin_path)' "$legacy_parser" \
        || ! grep -q 'classify_plugin_manifest_schema(manifest)' "$legacy_parser"; then
        echo "[x] Agent Plugin classification must route through its loader, not Claude normalization"
        exit 1
    fi
    for ingress_requirement in \
        "$repo_root/src/apm_cli/install/sources.py:4" \
        "$repo_root/src/apm_cli/install/template.py:2" \
        "$repo_root/src/apm_cli/deps/apm_resolver.py:2" \
        "$repo_root/src/apm_cli/deps/_shared.py:2" \
        "$repo_root/src/apm_cli/deps/github_downloader.py:2" \
        "$repo_root/src/apm_cli/deps/registry/resolver.py:3"; do
        ingress="${ingress_requirement%:*}"
        minimum="${ingress_requirement##*:}"
        if [ -f "$ingress" ] \
            && [ "$(grep -c 'route_agent_plugin_package' "$ingress")" -lt "$minimum" ]; then
            echo "[x] Package ingress must converge through route_agent_plugin_package"
            echo "$ingress"
            exit 1
        fi
    done
    marketplace_resolver="$repo_root/src/apm_cli/marketplace/resolver.py"
    if [ -f "$marketplace_resolver" ] \
        && grep -Eq 'route_agent_plugin_package|detect_agent_plugin|\$schema' \
            "$marketplace_resolver"; then
        echo "[x] Marketplace resolution must defer schema admission to materialized ingress"
        exit 1
    fi
    install_command="$repo_root/src/apm_cli/commands/install.py"
    if [ -f "$install_command" ]; then
        local_bundle_gate_line=$(
            grep -n 'enforce_agent_plugin_deployment_boundary(bundle_info=_bundle_info)' \
                "$install_command" | cut -d: -f1 || true
        )
        local_bundle_handler_line=$(
            grep -n 'from \.\.install\.local_bundle_handler import install_local_bundle' \
                "$install_command" | cut -d: -f1 || true
        )
        executable_trust_line=$(
            grep -n 'from \.\.security\.executables import read_bundle_allow_executables' \
                "$install_command" | cut -d: -f1 || true
        )
        if [ -z "$local_bundle_gate_line" ] \
            || [ -z "$local_bundle_handler_line" ] \
            || [ -z "$executable_trust_line" ] \
            || [ "$local_bundle_gate_line" -ge "$local_bundle_handler_line" ] \
            || [ "$local_bundle_gate_line" -ge "$executable_trust_line" ]; then
            echo "[x] Local bundles must hit the native boundary before deployment preparation"
            exit 1
        fi
    fi

    projection_duplicates=$(
        grep -rEn --include='*.py' \
            '^def project_agent_plugin_package\(' \
            "$repo_root/src/apm_cli" \
            | grep -v '/src/apm_cli/agent_plugins/projection.py:' \
            || true
    )
    normalization_callers=$(
        grep -rEn --include='*.py' \
            'normalize_plugin_directory\(' \
            "$repo_root/src/apm_cli" \
            | grep -v '/src/apm_cli/deps/plugin_parser.py:.*def normalize_plugin_directory(' \
            | grep -v '/src/apm_cli/models/validation.py:' \
            | grep -v '/src/apm_cli/install/drift.py:' \
            || true
    )
    raw_agent_package_construction=$(
        grep -rEn --include='*.py' \
            'APMPackage\(' \
            "$repo_root/src/apm_cli/agent_plugins" \
            || true
    )
    if [ ! -f "$projection_owner" ] \
        || [ "$(grep -Ec '^def project_agent_plugin_package\(' "$projection_owner")" -ne 1 ] \
        || [ -n "$projection_duplicates" ] \
        || [ "$(grep -Ec '^    def from_mapping\(' "$package_owner")" -ne 1 ] \
        || ! printf '%s\n' "$agent_validation_body" \
            | grep -q 'package = project_agent_plugin_package(plugin)' \
        || ! printf '%s\n' "$agent_validation_body" | grep -q 'result.package = package' \
        || grep -Eq 'read_json_document|json\.load|yaml\.' "$projection_owner" \
        || [ -n "$raw_agent_package_construction" ] \
        || [ -n "$normalization_callers" ]; then
        echo "[x] Agent Plugin compatibility packages must route through the projection owner"
        [ -n "$projection_duplicates" ] && echo "$projection_duplicates"
        [ -n "$raw_agent_package_construction" ] && echo "$raw_agent_package_construction"
        [ -n "$normalization_callers" ] && echo "$normalization_callers"
        exit 1
    fi
    if ! python3 "$(dirname "$0")/check_agent_plugin_projection_boundary.py" \
        --root "$repo_root"; then
        echo "[x] Agent Plugin projection AST boundary failed"
        exit 1
    fi
fi
