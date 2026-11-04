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
SERVICE = "src/apm_cli/install/service.py"
MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"
REFERENCE = "src/apm_cli/models/dependency/reference.py"


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
            ("read_local_package_source", "materialize_local_file_for_replay"),
        )
    )
    routes = {
        DOWNLOADER: {"download_virtual_file_package": {"materialize_virtual_file_package"}},
        DRIFT: {"_materialize_install_path": {"materialize_local_file_for_replay"}},
        LOCAL: {
            "materialize_local_file_for_replay": {
                "read_local_package_source",
                "materialize_virtual_file_package",
            },
            "_copy_local_package": {
                "read_local_package_source",
                "materialize_virtual_file_package",
            },
            "compute_local_package_hash": {
                "read_local_package_source",
                "materialize_virtual_file_package",
                "compute_package_hash",
            },
            "read_local_package_source": {"virtual_file_subdirectory", "load_frontmatter"},
        },
        SERVICE: {"_verify_frozen_local_content": {"compute_local_package_hash"}},
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
