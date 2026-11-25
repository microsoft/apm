"""Guard the shared single-file package owner and local admission routes."""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_dependency_policy import _unique_definitions
from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _name_calls_in,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-single-file-materialization"
MATERIALIZER = "src/apm_cli/deps/virtual_file.py"
LOCAL = "src/apm_cli/install/phases/local_content.py"
VALIDATION = "src/apm_cli/install/validation.py"
DOWNLOADER = "src/apm_cli/deps/github_downloader.py"
DRIFT = "src/apm_cli/install/drift.py"
LOCAL_VERDICT = "src/apm_cli/install/local_content_verdict.py"
MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"
REFERENCE = "src/apm_cli/models/dependency/reference.py"
UNINSTALL_ENGINE = "src/apm_cli/commands/uninstall/engine.py"
UNINSTALL_REANCHOR = "src/apm_cli/commands/uninstall/local_reanchor.py"


def check_single_file_materialization(provider: FactsProvider) -> tuple[Violation, ...]:
    """Reject split materializers and consumers bypassing source admission."""
    findings = _unique_definitions(
        provider,
        GUARD,
        MATERIALIZER,
        ("materialize_virtual_file_package", "virtual_file_subdirectory"),
    )
    findings.extend(
        _unique_definitions(
            provider,
            GUARD,
            LOCAL,
            (
                "read_local_package_source",
                "materialize_local_package",
                "materialize_local_package_for_replay",
                "local_source_matches_copy",
            ),
        )
    )
    routes = {
        DOWNLOADER: {"download_virtual_file_package": {"materialize_virtual_file_package"}},
        DRIFT: {"_materialize_install_path": {"materialize_local_package_for_replay"}},
        LOCAL: {
            "materialize_local_package": {
                "read_local_package_source",
                "materialize_virtual_file_package",
                "_copy_tree_dereferencing_validated",
            },
            "materialize_local_package_for_replay": {"materialize_local_package"},
            "_copy_local_package": {"materialize_local_package"},
            "compute_local_package_hash": {
                "read_local_package_source",
                "materialize_virtual_file_package",
                "compute_package_hash",
            },
            "read_local_package_source": {"virtual_file_subdirectory", "load_frontmatter"},
            # A copy is judged against the source image install normalizes (#5049).
            "local_source_matches_copy": {
                "compute_local_package_hash",
                "_normalize_legacy_local_plugin_for_replay",
                "verify_package_hash",
            },
        },
        UNINSTALL_ENGINE: {"_restage_changed_local_survivors": {"local_source_matches_copy"}},
        UNINSTALL_REANCHOR: {
            "_require_fresh_copy": {"local_source_matches_copy"},
            "refreshed_local_sources": {"local_source_matches_copy"},
        },
        LOCAL_VERDICT: {"local_content_reasons": {"compute_local_package_hash"}},
        MCP_VIEW: {
            "_package_manifest_path": {
                "read_local_package_source",
                "materialize_virtual_file_package",
            },
        },
        VALIDATION: {
            "_validate_local_package": {"read_local_package_source"},
            "_local_path_failure_reason": {"read_local_package_source"},
        },
        MATERIALIZER: {"materialize_virtual_file_package": {"virtual_file_subdirectory"}},
    }
    for path, functions in routes.items():
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        for function, required in functions.items():
            if not required <= _name_calls_in(facts, function):
                findings.append(
                    _summary(GUARD, path, f"{function} must use the single-file owners")
                )
        if path != MATERIALIZER and facts.tree_index is not None:
            for node in facts.tree_index.nodes:
                if isinstance(node, ast.Dict) and any(
                    isinstance(key, ast.Constant)
                    and key.value in (".agent.md", ".instructions.md", ".prompt.md")
                    for key in node.keys
                ):
                    findings.append(
                        _summary(
                            GUARD, path, "Single-file suffix mapping belongs to DependencyReference"
                        )
                    )
    facts, failures = _facts_for(provider, MATERIALIZER, GUARD)
    findings.extend(failures)
    if not any("DependencyReference.VIRTUAL_FILE_SUBDIRECTORIES" in line for line in facts.lines):
        findings.append(
            _summary(GUARD, MATERIALIZER, "Materialization must consume source vocabulary")
        )
    facts, failures = _facts_for(provider, REFERENCE, GUARD)
    findings.extend(failures)
    if not any(
        "VIRTUAL_FILE_EXTENSIONS = tuple(VIRTUAL_FILE_SUBDIRECTORIES)" in line
        for line in facts.lines
    ):
        findings.append(
            _summary(GUARD, REFERENCE, "Parsing and materialization must share suffixes")
        )
    return tuple(findings)
