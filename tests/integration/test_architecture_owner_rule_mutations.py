"""Frozen mutation-break matrix for every canonical architecture-owner guard.

`.apm/architecture/owners/*.json` names the durable decisions that have exactly
one canonical owner, and each owner lists the guard IDs that defend it.  The
runner already proves the registry and the rule catalog agree *by name* and that
every guard executes exactly once per run.  Names prove nothing about teeth: a
rule whose body was gutted still registers its guard ID and still runs.

This file supplies the missing half of that contract.  For each registered owner
guard it pins one minimal, meaningful source mutation -- a surgical edit that
kills a load-bearing sub-condition of the owning decision -- and asserts the one
rule that owns that guard reports a real `Violation`.
Coverage is a set equality against the live registry, so a new owner guard that
lands without a mutation case fails here instead of shipping a toothless rule.

Design notes:

* Mutations are applied through `run_selected_rules(..., source_overrides=...)`,
  which swaps the text a rule sees in memory.  Nothing is copied, nothing is
  written, and no CLI filter is involved.
* Exactly one rule executes per case, so an unrelated startup problem cannot be
  mistaken for teeth: the assertion is a `Violation` carrying that rule's ID,
  never a bare non-zero exit code.
* Mutations must stay surgical.  A syntax error, an unreadable file, or a
  deleted module would trip almost any rule for the wrong reason, so
  `test_owner_guard_mutation_is_surgical_and_meaningful` rejects those shapes
  before the linter ever runs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from scripts.architecture_linter.inventory import build_inventory
from scripts.architecture_linter.models import Rule
from scripts.architecture_linter.registry import load_registry
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = [
    pytest.mark.component,
    # The module-scoped baseline fixture below pays for one linter run across
    # every owner rule. `--dist loadgroup` (the xdist scheduler this repo's
    # sharded integration runs use) is the only scheduler that honors
    # `xdist_group`; without it these cases could be split across workers and
    # each worker would recompute that baseline.
    pytest.mark.xdist_group(name="architecture_owner_rule_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
OWNERS_DIR = ROOT / ".apm/architecture/owners"


@dataclass(frozen=True)
class MutationCase:
    """One guard's minimal source mutation and the rule that must catch it.

    `old` is a literal fragment of the current file at `path`; `new` replaces
    it (the first occurrence, or every occurrence when `replace_all` is set).
    `intent` records which load-bearing sub-condition the edit kills, so a
    future reader can tell a real regression proof from an incidental edit.
    """

    guard_id: str
    rule_id: str
    path: str
    old: str
    new: str
    intent: str
    replace_all: bool = False


MUTATIONS: tuple[MutationCase, ...] = (
    MutationCase(
        guard_id="contracts-tests-taxonomy-classification",
        rule_id="contracts-tests-taxonomy-classification",
        path="tests/quality/taxonomy_inventory_plugin.py",
        old='getattr(module, "pytestmark"',
        new='getattr(module, "pytest_mark"',
        intent="Taxonomy inventory stops reading the canonical module-level pytestmark.",
    ),
    MutationCase(
        guard_id="contracts-tooling-apply-to-placement",
        rule_id="contracts-tooling-apply-to-placement",
        path="src/apm_cli/primitives/parser.py",
        old="from apm_cli.utils.patterns import normalize_apply_to",
        new="from apm_cli.utils.patterns import literal_apply_to_top_level_roots",
        intent="Primitive parser drops its delegation to the applyTo normalization owner.",
    ),
    MutationCase(
        guard_id="contracts-tooling-cached-policy-shape",
        rule_id="contracts-tooling-cached-policy-shape",
        path="src/apm_cli/policy/discovery.py",
        old="_policy_to_dict(policy)",
        new="_policy_to_dict_v2(policy)",
        intent="Policy cache serializer stops routing through the canonical shape helper.",
    ),
    MutationCase(
        guard_id="contracts-tooling-compile-inventory",
        rule_id="registry_delegation.compile_inventory_authority",
        path="src/apm_cli/compilation/inventory.py",
        old='if path != root and (".git" in file_names or ".git" in child_dirs):',
        new="if False:",
        intent="Compile inventory stops identifying nested Git repository boundaries.",
    ),
    MutationCase(
        guard_id="contracts-tooling-dependency-identity",
        rule_id="contracts-tooling-dependency-identity",
        path="src/apm_cli/models/dependency/identity.py",
        old="    key = normalize_package_repo_url(",
        new="    key = canonical_repo_url(",
        intent="Unique-key construction skips the one sanctioned casing-normalization call.",
    ),
    MutationCase(
        guard_id="contracts-tooling-distributed-agents-output",
        rule_id="registry_delegation.compile_inventory_authority",
        path="src/apm_cli/compilation/agents_compiler.py",
        old="deploy_inventory.nested_repository_root_for(agents_path.parent)",
        new="None",
        intent="Distributed AGENTS output stops consulting the shared nested-repository boundary.",
    ),
    MutationCase(
        guard_id="contracts-tooling-frontmatter-yaml",
        rule_id="contracts-tooling-frontmatter-yaml",
        path="src/apm_cli/utils/yaml_io.py",
        old='def load_frontmatter(fd: Any, encoding: str = "utf-8-sig")',
        new='def load_frontmatter(fd: Any, encoding: str = "utf-8")',
        intent="Frontmatter owner loses its BOM-aware utf-8-sig decoding default.",
    ),
    MutationCase(
        guard_id="contracts-tooling-generation-footer",
        rule_id="contracts-tooling-generation-footer",
        path="src/apm_cli/compilation/footer.py",
        old="def build_generation_footer(",
        new="def build_generation_footer_v2(",
        intent="Generated footer owner loses the one canonical builder definition.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-read",
        rule_id="contracts-tooling-lockfile-read",
        path="src/apm_cli/deps/lockfile.py",
        old="    if read_only:\n",
        new="    if False and read_only:\n",
        intent="Read-only lockfile resolution stops guarding the mutating migration path.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-timestamp",
        rule_id="contracts-tooling-lockfile-timestamp",
        path="src/apm_cli/integration/mcp_integrator.py",
        old="_log = logging.getLogger(__name__)",
        new="_log = logging.getLogger(__name__)\nMCPIntegrator.generated_at = None",
        intent="An MCP consumer writes lockfile timestamp metadata outside its owner.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-timestamp-constructor",
        rule_id="contracts-tooling-lockfile-timestamp-constructor",
        path="src/apm_cli/integration/mcp_integrator.py",
        old="_log = logging.getLogger(__name__)",
        new=(
            "_log = logging.getLogger(__name__)\nLockFile(generated_at='2026-01-01T00:00:00+00:00')"
        ),
        intent="An MCP consumer sets timestamp metadata through the LockFile constructor.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-timestamp-fallback",
        rule_id="contracts-tooling-lockfile-timestamp-fallback",
        path="src/apm_cli/bundle/agent_plugin_exporter.py",
        old="import os",
        new='import os\n\nos.environ.get("SOURCE_DATE_EPOCH")',
        intent="An Agent Plugin consumer reimplements the reproducible timestamp fallback.",
    ),
    MutationCase(
        guard_id="contracts-tooling-project-yaml-write-delegation",
        rule_id="contracts-tooling-project-yaml-write-delegation",
        path="src/apm_cli/utils/yaml_io.py",
        old="    atomic_write_text(\n",
        new="    write_text_lf(\n",
        intent="The atomic project YAML writer bypasses the canonical atomic writer.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/agents_compiler.py",
        old="def _hand_authored_root_context_blocks_write(",
        new="def _hand_authored_root_context_blocks_write_disabled(",
        intent="Root context writes lose the canonical hand-authored ownership gate.",
    ),
    MutationCase(
        guard_id="hooks-integrations-copilot-cli-mcp-paths",
        rule_id="mutation_writes.copilot_cli_mcp_paths",
        path="src/apm_cli/adapters/client/copilot.py",
        old='"COPILOT_HOME"',
        new='"COPILOT_CFG_DIR"',
        intent="Copilot adapter stops owning the COPILOT_HOME MCP config root.",
    ),
    MutationCase(
        guard_id="hooks-integrations-hook-command-vocabulary",
        rule_id="mutation_writes.hook_command_vocabulary",
        path="src/apm_cli/integration/hook_command_paths.py",
        old="PLUGIN_ROOT_NAMES = (",
        new="PLUGIN_ROOT_NAMES_EXTRA = (",
        intent="Plugin-root hook command vocabulary loses its canonical constant.",
    ),
    MutationCase(
        guard_id="hooks-integrations-jetbrains-mcp-path",
        rule_id="mutation_writes.jetbrains_mcp_path",
        path="src/apm_cli/adapters/client/intellij.py",
        old="def _intellij_config_dir(",
        new="def _intellij_config_dir_impl(",
        intent="JetBrains MCP config-path owner no longer defines the single resolver.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-declaration-scope",
        rule_id="mutation_writes.mcp_declaration_scope",
        path="src/apm_cli/integration/mcp_config_view.py",
        old="root.get_all_mcp_dependencies()",
        new="root.get_mcp_deps()",
        intent="MCP config view stops deriving scope from the aggregated declaration owner.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-package-launcher",
        rule_id="mutation_writes.mcp_package_launcher",
        path="src/apm_cli/adapters/client/base.py",
        old='_REGISTRY_TYPE_ALIASES = {"oci": "docker"}',
        new='_REGISTRY_TYPE_ALIASES = {"oci": "podman"}',
        intent="Shared adapter loses the canonical OCI-to-docker launcher alias.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-passthrough-denylist",
        rule_id="mutation_writes.mcp_passthrough_denylist",
        path="src/apm_cli/models/dependency/mcp.py",
        old='frozenset({"enabled", "environment", "http_headers", "id"})',
        new='frozenset({"enabled", "http_headers", "id"})',
        intent="Shared MCP model stops denying the OpenCode environment alias.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-target-selection",
        rule_id="mutation_writes.mcp_target_selection",
        path="src/apm_cli/integration/mcp_integrator_install.py",
        old="parse_targets_field(apm_config)",
        new="parse_target_fields(apm_config)",
        intent="MCP install adapter stops parsing targets through the manifest owner.",
    ),
    MutationCase(
        guard_id="hooks-integrations-neutral-hook-contract",
        rule_id="mutation_writes.neutral_hook_contract",
        path="src/apm_cli/integration/hook_integrator.py",
        old="def _deploy_root_for_hook_rewrite(",
        new="def _deploy_root_for_rewrite_impl(",
        intent="HookIntegrator stops owning the neutral hook rewrite-scope resolver.",
    ),
    MutationCase(
        guard_id="hooks-integrations-user-root-scope",
        rule_id="mutation_writes.user_root_scope",
        path="src/apm_cli/integration/targets.py",
        old="include_scoped_in_user_root_context: bool = False",
        new="include_scoped_in_user_root_context: bool = True",
        intent="TargetProfile flips the user-root scoped-instruction eligibility default.",
    ),
    MutationCase(
        guard_id="install-deployment-audit-replay",
        rule_id="install-deployment-audit-replay",
        path="src/apm_cli/install/audit_replay.py",
        old="def prepare_ci_audit_replay(",
        new="def prepare_ci_audit_replay_disabled(",
        intent="CI audit scratch materialization loses its canonical entry point.",
    ),
    MutationCase(
        guard_id="install-deployment-base-integrator",
        rule_id="install-deployment-base-integrator",
        path="src/apm_cli/integration/base_integrator.py",
        old="    def check_collision(",
        new="    def check_collision_disabled(",
        intent="BaseIntegrator drops a mandatory file-level deploy/sync/cleanup method.",
    ),
    MutationCase(
        guard_id="install-deployment-bundle-native-layout",
        rule_id="install-deployment-bundle-native-layout",
        path="src/apm_cli/install/local_bundle_paths.py",
        old="if mapping is not None:",
        new='if target.name == "copilot" and mapping is not None:',
        intent="Local bundle routing branches on target names instead of target primitives.",
    ),
    MutationCase(
        guard_id="install-deployment-executable-trust-context",
        rule_id="install-deployment-executable-trust-context",
        path="src/apm_cli/security/executables.py",
        old="def exec_trust_context_for_project(",
        new="def exec_trust_context_for_project_disabled(",
        intent="Executable trust loses its canonical project-context resolver.",
    ),
    MutationCase(
        guard_id="install-deployment-frozen-mutation-eligibility",
        rule_id="install-deployment-frozen-mutation-eligibility",
        path="src/apm_cli/install/service.py",
        old="    def enforce_frozen(",
        new="    def enforce_frozen_disabled(",
        intent="InstallService stops owning the frozen-install mutation preflight.",
    ),
    MutationCase(
        guard_id="install-deployment-install-scope-selection",
        rule_id="install-deployment-install-scope-selection",
        path="src/apm_cli/commands/install.py",
        old="user_scope=is_user_scope(scope)",
        new="user_scope=False",
        intent="Direct MCP target resolution stops consuming the command's scope decision.",
    ),
    MutationCase(
        guard_id="install-deployment-lifecycle-serialization",
        rule_id="install-deployment-lifecycle-serialization",
        path="src/apm_cli/commands/config.py",
        old="@serialized_lifecycle\ndef set(",
        new="def set(",
        intent="Config mutation stops routing through the canonical lifecycle lock.",
    ),
    MutationCase(
        guard_id="install-deployment-lsp-lifecycle",
        rule_id="install-deployment-lsp-lifecycle",
        path="src/apm_cli/install/lsp/integration.py",
        old="def reconcile_lsp_after_uninstall(",
        new="def reconcile_lsp_after_uninstall_disabled(",
        intent="LSP reconciliation loses its canonical lifecycle entry point.",
    ),
    MutationCase(
        guard_id="install-deployment-lsp-target-contract",
        rule_id="install-deployment-lsp-target-contract",
        path="src/apm_cli/integration/lsp_integrator.py",
        old="return BaseIntegrator.resolve_deploy_path(relative_path, project_root)",
        new="return spec.path(project_root, user_scope=False)",
        intent="Claude LSP plugin writes bypass the canonical deployment-path gate.",
    ),
    MutationCase(
        guard_id="install-deployment-mcp-ownership-migration",
        rule_id="install-deployment-mcp-ownership-migration",
        path="src/apm_cli/install/mcp/ownership.py",
        old="def resolve_mcp_target_servers(",
        new="def resolve_mcp_target_servers_disabled(",
        intent="Legacy MCP target ownership adoption loses its canonical resolver.",
    ),
    MutationCase(
        guard_id="install-deployment-mcp-registry-resolution",
        rule_id="install-deployment-mcp-registry-resolution",
        path="src/apm_cli/registry/client.py",
        old="def resolve_mcp_registry_url(",
        new="def resolve_mcp_registry_url_disabled(",
        intent="The registry client loses the canonical MCP registry precedence resolver.",
    ),
    MutationCase(
        guard_id="install-deployment-outcome",
        rule_id="install-deployment-outcome",
        path="src/apm_cli/install/outcome.py",
        old="def finalize_install_result(",
        new="def finalize_install_result_disabled(",
        intent="Install outcome owner stops defining the result finalizer.",
    ),
    MutationCase(
        guard_id="install-deployment-package-target-authorization",
        rule_id="install-deployment-package-target-authorization",
        path="src/apm_cli/install/target_filter.py",
        old="def resolve_effective_package_targets(",
        new="def resolve_effective_package_targets_disabled(",
        intent="Effective package-target authorization loses its single resolver.",
    ),
    MutationCase(
        guard_id="install-deployment-prospective-dry-run-plan",
        rule_id="install-deployment-prospective-dry-run-plan",
        path="src/apm_cli/install/presentation/dry_run.py",
        old="for dep in plan.selected_lsp_dependencies:",
        new="for dep in plan.lsp_dependencies:",
        intent="Dry-run LSP rendering bypasses plan-owned service selection.",
    ),
    MutationCase(
        guard_id="install-deployment-provenance-state",
        rule_id="install-deployment-provenance-state",
        path="src/apm_cli/commands/prune.py",
        old="reconcile_owner_references",
        new="reconcile_owner_references_disabled",
        intent="Prune stops reconciling deployment provenance through the state owner.",
    ),
    MutationCase(
        guard_id="install-deployment-request-defaults",
        rule_id="install-deployment-request-defaults",
        path="src/apm_cli/install/request.py",
        old="trust_bin: bool | None = None",
        new="trust_bin: bool | None = True",
        intent="InstallRequest hard-codes an invocation default instead of deferring it.",
    ),
    MutationCase(
        guard_id="install-deployment-resolution-replacement",
        rule_id="install-deployment-resolution-replacement",
        path="src/apm_cli/install/resolution_staging.py",
        old="def prepare_replacement(",
        new="def prepare_replacement_disabled(",
        intent="Resolution staging owner drops a mandatory replacement-activation method.",
    ),
    MutationCase(
        guard_id="install-deployment-source-plan",
        rule_id="install-deployment-source-plan",
        path="src/apm_cli/install/services.py",
        old="source_plan = DeployableSourcePlan.create(",
        new="source_plan_x = DeployableSourcePlan.create(",
        intent="Install services stop binding the shared deployable source plan.",
    ),
    MutationCase(
        guard_id="install-deployment-target-file-contraction",
        rule_id="install-deployment-target-file-contraction",
        path="src/apm_cli/install/manifest_reconcile.py",
        old="def reconcile_target_deployed_files(",
        new="def reconcile_target_deployed_files_disabled(",
        intent="Target-scoped deployed-file contraction loses its canonical owner.",
    ),
    MutationCase(
        guard_id="install-deployment-uninstall-reachability",
        rule_id="install-deployment-uninstall-reachability",
        path="src/apm_cli/commands/uninstall/engine.py",
        old="    if not candidate_orphans:",
        new="    get_apm_dependencies = None\n    if not candidate_orphans:",
        intent="Uninstall engine grows a parallel manifest walk outside deps/reachability.py.",
    ),
    MutationCase(
        guard_id="install-deployment-uninstall-selection",
        rule_id="install-deployment-uninstall-selection",
        path="src/apm_cli/models/dependency/selection.py",
        old="def select_manifest_dependency(",
        new="def select_manifest_dependency_disabled(",
        intent="Dependency CLI selection loses the one manifest-selection function.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-agent-plugin-contract",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/ir.py",
        old="    mcp_servers: tuple[AgentPluginMcpServer, ...]",
        new="    mcp_server_list: tuple[AgentPluginMcpServer, ...]",
        intent="Portable AgentPluginComponents field set drifts from the v1 contract.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-catalog-manifest",
        rule_id="marketplace-integrations-catalog-manifest",
        path="src/apm_cli/deps/_shared.py",
        old="def materialize_marketplace_manifest(",
        new="def materialize_marketplace_manifest_X(",
        intent="Catalog-only marketplace manifest materialization loses its owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-copilot-ownership",
        rule_id="marketplace-integrations-copilot-ownership",
        path="src/apm_cli/copilot_plugins/registrar.py",
        old="def synchronize_copilot_plugins(",
        new="def synchronize_copilot_plugins_X(",
        intent="Copilot marketplace ownership loses its single synchronization owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-legacy-skill-membership",
        rule_id="marketplace-integrations-legacy-skill-membership",
        path="src/apm_cli/deps/plugin_parser.py",
        old="def normalized_plugin_skill_sources(",
        new="def normalized_plugin_skill_sources_X(",
        intent="Legacy plugin skill membership loses its normalization owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-metadata-enrichment",
        rule_id="marketplace-integrations-metadata-enrichment",
        path="src/apm_cli/marketplace/builder.py",
        old="class MetadataEnrichmentResult(",
        new="class MetadataEnrichmentResultX(",
        intent="Marketplace metadata enrichment loses its canonical certification result.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-native-registration",
        rule_id="marketplace-integrations-native-registration",
        path="src/apm_cli/copilot_plugins/capability.py",
        old="def resolve_native_registration_capability(",
        new="def resolve_native_registration_capability_X(",
        intent="Native agent-plugin registration admission loses its capability resolver.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-output-path",
        rule_id="marketplace-integrations-output-path",
        path="src/apm_cli/marketplace/output_profiles.py",
        old="def resolve_effective_output_path(",
        new="def resolve_effective_output_path_X(",
        intent="Effective marketplace output path loses its single resolver.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-package-construction",
        rule_id="marketplace-integrations-package-construction",
        path="src/apm_cli/models/apm_package.py",
        old="result = cls.from_mapping(",
        new="result = cls.from_other(",
        intent="from_apm_yml stops routing interpreted construction through from_mapping.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-package-format-precedence",
        rule_id="marketplace-integrations-package-format-precedence",
        path="src/apm_cli/bundle/local_bundle.py",
        old="package_type, _ = detect_package_type(",
        new="package_type, _ = bypass_package_type_precedence(",
        intent="Agent Plugin ingress bypasses the package-format precedence owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-package-projection",
        rule_id="marketplace-integrations-package-projection",
        path="src/apm_cli/agent_plugins/projection.py",
        old="def project_agent_plugin_package(",
        new="def project_agent_plugin_package_X(",
        intent="Agent-plugin package projection loses its single projection owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-producer-admission",
        rule_id="marketplace-integrations-producer-admission",
        path="src/apm_cli/bundle/agent_plugin_exporter.py",
        old="def _require_portable_agent_plugin(",
        new="def _require_portable_agent_plugin_X(",
        intent="Agent-plugin producer loses its portability admission gate.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-raw-diagnostics",
        rule_id="marketplace-integrations-raw-diagnostics",
        path="src/apm_cli/marketplace/models.py",
        old="structural_errors: tuple[str, ...] = ()",
        new="structural_errors: tuple[str, ...] = ('placeholder',)",
        intent="Raw structural diagnostics stop originating empty in the model owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-source-admission",
        rule_id="marketplace-integrations-source-admission",
        path="src/apm_cli/marketplace/client.py",
        old=("        host = source.host\n        host_info = AuthResolver.classify_host"),
        new=(
            "        host = _host_from_url(source.url)\n"
            "        host_info = AuthResolver.classify_host"
        ),
        intent="Marketplace client reintroduces source-host parsing outside the owner.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-tag-pattern",
        rule_id="marketplace-integrations-tag-pattern",
        path="src/apm_cli/marketplace/tag_pattern.py",
        old="def validate_tag_pattern(",
        new="def validate_tag_pattern_X(",
        intent="Marketplace tag-pattern owner loses its validation entry point.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-version-precedence",
        rule_id="marketplace-integrations-version-precedence",
        path="src/apm_cli/marketplace/version_check.py",
        old="return _read_plugin_json_version(package_root)",
        new="return (None, 'disabled')",
        intent="Local marketplace version precedence skips the plugin.json fallback read.",
    ),
    MutationCase(
        guard_id="registry-delegation-bootstrap-project-name",
        rule_id="registry_delegation.bootstrap_project_name",
        path="src/apm_cli/core/project_name.py",
        old='DEFAULT_BOOTSTRAP_PROJECT_NAME = "my-project"',
        new='DEFAULT_BOOTSTRAP_PROJECT_NAME = "my-app"',
        intent="Bootstrap project-name owner drifts off its pinned fallback value.",
    ),
    MutationCase(
        guard_id="registry-delegation-compiled-output-writes",
        rule_id="registry_delegation.compiled_output_writes",
        path="src/apm_cli/compilation/context_optimizer.py",
        old="class DirectoryAnalysis:",
        new=(
            "def _write_compiled_output(path, text): path.write_text(text)\n\n"
            "class DirectoryAnalysis:"
        ),
        intent="Compilation gains a direct write that bypasses CompiledOutputWriter.",
    ),
    MutationCase(
        guard_id="registry-delegation-install-target-selection",
        rule_id="registry_delegation.install_target_selection",
        path="src/apm_cli/install/pipeline.py",
        old="target_decision = resolve_effective_target_decision(",
        new="target_decision = _resolve_effective_target_decision_REMOVED(",
        intent="Install pipeline stops resolving the shared EffectiveTargetDecision.",
        replace_all=True,
    ),
    MutationCase(
        guard_id="registry-delegation-output-diagnostics",
        rule_id="registry_delegation.output_diagnostics",
        path="src/apm_cli/commands/marketplace/__init__.py",
        old=(
            'return STATUS_SYMBOLS["warning"] if check.informational '
            'else STATUS_SYMBOLS["error"]\n'
            '    return STATUS_SYMBOLS["info"] if check.informational '
            'else STATUS_SYMBOLS["check"]'
        ),
        new=(
            'return "[!]" if check.informational else "[x]"\n'
            '    return "[i]" if check.informational else "[+]"'
        ),
        intent="Doctor status icons hard-code glyphs instead of console STATUS_SYMBOLS.",
    ),
    MutationCase(
        guard_id="registry-delegation-runtime-descriptors",
        rule_id="registry_delegation.runtime_descriptors",
        path="src/apm_cli/commands/runtime.py",
        old=(
            '@click.argument("runtime_name", type=click.Choice(runtime_names()))\n'
            '@click.option("--version", help="Specific version to install")'
        ),
        new=(
            '@click.argument("runtime_name", type=click.Choice(["copilot", "codex"]))\n'
            '@click.option("--version", help="Specific version to install")'
        ),
        intent="Runtime command hard-codes runtime names instead of the registry vocabulary.",
    ),
    MutationCase(
        guard_id="registry-delegation-target-vocabulary",
        rule_id="registry_delegation.target_vocabulary",
        path="src/apm_cli/commands/uninstall/engine.py",
        old="config_target = list(apm_package.canonical_targets)",
        new="config_target = list(apm_package.targets)",
        intent="A manifest consumer reads raw targets instead of canonical_targets.",
    ),
    MutationCase(
        guard_id="transport-platform-ado-validation-bearer-fallback",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/install/validation.py",
        old="        fallback = auth_resolver.execute_with_bearer_fallback(",
        new="        fallback = _bypass_ado_bearer_fallback(",
        intent="Install validation bypasses canonical ADO PAT-to-bearer fallback.",
    ),
    MutationCase(
        guard_id="transport-platform-ado-validation-caller-config",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/deps/clone_engine.py",
        old=(
            "                    attempt.effective_url or attempt_url,\n"
            "                    base_env=host.git_env,"
        ),
        new=(
            "                    attempt.effective_url or attempt_url,\n"
            "                    base_env=None,"
        ),
        intent="Tokenless ADO clone attempts discard caller-owned Git configuration.",
    ),
    MutationCase(
        guard_id="transport-platform-ado-validation-clone-bearer-fallback",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/deps/clone_engine.py",
        old="                    fallback = host.auth_resolver.execute_with_bearer_fallback(",
        new="                    fallback = _execute_ado_bearer_fallback_locally(",
        intent="Clone execution bypasses AuthResolver's PAT-to-bearer owner.",
    ),
    MutationCase(
        guard_id="transport-platform-ado-validation-helper-suppression",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/core/auth.py",
        old='        if host_kind == "ado" and not token:',
        new='        if host_kind == "generic" and not token:',
        intent="Tokenless ADO environments can reactivate native Git helpers.",
    ),
    MutationCase(
        guard_id="transport-platform-artifactory-full-commit-sha",
        rule_id="transport-platform-artifactory-full-commit-sha",
        path="src/apm_cli/utils/github_host.py",
        old="    if is_full_commit_sha(ref):",
        new="    if False and is_full_commit_sha(ref):",
        intent="Artifactory archive routing stops consulting the full commit SHA owner.",
    ),
    MutationCase(
        guard_id="transport-platform-artifactory-netrc-isolation",
        rule_id="transport-platform-artifactory-netrc-isolation",
        path="src/apm_cli/deps/artifactory_entry.py",
        old="                    with _NoNetrcSession() as session:",
        new="                    with _requests.Session() as session:",
        intent="Direct Artifactory entry requests regain ambient netrc credentials.",
    ),
    MutationCase(
        guard_id="transport-platform-git-cache-identity",
        rule_id="transport-platform-git-cache-identity",
        path="src/apm_cli/deps/shared_clone_cache.py",
        old=(
            "        repository = normalize_repo_url(repository_url)\n"
            "        repository_shard = cache_shard_key(repository)"
        ),
        new=(
            "        repository = repository_url\n"
            "        repository_shard = cache_shard_key(repository)"
        ),
        intent="Shared clone cache keys on a raw URL instead of the normalized identity.",
    ),
    MutationCase(
        guard_id="transport-platform-git-child-environment",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='        "GIT_CONFIG",',
        new='        "GIT_CONFIG_UNSAFE",',
        intent="Git children retain the repository-local config override.",
    ),
    MutationCase(
        guard_id="transport-platform-git-clone-hooks-disabled",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='    return "-c", "core.hooksPath=/dev/null"',
        new='    return "-c", "core.hooksPath=.githooks"',
        intent="Dependency clones reactivate repository-provided checkout hooks.",
    ),
    MutationCase(
        guard_id="transport-platform-git-clone-templates-disabled",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='    return ("--template=",)',
        new='    return ("--template=.git-templates",)',
        intent="Dependency clones load repository template configuration.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-redaction",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/git_file_transport.py",
        old="            safe_stderr = redact_git_diagnostic(result.stderr.strip())",
        new="            safe_stderr = result.stderr.strip()",
        intent="Sparse Git failures expose Authorization header values.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-redaction-debug",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/github_downloader.py",
        old='        print(f"[DEBUG] {redact_git_diagnostic(message)}", file=sys.stderr)',
        new='        print(f"[DEBUG] {message}", file=sys.stderr)',
        intent="Downloader debug output renders raw Git diagnostics.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-sanitizer-ownership",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/github_downloader_validation.py",
        old='_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")',
        new=(
            '_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")\n\n'
            "def _sanitize_git_error(value: str) -> str:\n"
            "    return value"
        ),
        intent="A downloader helper introduces a competing Git diagnostic sanitizer.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-sanitizer-ownership-downloader",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/github_downloader.py",
        old="        return redact_git_diagnostic(error_message)",
        new="        return error_message",
        intent="The downloader compatibility sanitizer stops delegating to the owner.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-token-shapes",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old="github_pat_",
        new="github_bad_",
        intent="Fine-grained GitHub PATs stop being redacted from Git diagnostics.",
    ),
    MutationCase(
        guard_id="transport-platform-git-diagnostic-token-shapes-jwt",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old="eyJ[A-Za-z0-9_-]",
        new="bad[A-Za-z0-9_-]",
        intent="Bare AAD bearer JWTs stop being redacted from Git diagnostics.",
    ),
    MutationCase(
        guard_id="transport-platform-git-semver-preflight",
        rule_id="transport-platform-git-semver-preflight",
        path="src/apm_cli/install/helpers/ref_reuse.py",
        old="if not is_git_semver_resolution_eligible(dep_ref):",
        new="if False:  # bypassed eligibility check",
        intent="Ref reuse drops the semver preflight eligibility gate.",
    ),
    MutationCase(
        guard_id="transport-platform-git-semver-remote-auth",
        rule_id="transport-platform-git-semver-preflight",
        path="src/apm_cli/install/helpers/ref_reuse.py",
        old="        git_env_factory=resolver_git_env_factory,",
        new="        git_env=None,",
        intent="Semver resolution stops creating remote Git environments lazily on cache miss.",
    ),
    MutationCase(
        guard_id="transport-platform-git-single-remote-fetch",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/cache/git_cache.py",
        old="            fallback_fetch_args += [url, *_FALLBACK_REFSPECS]",
        new='            fallback_fetch_args += ["--all"]',
        intent="A failed SHA fetch fans out to every configured remote.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-credentials-out-of-argv",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/clone_engine.py",
        old='                    token="",\n                    auth_scheme="basic",',
        new='                    token=token,\n                    auth_scheme="basic",',
        intent="Authenticated GitHub clone URLs regain process-visible credentials.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-header-specificity",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='                "--get-urlmatch",',
        new='                "--get-regexp",',
        intent="Git URL-scoped header precedence falls back to config order.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-header-specificity-fence",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old="    if not managed and not reset_headers and not helper_reset:",
        new="    if not managed and not reset_headers:",
        intent="Credential-helper-only fences fail to remove ambient helpers.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-header-specificity-fence-malformed-values",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='    if any(character in value for character in ("\\r", "\\n", "\\0")):',
        new="    if False:",
        intent="Ambient extraHeader values can retain header-injection delimiters.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-header-specificity-fence-managed-auth",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old='    env[_MANAGED_GIT_AUTH_INTENT_ENV] = "1"',
        new='    env[_MANAGED_GIT_AUTH_INTENT_ENV] = "0"',
        intent="Managed authentication loses its explicit rewrite-safety intent.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-enforcement",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/utils/git_env.py",
        old="    effective_url, snapshot = _validated_git_url_rewrite_policy(",
        new=(
            "    effective_url, snapshot = "
            "(lambda *_args, **_kwargs: (None, _GitConfigSnapshot((), (), ())))("
        ),
        intent="The canonical network environment bypasses URL rewrite validation.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-once",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/clone_engine.py",
        old="                url = attempt.requested_url",
        new="                url = attempt.effective_url",
        intent="Clone execution applies an already-resolved URL rewrite a second time.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-recovery",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/marketplace/client.py",
        old='            reason = f"{reason}; {exc.recovery_hint}"',
        new="            reason = reason",
        intent="Marketplace wrapping drops the safe Git rewrite inspection command.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-routing",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/bare_cache.py",
        old="                    remote_env = git_network_env(url, env, git_dir=target)",
        new="                    remote_env = sanitize_for_git(env)",
        intent="Shared bare clones bypass the canonical network Git environment owner.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-routing-validation",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/install/validation.py",
        old="    transport_plan = ado_downloader._transport_selector.select(",
        new="    transport_plan = _legacy_validation_transport_plan(",
        intent="Positional validation bypasses the canonical TransportSelector.",
    ),
    MutationCase(
        guard_id="transport-platform-git-url-rewrite-safety",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/git_auth_env.py",
        old='class GitAuthEnvBuilder:\n    """Build the various git env dicts the downloader needs."""',
        new=(
            "class GitAuthEnvBuilder:\n"
            '    """Build the various git env dicts the downloader needs."""\n\n'
            "    @staticmethod\n"
            "    def has_https_to_http_url_rewrite(\n"
            "        remote_url: str, env: dict[str, str]\n"
            "    ) -> bool:\n"
            "        return False"
        ),
        intent="GitAuthEnvBuilder regains a parallel URL rewrite safety policy.",
    ),
    MutationCase(
        guard_id="transport-platform-github-throttle",
        rule_id="transport-platform-github-throttle",
        path="src/apm_cli/deps/download_strategies.py",
        old="def _debug(message: str) -> None:",
        new=(
            "def _check_rate(resp):\n"
            '    return resp.headers.get("X-RateLimit-Remaining")\n\n\n'
            "def _debug(message: str) -> None:"
        ),
        intent="A parallel throttle classifier appears outside deps/github_rate_limit.py.",
    ),
    MutationCase(
        guard_id="transport-platform-host-credential-resolution",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/deps/download_strategies.py",
        old="def _debug(message: str) -> None:",
        new=(
            "def _bad_token_read(self):\n"
            "    return self._host.ado_token\n\n\n"
            "def _debug(message: str) -> None:"
        ),
        intent="A downloader reads an ADO token off the host instead of via AuthResolver.",
    ),
    MutationCase(
        guard_id="transport-platform-network-host-parsing",
        rule_id="transport-platform-network-host-parsing",
        path="src/apm_cli/install/mcp/warnings.py",
        old="ip = parse_host_address(bare)",
        new="ip = None  # bypass",
        intent="MCP warnings stop classifying host literals through utils/net.py.",
    ),
    MutationCase(
        guard_id="transport-platform-ref-freshness",
        rule_id="transport-platform-ref-freshness",
        path="src/apm_cli/install/helpers/ref_seed.py",
        old="if not freshness_policy.allows_lock_seed:",
        new="if ctx.update_refs or ctx.refresh:",
        intent="Ref seeding makes a parallel freshness decision outside RefFreshnessPolicy.",
    ),
    MutationCase(
        guard_id="transport-platform-revision-pin-outcome",
        rule_id="transport-platform-revision-pin-outcome",
        path="src/apm_cli/commands/update.py",
        old="logger.revision_pins_retained(resolution.skips)",
        new="logger.revision_pins_retained(())",
        intent="Update discards resolver-provided retained revision pins.",
    ),
    MutationCase(
        guard_id="transport-platform-self-update-resolution",
        rule_id="transport-platform-self-update-resolution",
        path="src/apm_cli/commands/self_update.py",
        old="resolved_ref = release.tag if release is not None else _INSTALL_SCRIPT_REF",
        new="resolved_ref = _INSTALL_SCRIPT_REF",
        intent="Self-update installer ref stops sharing the resolved release decision.",
    ),
    MutationCase(
        guard_id="transport-platform-sparse-symlink-validation",
        rule_id="transport-platform-sparse-symlink-validation",
        path="src/apm_cli/deps/github_downloader.py",
        old="            return _repair(env)\n",
        new="            return True\n",
        intent="Downloader skips the dangling-cone-symlink repair owner.",
    ),
    MutationCase(
        guard_id="transport-platform-url-path-security",
        rule_id="transport-platform-url-path-security",
        path="src/apm_cli/marketplace/yml_schema.py",
        old='decode_url_path_segments(parsed.path, context="sourceBase")',
        new="parsed.path  # dropped decode_url_path_segments",
        intent="sourceBase parsing skips containment-checked URL path decoding.",
    ),
    MutationCase(
        guard_id="transport-platform-windows-stable-path",
        rule_id="transport-platform-windows-stable-path",
        path="install.ps1",
        old='$currentExe = Join-Path $currentDir "apm.exe"',
        new='$currentExe = Join-Path $currentDir "apm_tool.exe"',
        intent="Windows installer drifts off the stable current/apm.exe executable path.",
    ),
)

CASE_IDS: tuple[str, ...] = tuple(case.guard_id for case in MUTATIONS)


@cache
def _source(path: str) -> str:
    """Return the current on-disk text of `path`, read at most once per session."""
    return (ROOT / path).read_text(encoding="utf-8")


@cache
def _rules_by_guard() -> dict[str, Rule]:
    """Map each registered guard ID to the one rule that enforces it."""
    return {guard: rule for rule in registered_rules() for guard in rule.guard_ids}


@cache
def _registry_guard_ids() -> frozenset[str]:
    """Return every guard ID declared by the real canonical-owner registry."""
    rule_guard_ids = frozenset(_rules_by_guard())
    registry = load_registry(
        OWNERS_DIR,
        build_inventory(ROOT).files,
        known_guard_ids=rule_guard_ids,
        owner_specific_guard_ids=rule_guard_ids,
    )
    return frozenset(guard for owner in registry.owners for guard in owner.guards)


def _mutate(case: MutationCase) -> str:
    """Return `case`'s mutated source, proving its fragment still exists verbatim."""
    original = _source(case.path)
    occurrences = original.count(case.old)
    assert occurrences >= 1, (
        f"{case.guard_id}: fragment absent from {case.path}; the file drifted and this "
        f"mutation no longer tests anything: {case.old!r}"
    )
    if not case.replace_all:
        assert occurrences == 1, (
            f"{case.guard_id}: fragment is ambiguous in {case.path} "
            f"({occurrences} occurrences); pin a unique fragment or set replace_all"
        )
    return original.replace(case.old, case.new, -1 if case.replace_all else 1)


