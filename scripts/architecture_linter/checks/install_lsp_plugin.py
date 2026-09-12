"""Claude LSP plugin and executable-trust architecture guards."""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_EXECUTABLE_TRUST = "install-deployment-executable-trust-context"
GUARD_LSP_TARGET_CONTRACT = "install-deployment-lsp-target-contract"
GUARD_LSP_LIFECYCLE = "install-deployment-lsp-lifecycle"

_EXECUTABLES = "src/apm_cli/security/executables.py"
_APPROVE_COMMAND = "src/apm_cli/commands/approve.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_INSTALL_TEMPLATE = "src/apm_cli/install/template.py"
_LSP_INTEGRATOR = "src/apm_cli/integration/lsp_integrator.py"
_LSP_PIPELINE = "src/apm_cli/install/lsp/integration.py"
_LOCAL_BUNDLE = "src/apm_cli/install/local_bundle_handler.py"
_INSTALL_SERVICES = "src/apm_cli/install/services.py"
_SKILL_SUPPORT = "src/apm_cli/integration/skill_support.py"


def _missing_tokens(
    provider: FactsProvider,
    rule_id: str,
    required: dict[str, tuple[str, ...]],
) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    for path, tokens in required.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        missing = [token for token in tokens if not _present(facts, token)]
        if missing:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    "Canonical routing tokens are missing: " + ", ".join(missing),
                )
            )
    return tuple(findings)


def check_executable_trust_context(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install and update must consume one effective executable-trust owner."""
    return _missing_tokens(
        provider,
        GUARD_EXECUTABLE_TRUST,
        {
            _EXECUTABLES: (
                "def exec_trust_context_for_project(",
                "def locked_dependency_approval_keys(",
                "def local_bundle_approval_key(",
                'owner = getattr(dependency, "resolved_by", None)',
                'approval_keys = getattr(dependency, "approval_keys", ())',
            ),
            _APPROVE_COMMAND: ("approval_identity=locked.get_unique_key()",),
            _INSTALL_COMMAND: ("effective_bundle_allow_map as _effective_bundle_allow_map",),
            _INSTALL_TEMPLATE: ("trust_ctx = exec_trust_context_for_project(",),
            _LSP_PIPELINE: (
                "effective_allow_executables = effective_exec_map_for_project(",
                "if not effective_allow_resolved:",
            ),
            _LOCAL_BUNDLE: (
                "bundle_approval_key = local_bundle_approval_key(",
                "approval_key=bundle_approval_key,",
            ),
            _INSTALL_SERVICES: (
                "is_package_approved(allow_executables, approval_key, EXEC_TYPE_CANVAS)",
            ),
        },
    )


def check_lsp_target_contract(provider: FactsProvider) -> tuple[Violation, ...]:
    """LSP target shape and deployment paths must route through LSPIntegrator."""
    return _missing_tokens(
        provider,
        GUARD_LSP_TARGET_CONTRACT,
        {
            _LSP_INTEGRATOR: (
                "def reserved_project_skill_names(",
                "def _prepare_target_config(",
                "prepared_targets.append((runtime, spec, prepared))",
                "BaseIntegrator.resolve_deploy_path(relative_path, project_root)",
                "allowed_prefixes=(relative_path,)",
                "locked_dependency_approval_keys(locked_dependency)",
                "approval_keys=approval_keys",
            ),
            _SKILL_SUPPORT: (
                "LSPIntegrator.reserved_project_skill_names(skills_dir, project_root)",
            ),
        },
    )


def check_lsp_lifecycle(provider: FactsProvider) -> tuple[Violation, ...]:
    """LSP collection and reconciliation must route through the pipeline owner."""
    findings = list(
        _missing_tokens(
            provider,
            GUARD_LSP_LIFECYCLE,
            {
                _LSP_PIPELINE: (
                    "transitive_lsp = filter_lsp_by_allow_executables(",
                    "lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)",
                    "def reconcile_lsp_after_uninstall(",
                    "lsp_target_servers=new_targets",
                    "managed_target_servers=old_lsp_targets",
                ),
                _LOCAL_BUNDLE: (
                    "def effective_bundle_allow_map(",
                    "return effective_exec_map_for_project(",
                    "def _wire_bundle_lsp_servers(",
                    "force=force,",
                ),
            },
        )
    )
    pipeline, failures = _facts_for(provider, _LSP_PIPELINE, GUARD_LSP_LIFECYCLE)
    if failures:
        return tuple([*findings, *failures])
    lines = getattr(pipeline, "lines", ())
    filter_line = next(
        (
            index
            for index, line in enumerate(lines)
            if "transitive_lsp = filter_lsp_by_allow_executables(" in line
        ),
        None,
    )
    dedup_line = next(
        (
            index
            for index, line in enumerate(lines)
            if "lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)" in line
        ),
        None,
    )
    if filter_line is not None and dedup_line is not None and filter_line >= dedup_line:
        findings.append(
            _summary(
                GUARD_LSP_LIFECYCLE,
                _LSP_PIPELINE,
                "Transitive LSP trust filtering must run before first-wins deduplication",
            )
        )
    lifecycle_calls = (
        "LSPIntegrator.collect_transitive(",
        "LSPIntegrator.resolve_target_runtimes(",
        "LSPIntegrator.install(",
        "LSPIntegrator.remove_stale(",
        "LSPIntegrator.update_lockfile(",
    )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path in {_LSP_INTEGRATOR, _LSP_PIPELINE}:
            continue
        facts, failures = _facts_for(provider, path, GUARD_LSP_LIFECYCLE)
        if failures:
            findings.extend(failures)
            continue
        direct_calls = [token for token in lifecycle_calls if _present(facts, token)]
        if direct_calls:
            findings.append(
                _summary(
                    GUARD_LSP_LIFECYCLE,
                    path,
                    "LSP lifecycle calls must route through install/lsp/integration.py: "
                    + ", ".join(direct_calls),
                )
            )
    return tuple(findings)