@pytest.fixture(scope="module")
def baseline_violated_rule_ids() -> frozenset[str]:
    """Run every owner rule once, unmutated, so each case can prove attribution."""
    report = run_selected_rules(ROOT, tuple(sorted({case.rule_id for case in MUTATIONS})))
    return frozenset(violation.rule_id for violation in report.violations)


def test_matrix_covers_every_registry_guard_exactly_once() -> None:
    """The matrix guard set must equal the registry guard set, one case each."""
    matrix_guard_ids = [case.guard_id for case in MUTATIONS]
    duplicates = sorted({guard for guard in matrix_guard_ids if matrix_guard_ids.count(guard) > 1})

    assert not duplicates, f"guards covered by more than one mutation case: {duplicates}"
    assert frozenset(matrix_guard_ids) == _registry_guard_ids()
    assert len(MUTATIONS) == len(_registry_guard_ids())


def test_matrix_case_order_is_deterministic() -> None:
    """Cases stay sorted by guard ID so parameterized IDs never reshuffle."""
    assert list(CASE_IDS) == sorted(CASE_IDS)


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_case_targets_the_single_rule_that_owns_its_guard(case: MutationCase) -> None:
    """Each case must name the one registered rule declaring its guard ID."""
    owners = [rule for rule in registered_rules() if case.guard_id in rule.guard_ids]

    assert len(owners) == 1, f"{case.guard_id}: expected one owning rule, found {len(owners)}"
    assert owners[0].id == case.rule_id


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_owner_guard_mutation_is_surgical_and_meaningful(case: MutationCase) -> None:
    """Mutations must edit real semantics, not break reading or parsing.

    A syntax error, an unreadable file, or a truncated module would trip most
    rules for reasons that have nothing to do with the guarded decision, so
    those shapes are rejected before the mutation is ever linted.
    """
    original = _source(case.path)
    mutated = _mutate(case)
    removed_lines = len(original.splitlines()) - len(mutated.splitlines())

    assert case.old.isascii() and case.new.isascii()
    assert case.intent.isascii() and case.intent.endswith(".")
    assert mutated != original
    assert mutated.strip(), f"{case.guard_id}: mutation emptied {case.path}"
    assert removed_lines <= 2, (
        f"{case.guard_id}: mutation deleted {removed_lines} lines of {case.path}; "
        "owner guards must be broken surgically, not by truncation"
    )
    if case.path.endswith(".py"):
        ast.parse(mutated, filename=case.path)


def test_owner_rules_report_nothing_before_mutation(
    baseline_violated_rule_ids: frozenset[str],
) -> None:
    """Every owner rule is clean at HEAD, so any violation below is the mutation."""
    assert baseline_violated_rule_ids == frozenset()


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_owner_rule_catches_its_guard_mutation(
    case: MutationCase, baseline_violated_rule_ids: frozenset[str]
) -> None:
    """The rule owning each guard must report a Violation for its mutation.

    Only that one rule executes, and only a `Violation` carrying its ID counts:
    a non-zero exit code from unrelated startup, registry, or read failures is
    explicitly not accepted as proof that the guard has teeth.
    """
    assert case.rule_id not in baseline_violated_rule_ids, (
        f"{case.guard_id}: {case.rule_id} already violates unmutated"
    )

    report = run_selected_rules(ROOT, (case.rule_id,), source_overrides={case.path: _mutate(case)})

    blamed = tuple(item for item in report.violations if item.rule_id == case.rule_id)
    broken_input = tuple(
        failure.stage
        for failure in report.failures
        if failure.stage in (f"read:{case.path}", f"parse:{case.path}")
    )
    assert not broken_input, (
        f"{case.guard_id}: mutation made {case.path} unreadable or unparsable {broken_input}"
    )
    assert blamed, (
        f"{case.guard_id}: {case.rule_id} stayed silent after mutating {case.path} "
        f"({case.intent}) -- the guard has no teeth for this owner. "
        f"failures={[(f.stage, f.message) for f in report.failures]}"
    )
