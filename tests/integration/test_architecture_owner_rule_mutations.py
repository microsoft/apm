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
from tests.utils.xdist_shared import shared_session_value, sources_digest

pytestmark = [
    pytest.mark.component,
]

ROOT = Path(__file__).resolve().parents[2]
OWNERS_DIR = ROOT / ".apm/architecture/owners"
ARCHITECTURE_SOURCE_ROOTS = ("src/apm_cli", "scripts/architecture_linter", ".apm/architecture")


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/deps/registry/extractor.py",
            "    safe_extract_tar(tar, dest_root, error_type=UnsafeTarballError)",
            "    tar.extractall(dest_root)",
            id="registry-tar-extractall",
        ),
        pytest.param(
            "src/apm_cli/deps/download_strategies.py",
            "return read_zip_member(zf, entry, error_type=ArchiveError)",
            "return archive_handle.open(entry).read()",
            id="renamed-zip-member-read",
        ),
        pytest.param(
            "src/apm_cli/bundle/packer.py",
            "from ..utils.archive import (",
            'import zipfile\n_SIDE = zipfile.ZipFile("side.zip")\nfrom ..utils.archive import (',
            id="unlisted-archive-opener",
        ),
    ],
)
def test_archive_member_reads_cannot_bypass_the_limits_owner(
    path: str, original: str, replacement: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "transport-platform-archive-extraction-limits"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/install/drift.py",
            "deployment_modules_dir, deployment_frame = replay_deployment_frame(",
            "deployment_modules_dir, deployment_frame = private_deployment_frame(",
            id="replay-call",
        ),
        pytest.param(
            "src/apm_cli/install/audit_target_roots.py",
            "return get_modules_dir(scope), (scratch_root, get_deploy_root(scope))",
            'return scratch_root / ".apm" / APM_MODULES_DIR, (scratch_root, scratch_root)',
            id="user-scope-live-frame",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "package_info.deployment_frame = deployment_frame",
            "package_info.deployment_frame = None",
            id="replay-live-link-frame",
        ),
        pytest.param(
            "src/apm_cli/compilation/link_resolver.py",
            "target_location = live_root / target_location.relative_to(replay_root)",
            "target_location = target_location",
            id="resolver-live-link-frame",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "replay_path=install_path,",
            "replay_path=package_info.deployment_package_root,",
            id="native-replay-image",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "lock.dependencies[_SELF_KEY] = lock.project_self_entry()",
            'lock.dependencies[_SELF_KEY] = LockedDependency(repo_url="<self>", local_path=".")',
            id="replay-inline-self-entry",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "and _project_has_root_primitives(project_root)",
            'and (project_root / ".apm").is_dir()',
            id="replay-root-predicate",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            "lock.dependencies[_SELF_KEY] = lock.project_self_entry()",
            'lock.dependencies[_SELF_KEY] = LockedDependency(repo_url="<self>", local_path=".")',
            id="load-inline-self-entry",
        ),
    ],
)
def test_audit_replay_scope_projection_cannot_bypass_owner(
    path: str, original: str, replacement: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "install-deployment-audit-replay"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("rule", "path", "original", "replacement"),
    [
        pytest.param(
            "install-deployment-lsp-lifecycle",
            "src/apm_cli/install/lsp/integration.py",
            "        recorded_targets=recorded_lsp_targets(lockfile),\n",
            "        recorded_targets=(),\n",
            id="uninstall-reselects-lsp-targets",
        ),
        pytest.param(
            "registry_delegation.audit_target_selection",
            "src/apm_cli/install/lsp/integration.py",
            "def recorded_lsp_targets(",
            "def resolve_deployment_target_decision(**kwargs): return None\n\n\n"
            "def recorded_lsp_targets(",
            id="second-deployment-target-owner",
        ),
    ],
)
def test_uninstall_lsp_target_selection_cannot_bypass_owner(
    rule: str, path: str, original: str, replacement: str
) -> None:
    """Uninstall keeps survivors on the recorded LSP targets through one owner (#4059)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/install/drift.py",
            "skill_subset=selection.skill_subset,",
            "skill_subset=package_info.dependency_ref.skill_subset,",
            id="replay-local-skill-subset",
        ),
        pytest.param(
            "src/apm_cli/install/template.py",
            "dep_target_subset=selection.target_subset,\n            ctx=ctx,\n"
            "            allow_executables=effective_allow_map(ctx),\n"
            "            trust_bin=ctx.trust_bin,",
            "dep_target_subset=dependency.target_subset,\n            ctx=ctx,\n"
            "            allow_executables=effective_allow_map(ctx),\n"
            "            trust_bin=ctx.trust_bin,",
            id="preflight-local-target-subset",
        ),
        pytest.param(
            "src/apm_cli/install/services.py",
            "skill_subset=effective_dependency_selection(None, ctx).skill_subset,",
            "skill_subset=None,",
            id="local-content-unallowlisted-none",
        ),
        pytest.param(
            "src/apm_cli/install/deployment_plan.py",
            "    skill_subset: tuple | None,\n",
            "    skill_subset: tuple | None = None,\n",
            id="optional-skill-subset",
        ),
        pytest.param(
            "src/apm_cli/commands/uninstall/engine.py",
            "            effective_dependency_selection(dep_ref).target_subset,\n",
            "            dep_ref.target_subset,\n",
            id="uninstall-local-target-subset",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            "                effective_dependency_selection(dep_ref).target_subset,\n",
            "                dep_ref.target_subset,\n",
            id="hook-reconcile-local-target-subset",
        ),
        pytest.param(
            "src/apm_cli/install/phases/lockfile.py",
            "effective_dependency_selection(locked_dep, self.ctx).skill_subset",
            "effective_deploy_skill_subset(False, self.ctx.skill_subset, None)",
            id="lockfile-records-outside-owner",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "    from apm_cli.install.services import IntegratorBundle, integrate_package_primitives\n",
            "    from apm_cli.install.services import IntegratorBundle\n"
            "    from apm_cli.install.services import integrate_package_primitives as _deploy\n"
            "    integrate_package_primitives = None\n"
            "    _deploy(skill_subset=package_info.dependency_ref.skill_subset)\n",
            id="aliased-deploy-call",
        ),
    ],
)
def test_deploy_selection_cannot_bypass_owner(path: str, original: str, replacement: str) -> None:
    """Deploy calls take skill and target selection from one owner (#3760)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "install-deployment-effective-selection"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/models/dependency/identity.py",
            "    if port:\n",
            "    if False:\n",
            id="builder-drops-port",
        ),
        pytest.param(
            "src/apm_cli/models/dependency/reference.py",
            "            host=self.host,\n            port=self.port,\n"
            '            source="local" if self.is_local else self.source,\n',
            "            host=self.host,\n"
            '            source="local" if self.is_local else self.source,\n',
            id="reference-drops-port",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            "            host=self.host,\n            port=self.port,\n"
            "            source=self.source,\n",
            "            host=self.host,\n            source=self.source,\n",
            id="lockfile-drops-port",
        ),
        pytest.param(
            "src/apm_cli/security/executables.py",
            "canonical = build_port_blind_dependency_key(reference)",
            "canonical = reference.get_unique_key()",
            id="deny-keeps-port",
        ),
        pytest.param(
            "src/apm_cli/models/dependency/identity.py",
            "return replace(dependency, port=None).get_unique_key()",
            "return dependency.get_unique_key()",
            id="port-blind-key-keeps-port",
        ),
    ],
)
def test_dependency_unique_key_cannot_drop_the_port(
    path: str, original: str, replacement: str
) -> None:
    """Both identity models key a non-default port through one builder (#3927)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "contracts-tooling-dependency-identity"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/compilation/root_context_protection.py",
            "if not lexical_root and path.name in accepted_markers_by_name:",
            "if False:",
            id="helper-root-only",
        ),
        pytest.param(
            "src/apm_cli/compilation/agents_compiler.py",
            "for path in claude_result.content_map\n            if self._hand_authored",
            "for path in claude_result.content_map\n            if path.parent == self.base_dir"
            " and self._hand_authored",
            id="claude-root-only",
        ),
    ],
)
def test_context_protection_cannot_regress_to_root_only(
    path: str, original: str, replacement: str
) -> None:
    """Hand-authored context protection covers nested placements too (#3655)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "contracts-tooling-root-context-write-eligibility"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/install/service.py",
            "drifted, unresolved = verify_deployed_file_hashes(deploy_root, committed, targets)",
            "drifted, unresolved = [], []",
            id="frozen-skips-hash-verification",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "hash_mismatches, unresolved_hash_paths = verify_deployed_file_hashes(",
            "hash_mismatches, unresolved_hash_paths = _local_hash_loop(",
            id="audit-bypasses-shared-verifier",
        ),
    ],
)
def test_frozen_and_audit_share_deployed_hash_verifier(
    path: str, original: str, replacement: str
) -> None:
    """``install --frozen`` and ``audit --ci`` re-verify hashes through one owner (#3654)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(original) == 1
    rule = "install-deployment-frozen-mutation-eligibility"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement"),
    [
        pytest.param(
            "src/apm_cli/install/services.py",
            "(staged_hashes if _staged else deployed_hashes)[record] = (",
            "(deployed_hashes if not _staged else {})[record] = (",
            id="install-stages-without-hash",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "staged_drift = lock.staged_hash_drift(project_root)",
            "staged_drift: list[tuple[str, str, str]] = []",
            id="audit-skips-staged-drift",
        ),
        pytest.param(
            "src/apm_cli/commands/compile/cli.py",
            "    report_staged_hash_drift(logger, _src)\n",
            "",
            id="compile-skips-staged-drift",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            "actual = compute_file_hash(file_path)",
            "actual = hashlib.sha256(file_path.read_bytes()).hexdigest()",
            id="staged-drift-uses-second-hash-scheme",
        ),
        pytest.param(
            "src/apm_cli/install/services.py",
            'staged_fallback_hashes[record] = f"sha256:{desired_hash}"',
            'skipped_hash = f"sha256:{desired_hash}"',
            id="collision-skipped-staging-left-unhashed",
        ),
        pytest.param(
            "src/apm_cli/install/phases/lockfile.py",
            "lockfile.carry_local_staging_from(self.ctx.existing_lockfile)",
            "lockfile.local_staged_files = list(self.ctx.existing_lockfile.local_staged_files)",
            id="declarative-install-drops-staged-hashes",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "staged_drift = lock.staged_hash_drift(project_root)",
            "lock.local_staged_file_hashes = {}; staged_drift = lock.staged_hash_drift(project_root)",
            id="audit-writes-staged-hashes-outside-owner",
        ),
        pytest.param(
            "src/apm_cli/commands/compile/watcher.py",
            '        report_staged_hash_drift(self.logger, Path("."))\n',
            "",
            id="watch-recompile-skips-staged-drift",
        ),
        pytest.param(
            "src/apm_cli/install/local_bundle_handler.py",
            "if deployed or staged_instructions or staged_fallbacks:",
            "if deployed or staged_instructions:",
            id="all-skipped-bundle-drops-fallback-hashes",
        ),
        pytest.param(
            "src/apm_cli/install/manifest_reconcile.py",
            "    survivors.update(lockfile.local_staged_files)\n",
            "    survivors.update(lockfile.local_staged_files)\n    lockfile.local_staged_files += []\n",
            id="non-owner-writes-staged-files",
        ),
    ],
)
def test_staged_compile_input_drift_routes_through_lockfile_owner(
    path: str, original: str, replacement: str
) -> None:
    """Staged bundle input is hashed and verified by one owner (#3718)."""
    source = _source(path)
    assert source.count(original) == 1
    rule = "install-deployment-bundle-compile-staging"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert any(item.rule_id == rule for item in report.violations)


@pytest.mark.parametrize(
    ("path", "original", "replacement", "refused"),
    [
        (
            "src/apm_cli/policy/ci_checks.py",
            "except (AgentPluginDeploymentBoundaryError, CodexNativeError) as exc:",
            "except AgentPluginDeploymentBoundaryError as exc:",
            False,
        ),
        (
            "src/apm_cli/policy/ci_checks.py",
            "except (AgentPluginDeploymentBoundaryError, CodexNativeError) as exc:",
            "except (AgentPluginDeploymentBoundaryError, Exception) as exc:",
            True,
        ),
    ],
)
def test_native_typed_error_guard_retains_legacy_and_rejects_broad_handlers(
    path: str, original: str, replacement: str, refused: bool
) -> None:
    """Adding the Codex error must not weaken the established portable error contract."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert original in source
    rule = "marketplace-integrations-projection-boundary"
    report = run_selected_rules(
        ROOT, (rule,), source_overrides={path: source.replace(original, replacement)}
    )
    assert report.failures == ()
    assert bool(report.violations) is refused


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/utils/content_hash.py",
            "_package_files(package_path, native=True)",
            "_package_files(package_path, native=False)",
        ),
        (
            "src/apm_cli/utils/content_hash.py",
            "path.stat().st_mode & stat.S_IXUSR",
            "False",
        ),
        (
            "src/apm_cli/codex_plugins/registrar.py",
            "def owns_codex_dependency(",
            "def duplicate_owns_codex_dependency(",
        ),
        (
            "src/apm_cli/install/phases/codex_plugins.py",
            "ctx.package_deployed_files.get(key)",
            "False",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "owns_codex_dependency(",
            "noncanonical_ownership(",
        ),
        (
            "src/apm_cli/deps/package_validator.py",
            'getattr(package, "codex_plugin", None) is not None',
            "False",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "ownership_cache=native_ownership_cache",
            "ownership_cache=None",
        ),
        (
            "src/apm_cli/codex_plugins/registrar.py",
            "verify_codex_source_identity(modules_dir, records=records, **item.plugin.source_identity())",
            "None",
        ),
        (
            "src/apm_cli/codex_plugins/registrar.py",
            "record.source_commit == source_commit",
            "False",
        ),
        (
            "src/apm_cli/install/sources.py",
            "is_codex_source_pin(cached_commit)",
            "bool(cached_commit)",
        ),
        (
            "src/apm_cli/install/sources.py",
            "verify_marker(self.install_path, cached_commit)",
            "None",
        ),
        (
            "src/apm_cli/install/drift.py",
            "codex_records = verify_codex_source_identity(",
            "codex_records = noncanonical_source_check(",
        ),
        (
            "src/apm_cli/install/drift.py",
            "records=codex_records",
            "records=None",
        ),
        (
            "src/apm_cli/codex_plugins/admission.py",
            'result["codex_source_identity"] = plugin.source_identity()',
            "pass",
        ),
        (
            "src/apm_cli/codex_plugins/registrar.py",
            "record = replace(record, source_commit=item.plugin.source_commit)",
            "pass",
        ),
        (
            "src/apm_cli/policy/ci_checks.py",
            "except (AgentPluginDeploymentBoundaryError, CodexNativeError) as exc:",
            "except AgentPluginDeploymentBoundaryError as exc:",
        ),
    ],
)
def test_codex_native_content_and_ownership_gates_cannot_be_bypassed(
    path: str, old: str, new: str
) -> None:
    """Native integrity and cold-cache identity use their canonical owners."""
    rule_id = "marketplace-integrations-codex-native-lifecycle"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/package_selection.py",
            "selected_dependency_identity(package, resolved_refs)",
            "DependencyReference.parse(package).get_identity()",
        ),
        (
            "src/apm_cli/install/dry_run_plan.py",
            "selected_dependency_identity(package, all_apm_dependencies)",
            "DependencyReference.parse(package).get_identity()",
        ),
        (
            "src/apm_cli/install/phases/resolve.py",
            "selected_dependency_keys(ctx.only_packages, ctx.all_apm_deps)",
            "selected_dependency_keys(ctx.only_packages)",
        ),
        (
            "src/apm_cli/install/package_selection.py",
            "for dependency in dependencies:",
            "for dependency in ():",
        ),
        (
            "src/apm_cli/install/package_selection.py",
            "_selected_dependency_reference(spec, dependencies).get_unique_key()",
            "DependencyReference.parse(spec).get_unique_key()",
        ),
        (
            "src/apm_cli/install/package_selection.py",
            "return _selected_dependency_reference(package, dependencies).get_identity()",
            "return DependencyReference.parse(package).get_identity()",
        ),
    ],
)
def test_package_selector_identity_cannot_bypass_interpreted_references(
    path: str, old: str, new: str
) -> None:
    """Install and preview cannot revert to lossy shorthand reparsing."""
    rule_id = "install-deployment-prospective-dry-run-plan"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/dry_run_plan.py",
            "read_only=True,",
            "read_only=False,",
        ),
        (
            "src/apm_cli/install/dry_run_plan.py",
            "graph.flattened_dependencies.get_installation_list()",
            "apm_package.get_apm_dependencies()",
        ),
        (
            "src/apm_cli/install/phases/resolve.py",
            "ctx.deps_to_install = selected_subtree_dependencies(",
            "ctx.deps_to_install = alternate_subtree_dependencies(",
        ),
        (
            "src/apm_cli/install/dry_run_plan.py",
            "selected_subtree_dependencies(",
            "alternate_subtree_dependencies(",
        ),
        (
            "src/apm_cli/install/presentation/dry_run.py",
            "and not plan.dependency_preview_errors:",
            ":",
        ),
    ],
)
def test_preview_cannot_bypass_offline_graph_authority(path: str, old: str, new: str) -> None:
    """An alternate root-only preview or incomplete-removal claim fails the guard."""
    rule_id = "install-deployment-prospective-dry-run-plan"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_marketplace_source_parsing_guard_covers_packed_sources() -> None:
    """Packed marketplace sources stay on DependencyReference, not just entry coordinates.

    The matrix case for this guard mutates the entry-coordinate half in
    ref_resolver.py; this proves the packed-source half in resolver.py has teeth.
    """
    rule_id = "marketplace-integrations-source-parsing"
    path = "src/apm_cli/marketplace/resolver.py"
    old = "dependency = DependencyReference.parse_from_dict(entry)"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, "dependency = DependencyReference.parse_from_mapping(entry)", 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert {violation.rule_id for violation in report.violations} == {rule_id}
    assert {violation.path for violation in report.violations} == {path}


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/template.py",
            "            skill_pairing=selection.skill_pairing,\n",
            "",
            id="deploy-drops-pairing",
        ),
        pytest.param(
            "src/apm_cli/install/services.py",
            "        skill_pairing=skill_pairing,\n",
            "",
            id="skill-integrator-drops-pairing",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            "            subset_pairings=_replay_pairing(\n",
            "            _ignored=_replay_pairing(\n",
            id="lock-replay-drops-pairing",
        ),
        pytest.param(
            "src/apm_cli/install/phases/integrate.py",
            "InstallService.enforce_frozen_selections(",
            "InstallService.describe_frozen_selections(",
            id="frozen-skips-selection-check",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "def _pairing_summary(",
            "def merge_edge_pairings(edges):\n    return edges\n\n\ndef _pairing_summary(",
            id="second-pairing-owner",
        ),
    ],
)
def test_skill_pairing_cannot_bypass_selection_owner(path: str, old: str, new: str) -> None:
    """req-rs-018: every deploy, replay and frozen path keeps the exact pairing (#3763)."""
    rule_id = "install-deployment-selection-merge"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/policy/_constraint_pinning.py",
            "name = git_semver_package_name(dep)",
            'name = (dep.virtual_path or dep.repo_url).rstrip("/").rsplit("/", 1)[-1]',
        ),
        (
            "src/apm_cli/install/helpers/ref_reuse.py",
            "def git_semver_package_name(",
            "def git_semver_package_label(",
        ),
    ],
)
def test_pinned_constraint_tag_name_must_use_resolver_owner(path: str, old: str, new: str) -> None:
    """The policy recognises release tags with the resolver's package-name owner (#3224)."""
    rule_id = "transport-platform-git-semver-preflight"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/integration/hook_ownership.py",
            "def canonicalize_hook_sidecar(",
            "def canonicalize_hook_sidecar_disabled(",
        ),
        (
            "src/apm_cli/install/drift.py",
            "s_bytes = canonicalize_hook_sidecar(s_bytes)",
            "s_bytes = s_bytes",
        ),
        (
            "src/apm_cli/install/drift.py",
            "p_bytes = canonicalize_hook_sidecar(p_bytes)",
            "p_bytes = p_bytes",
        ),
        (
            "src/apm_cli/install/drift.py",
            "def diff_scratch_against_project(",
            "def canonicalize_hook_sidecar(data):\n    return data\n\n\ndef diff_scratch_against_project(",
        ),
    ],
)
def test_hook_sidecar_comparison_must_use_canonical_owner(path: str, old: str, new: str) -> None:
    """Both drift inputs must use the single sidecar comparison authority."""
    rule_id = "mutation_writes.neutral_hook_contract"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/integration/hook_ownership.py",
            "def local_bundle_hook_source_marker(",
            "def local_bundle_hook_source_marker_disabled(",
        ),
        (
            "src/apm_cli/integration/hook_integrator.py",
            "return local_bundle_hook_source_marker(bundle_id, package_info.local_bundle_approval)",
            'return f"local-bundle::{bundle_id}"',
        ),
        (
            "src/apm_cli/install/local_bundle_hooks.py",
            "outcome = integrator.integrate_hooks_for_target(",
            "outcome = integrator.integrate_package_hooks_claude(",
        ),
    ],
)
def test_local_bundle_hooks_must_use_canonical_owner(path: str, old: str, new: str) -> None:
    """Local-bundle hooks keep the owner's marker and register via HookIntegrator (#3700)."""
    rule_id = "mutation_writes.neutral_hook_contract"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/drift.py",
            "replay_allow = gated_allow_map.for_dependency(lock_dep, package_info)",
            "replay_allow = None",
        ),
        (
            "src/apm_cli/install/exec_gate.py",
            "self._trust_ctx_value = exec_trust_context_for_project(",
            "self._trust_ctx_value = build_exec_trust_context(",
        ),
        (
            "src/apm_cli/install/drift.py",
            "        project_root, config.policy_provider, user_scope=config.user_scope\n",
            "        project_root\n",
        ),
        (
            "src/apm_cli/install/audit_replay.py",
            "policy_provider=policy_provider,",
            "policy_provider=None,",
        ),
        (
            "src/apm_cli/policy/ci_checks.py",
            "policy_provider=policy_provider,",
            "policy_provider=None,",
        ),
        (
            "src/apm_cli/commands/audit.py",
            "policy_provider=replay_policy",
            "policy_provider=None",
        ),
        (
            "src/apm_cli/install/services.py",
            "reconcile_package_targets(package_info, project_root, targets),",
            "{},",
        ),
        (
            "src/apm_cli/install/exec_gate.py",
            "denied |= denied_exec_types(ctx.exec_trust_ctx, candidate_keys, unapproved)",
            "denied |= frozenset(unapproved)",
        ),
        (
            "src/apm_cli/install/exec_gate.py",
            "denied = frozenset(t for t, status in pinned_status.items() if status == TRUST_DENIED)",
            "denied = frozenset()",
        ),
    ],
)
def test_gated_hooks_retire_and_replay_through_trust_owner(path: str, old: str, new: str) -> None:
    """Gated hooks retire at install and stay withheld in audit replay (#3924)."""
    rule_id = "install-deployment-executable-trust-context"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) >= 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/template.py",
            'project_root, user_scope=is_user_scope(getattr(ctx, "scope", None))',
            "project_root, user_scope=False",
        ),
        (
            "src/apm_cli/security/executables.py",
            "    project_data: dict[str, Any] | None = None\n    if manifest_path.is_file():",
            "    project_data: dict[str, Any] | None = None\n"
            '    manifest_path = manifest_path.parent / "apm.yml"\n'
            "    if manifest_path.is_file():",
        ),
        (
            "src/apm_cli/commands/approve.py",
            "        lambda d: pkg == d.package_name,",
            "        lambda d: pkg == d.package_name or d.package_name.startswith(pkg),",
        ),
        (
            "src/apm_cli/install/local_bundle_handler.py",
            "def _filter_bundle_executables(",
            "def _bypass_trust(root):\n"
            "    return exec_trust_context_for_project(root, policy=None)\n\n\n"
            "def _filter_bundle_executables(",
        ),
    ],
)
def test_trust_owner_reads_the_scope_manifest_closed_world(path: str, old: str, new: str) -> None:
    """Trust reads the scope's manifest; an unregistered caller fails closed (#3954)."""
    rule_id = "install-deployment-executable-trust-context"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("rule_id", "path", "old", "new"),
    [
        (
            "install-deployment-trust-policy-outcome",
            "src/apm_cli/install/exec_gate.py",
            "            policy = resolve_trust_policy(\n"
            "                manifest_root(self._project_root, user_scope=self._user_scope), "
            "announce=False\n"
            "            )\n",
            "            policy = None\n",
        ),
        (
            "install-deployment-trust-policy-outcome",
            "src/apm_cli/install/local_bundle_handler.py",
            "        announce=False,\n        raise_blocking_errors=not dry_run,\n",
            "        announce=False,\n        raise_blocking_errors=not dry_run,\n"
            "        cache_only=True,\n",
        ),
        (
            "install-deployment-trust-policy-outcome",
            "src/apm_cli/commands/uninstall/engine.py",
            "    preflight_survivor_hooks(target_survivor_plan, _allow_executables)\n",
            "    from ...policy.discovery import discover_policy_with_chain\n\n"
            "    discover_policy_with_chain(project_root)\n"
            "    preflight_survivor_hooks(target_survivor_plan, _allow_executables)\n",
        ),
        (
            "install-deployment-local-bundle-hook-trust",
            "src/apm_cli/install/local_bundle_hook_trust.py",
            "            _retire_per_file(project_root, targets, item)\n",
            "",
        ),
        (
            "install-deployment-local-bundle-hook-trust",
            "src/apm_cli/commands/install.py",
            "    reconcile_scope_bundle_hooks(\n",
            "    (lambda *a, **k: None)(\n",
        ),
        (
            "install-deployment-local-bundle-hook-trust",
            "src/apm_cli/install/local_bundle_hook_trust.py",
            "        elif not all(package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS) for key in keys):\n",
            "        elif False:\n",
        ),
        (
            "install-deployment-local-bundle-hook-trust",
            "src/apm_cli/install/local_bundle_hooks.py",
            "def _retire_bundle_hooks(",
            'def _marker(slug):\n    return "local-bundle::" + slug\n\n\ndef _retire_bundle_hooks(',
        ),
        (
            "install-deployment-bundle-user-scope-root",
            "src/apm_cli/commands/install.py",
            "                    user_scope=global_,\n",
            "                    user_scope=False,\n",
        ),
        (
            "install-deployment-update-content-integrity",
            "src/apm_cli/install/sources.py",
            "                not update_accepts_new_content(ctx, dep_locked_chk, resolved_commit)\n",
            "                not ctx.update_refs\n",
        ),
        (
            "install-deployment-update-content-integrity",
            "src/apm_cli/install/phases/integrate.py",
            "        and not _locked_commit_unchanged\n",
            "",
        ),
        (
            "install-deployment-update-content-integrity",
            "src/apm_cli/install/phases/integrate.py",
            "        and locked_commit_unchanged(_dep_locked_chk, _callback_sha)\n",
            "        and _dep_locked_chk.resolved_commit == _callback_sha\n",
        ),
        (
            "install-deployment-update-content-integrity",
            "src/apm_cli/install/source_helpers.py",
            "    return bool(ctx.update_refs and not locked_commit_unchanged(locked_dep, "
            "resolved_commit))\n",
            "    return bool(ctx.update_refs)\n",
        ),
        (
            "install-deployment-update-content-integrity",
            "src/apm_cli/install/source_helpers.py",
            "        update_accepts_new_content(ctx, locked_dep, resolved_commit)\n        or dep_key",
            "        ctx.update_refs\n        or dep_key",
        ),
        (
            "install-deployment-trust-policy-outcome",
            "src/apm_cli/policy/outcome_routing.py",
            "        fetch_result = discover_trust_policy(root)\n",
            "        fetch_result = discover_policy_with_chain(root)\n",
        ),
        (
            "install-deployment-trust-policy-outcome",
            "src/apm_cli/commands/audit.py",
            "        return resolve_trust_policy(cfg.project_root, announce=False, "
            "fetch_result=fetch_policy())\n",
            '        return getattr(fetch_policy(), "policy", None)\n',
        ),
    ],
)
def test_trust_and_integrity_owners_reject_bypasses(
    rule_id: str, path: str, old: str, new: str
) -> None:
    """Each trust and integrity consistency guard rejects its bypass (#4027)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert {violation.rule_id for violation in report.violations} == {rule_id}


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/dry_run_plan.py",
            "filter_lsp_by_allow_executables(list(self.lsp_dependencies), allow_map, ctx.logger)",
            "filter_lsp_by_allow_executables(\n"
            "                list(self.lsp_dependencies),\n"
            '                getattr(apm_package, "allow_executables", None),\n'
            "                ctx.logger,\n"
            "            )",
            id="manifest-grants-bypass-owner",
        ),
        pytest.param(
            "src/apm_cli/install/template.py",
            "            allow_executables=authorized_exec_map(AUTHORIZED_FIRST_PARTY),\n",
            "",
            id="omitted-first-party-map",
        ),
        pytest.param(
            "src/apm_cli/install/drift.py",
            "                            allow_executables=replay_allow,\n",
            "                            allow_executables=None,\n",
            id="literal-none-map",
        ),
        pytest.param(
            "src/apm_cli/install/lsp/integration.py",
            "    allow_map = closed_exec_map(None)\n",
            "    allow_map = None\n",
            id="none-placeholder-map",
        ),
    ],
)
def test_deploy_path_allow_map_comes_from_the_trust_owner(path: str, old: str, new: str) -> None:
    """Closed-world provenance: every allow-map consumer gets an owner map (#4018)."""
    rule_id = "install-deployment-executable-trust-context"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/deployment_plan.py",
            "def prepare_package_deployment(",
            "def _hooks_ok(allow_executables, key):\n"
            "    from apm_cli.security.executables import is_package_approved\n\n"
            '    return is_package_approved(allow_executables, key, "hooks")\n\n\n'
            "def prepare_package_deployment(",
            id="out-of-owner-approval-lookup",
        ),
        pytest.param(
            "src/apm_cli/install/services.py",
            "                hooks_approved=_hooks_approved_bundle,\n"
            "                canvas_approved=_canvas_enabled and _canvas_approved_bundle,\n"
            "            )\n"
            "            if _deploy_rel is None:",
            "                canvas_approved=_canvas_enabled and _canvas_approved_bundle,\n"
            "            )\n"
            "            if _deploy_rel is None:",
            id="omitted-hooks-approved",
        ),
        pytest.param(
            "src/apm_cli/install/local_bundle_paths.py",
            "    hooks_approved: bool,\n    canvas_approved: bool,\n) -> str | None:\n"
            '    """Return a bundle path relative',
            "    hooks_approved: bool = True,\n    canvas_approved: bool,\n) -> str | None:\n"
            '    """Return a bundle path relative',
            id="fail-open-hooks-approved-default",
        ),
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "    non_interactive: bool,\n",
            "    non_interactive: bool = False,\n",
            id="fail-open-trust-bin-default",
        ),
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "    if package_name == FIRST_PARTY_PACKAGE or exec_map_authorizes_all(allow_executables):\n"
            "        return True, True, True, True, True\n",
            "    if allow_executables is None or exec_map_authorizes_all(allow_executables):\n"
            "        return True, True, True, True, True\n",
            id="missing-map-authorizes",
        ),
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "    if native_install(package_info):\n",
            "    if False:\n",
            id="native-admission-skipped",
        ),
        pytest.param(
            "src/apm_cli/install/deployment_plan.py",
            "        hook_file_admitted=hook_filter,\n",
            "",
            id="pinned-hook-admission-omitted",
        ),
    ],
)
def test_executable_approval_cannot_leave_the_trust_owners(path: str, old: str, new: str) -> None:
    """Closed-world: no adapter looks approvals up or fails open on omission (#4018)."""
    rule_id = "install-deployment-executable-item-admission"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/commands/deps/cli.py",
            "InstallService.catalog_freshness(update_refs=True, refresh=False)",
            "nullcontext()",
        ),
        (
            "src/apm_cli/commands/lock.py",
            "InstallService.catalog_freshness(update_refs=update_refs, refresh=False)",
            "nullcontext()",
        ),
        (
            "src/apm_cli/commands/prune.py",
            "def prune(",
            "def _refresh_everything(apm_package):\n"
            "    return _install_apm_dependencies(apm_package, update_refs=True)\n\n\n"
            "def prune(",
        ),
    ],
)
def test_update_intent_runs_inside_the_catalog_freshness_envelope(
    path: str, old: str, new: str
) -> None:
    """Every install entry caller revalidates marketplaces through one envelope (#3961)."""
    rule_id = "marketplace-integrations-catalog-freshness"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/core/plugin_manifest.py",
            "project_root: Path, *, logger: Any = None, exclude_apm_managed: bool = True",
            "project_root: Path, *, logger: Any = None, exclude_apm_managed: bool = False",
        ),
        (
            "src/apm_cli/bundle/plugin_exporter.py",
            "    root_mcp = _collect_mcp(project_root)",
            "    root_mcp = _collect_mcp(project_root, exclude_apm_managed=False)",
        ),
    ],
)
def test_pack_never_ships_apm_rendered_mcp_servers(path: str, old: str, new: str) -> None:
    """Root .mcp.json reads exclude APM-managed servers; opt-outs are registered (#3966)."""
    rule_id = "marketplace-integrations-pack-plugin-manifests"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_bundle_reinstall_reconciles_owned_mcp_servers() -> None:
    """Dropping the empty-list reconcile lets denied bundle MCP servers linger (#3956)."""
    rule_id = "install-deployment-mcp-ownership-migration"
    path = "src/apm_cli/install/local_bundle_handler.py"
    old = "bool(bundle_mcp_deps) or bundle_info.format == BundleFormat.CLAUDE_PLUGIN.value"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, "bool(bundle_mcp_deps)")
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/orphans.py",
            "dep = locked_marketplace_form(dep, lockfile, marketplace_index)",
            "dep = dep",
        ),
        (
            "src/apm_cli/install/dry_run_plan.py",
            "locked = locked_marketplace_row(dependency, lockfile, marketplace_index)",
            "locked = None",
        ),
    ],
)
def test_object_form_declarations_map_to_their_locked_row(path: str, old: str, new: str) -> None:
    """Prune and dry-run read an object-form declaration through its lock row (#3960)."""
    rule_id = "marketplace-integrations-locked-evidence"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_raw_manifest_target_readers_pass_the_manifest_path() -> None:
    """Plugin-only pack warns about deprecated target keys like every reader (#3965)."""
    rule_id = "registry_delegation.target_vocabulary"
    path = "src/apm_cli/core/build_orchestrator.py"
    old = "targets = manifest_target_restriction(data, apm_yml_path)"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, "targets = manifest_target_restriction(data)")
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/audit_replay.py",
            "        if locked_copy_is_stale(dep, path):",
            "        if False:",
        ),
        (
            "src/apm_cli/install/drift.py",
            "        if not locked_copy_holds_commit(lock_dep, candidate):",
            "        if not verify_marker(candidate, lock_dep.resolved_commit):",
        ),
        (
            "src/apm_cli/install/orphans.py",
            "def _build_expected_install_paths(",
            "def _second_proof(dep, path):\n"
            "    return _should_skip_redownload(dep, path)\n\n\n"
            "def _build_expected_install_paths(",
        ),
    ],
)
def test_audit_and_install_share_one_copy_validity_owner(path: str, old: str, new: str) -> None:
    """Warm audit and replay prove copies through drift.locked_copy_proof only (#3964)."""
    rule_id = "install-deployment-locked-copy-validity"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_failed_direct_dependency_stops_before_integration() -> None:
    """Dropping the apply barrier lets a failed install deploy untracked files (#3874)."""
    rule_id = "install-deployment-outcome"
    path = "src/apm_cli/install/pipeline.py"
    old = "        if direct_dependency_failed(ctx):"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, "        if False:")
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/security/trust_fingerprint.py",
            "return compute_native_plugin_pin(plugin_root)",
            "return compute_native_plugin_hash(plugin_root)",
        ),
        (
            "src/apm_cli/security/trust.py",
            "def admit_native_plugin(",
            "def _second_pin(root):\n"
            "    return compute_native_plugin_pin(root)\n\n\n"
            "def admit_native_plugin(",
        ),
    ],
)
def test_native_plugin_pin_is_portable_and_owned(path: str, old: str, new: str) -> None:
    """Approval pins leave the exec bit out and come from one owner."""
    rule_id = "install-deployment-trust-fingerprint"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


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
        guard_id="audit-finding-serialization",
        rule_id="audit-primitive-discovery",
        path="src/apm_cli/security/audit_report.py",
        old="items = [finding_to_json(finding) for finding in all_findings]",
        new="items = [bypass_serializer(finding) for finding in all_findings]",
        intent="Ordinary JSON reporting bypasses the canonical single-finding serializer.",
    ),
    MutationCase(
        guard_id="audit-primitive-discovery",
        rule_id="audit-primitive-discovery",
        path="src/apm_cli/security/file_scanner.py",
        old="for surface in primitive_surfaces(project_root, scoped, user_scope=user_scope):",
        new="for surface in ():",
        intent="Automatic audit discovery stops enumerating canonical primitive surfaces.",
    ),
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
        guard_id="contracts-tooling-governance-evidence",
        rule_id="contracts-tooling-governance-evidence",
        path="scripts/governance/authority.cjs",
        old="authorizes_implementation: false",
        new="authorizes_implementation: true",
        intent="Advisory evidence starts claiming implementation authority.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lifecycle-policy-errors",
        rule_id="contracts-tooling-lifecycle-policy-errors",
        path="src/apm_cli/core/lifecycle_scripts.py",
        old=(
            '        _logger.debug("Lifecycle org-policy check failed", exc_info=True)\n'
            '        suppressed_reason = "org policy could not be enforced"\n'
        ),
        new='        _logger.debug("Lifecycle org-policy check failed", exc_info=True)\n',
        intent="An org-policy read error lets lifecycle scripts fire with the org ceiling unread.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lifecycle-script-gate",
        rule_id="contracts-tooling-lifecycle-script-gate",
        path="src/apm_cli/commands/lifecycle.py",
        old="    runner = build_runner_from_context(verbose=verbose, project_root=project_root, events=(event,))",
        new="    runner = LifecycleScriptRunner(scripts=matching, verbose=verbose, project_root=project_root)",
        intent="lifecycle test --execute fires scripts the script gate would withhold.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-generator-gate",
        rule_id="contracts-tooling-lockfile-generator-gate",
        path="src/apm_cli/commands/pack.py",
        old='check_current_cli(project_root, command="apm pack")',
        new="pass",
        intent="apm pack stops refusing a lockfile written by a newer APM.",
    ),
    MutationCase(
        guard_id="contracts-tooling-lockfile-generator-stamp",
        rule_id="contracts-tooling-lockfile-generator-stamp",
        path="src/apm_cli/deps/lockfile.py",
        old="            self.apm_version = installed_apm_version()\n",
        new="            pass\n",
        intent="Lockfile writes stop recording the running APM, so older CLIs accept them.",
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
        guard_id="contracts-tooling-lockfile-symlink-write-guard",
        rule_id="contracts-tooling-lockfile-symlink-write-guard",
        path="src/apm_cli/deps/lockfile.py",
        old="def ensure_lockfile_writable(",
        new="def _ensure_lockfile_writable(",
        intent="Lockfile symlink write guard loses its canonical public definition.",
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
        guard_id="contracts-tooling-native-lifecycle-evidence",
        rule_id="contracts-tooling-native-lifecycle-evidence",
        path="scripts/check_lifecycle_evidence.py",
        old="plugins=[plugin]",
        new="plugins=[]",
        intent="Native lifecycle execution disconnects pytest from the fresh subprocess observer.",
    ),
    MutationCase(
        guard_id="contracts-tooling-policy-content-hash",
        rule_id="contracts-tooling-policy-content-hash",
        path="src/apm_cli/policy/discovery.py",
        old="actual_hex = compute_policy_hash(raw_bytes, algo)",
        new="actual_hex = _local_policy_hash(raw_bytes, algo)",
        intent="Policy verification bypasses the canonical SHA-2 digest owner.",
    ),
    MutationCase(
        guard_id="contracts-tooling-policy-identity",
        rule_id="contracts-tooling-policy-identity",
        path="src/apm_cli/policy/matcher.py",
        old=(
            "                name, case_insensitive_prefix_segments=prefix\n"
            "            )\n"
            "            index._names"
        ),
        new=(
            "                name, case_insensitive_prefix_segments=0\n"
            "            )\n"
            "            index._names"
        ),
        intent="Policy index construction ignores the source-owned casing prefix.",
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
        guard_id="contracts-tooling-python-artifact-membership",
        rule_id="contracts-tooling-python-artifact-membership",
        path="src/apm_cli/install/deployed_paths.py",
        old="is_generated_python_artifact(relative)",
        new="False",
        intent="Skill lockfile inventory stops excluding generated Python artifacts.",
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
        guard_id="contracts-tooling-source-replacements",
        rule_id="contracts-tooling-source-replacements",
        path="src/apm_cli/models/dependency/replacement.py",
        old="return (self.host, self.port, self.repo_url, self.subpath)",
        new="return ('github.com', None, self.repo_url, self.subpath)",
        intent="Root replacement matching becomes host- and endpoint-blind.",
    ),
    MutationCase(
        guard_id="contracts-tooling-strip-charset",
        rule_id="contracts-tooling-strip-charset",
        path="src/apm_cli/models/dependency/reference.py",
        old='part.removesuffix(".git")',
        new='part.rstrip(".git")',
        intent="Repository segment validation strips a .git character set again (#3715).",
    ),
    MutationCase(
        guard_id="hooks-integrations-claude-mcp-paths",
        rule_id="mutation_writes.claude_mcp_paths",
        path="src/apm_cli/integration/mcp_integrator.py",
        old="claude_cfg = ClaudeClientAdapter.resolve_user_claude_json_path(unresolved_cfg)",
        new='claude_cfg = Path.home() / ".claude.json"',
        intent="Claude user cleanup bypasses the adapter-selected profile path.",
    ),
    MutationCase(
        guard_id="hooks-integrations-copilot-cli-mcp-paths",
        rule_id="mutation_writes.copilot_cli_mcp_paths",
        path="src/apm_cli/adapters/client/copilot.py",
        old='resolve_user_root("copilot") / "mcp-config.json"',
        new='Path.home() / ".copilot" / "mcp-config.json"',
        intent="Copilot adapter bypasses the owner-selected COPILOT_HOME user root.",
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
        guard_id="hooks-integrations-mcp-config-key-derivation",
        rule_id="mutation_writes.mcp_config_key_derivation",
        path="src/apm_cli/adapters/client/base.py",
        old="def determine_config_key(",
        new="def _derive_config_key(",
        intent="MCPClientAdapter loses the canonical config-key derivation definition.",
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
        guard_id="hooks-integrations-mcp-json-config-write",
        rule_id="mutation_writes.mcp_json_config_write",
        path="src/apm_cli/adapters/client/vscode.py",
        old="self._write_json_config(Path(config_path), new_config, shared=True)",
        new='open(config_path, "w").write(json.dumps(new_config))',
        intent="An adapter serializes MCP JSON straight to a file instead of the owner writer.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-omission-evidence",
        rule_id="mutation_writes.mcp_omission_evidence",
        path="src/apm_cli/integration/mcp_config_view.py",
        old='"dependency": dependency.get_unique_key(),',
        new='"dependency": dependency.name,',
        intent="Omission evidence binds only a display name instead of canonical dependency identity.",
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
        old='        "environment",\n',
        new="",
        intent="Shared MCP model stops denying the OpenCode environment alias.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-remote-shape-table",
        rule_id="mutation_writes.mcp_remote_shape_table",
        path="src/apm_cli/adapters/client/antigravity.py",
        old='    remote_shape = remote_shape_row("antigravity")\n',
        new="",
        intent="Antigravity silently inherits Gemini's remote shape row (httpUrl).",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-remote-transport-normalization",
        rule_id="mutation_writes.mcp_remote_transport_normalization",
        path="src/apm_cli/adapters/client/kiro.py",
        old='remote, runtime_label="Kiro", server_name=server_name',
        new='{"transport": remote.get("transport_type")}, runtime_label="Kiro"',
        intent="Kiro re-derives the remote transport from the raw legacy key.",
    ),
    MutationCase(
        guard_id="hooks-integrations-mcp-target-selection",
        rule_id="mutation_writes.mcp_target_selection",
        path="src/apm_cli/integration/mcp_integrator_install.py",
        old="manifest_target_restriction(apm_config)",
        new="manifest_target_restrictions(apm_config)",
        intent="MCP install adapter stops parsing targets through the manifest owner.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/integration/instruction_integrator.py",
        old="plan = self._prepare_rule_plan([source], rules_dir, mapping.extension, mapping.format_id)",
        new="plan = self._local_rule_plan([source], rules_dir, mapping.extension, mapping.format_id)",
        intent="Native coverage stops sharing the install filename and content projection.",
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
        guard_id="hooks-integrations-opencode-enabled-intent",
        rule_id="mutation_writes.opencode_enabled_intent",
        path="src/apm_cli/models/dependency/mcp.py",
        old="return self.enabled is not _ENABLED_UNSET",
        new="return bool(self.enabled)",
        intent="Explicit false and null become omission instead of manifest intent.",
    ),
    MutationCase(
        guard_id="hooks-integrations-plugin-root-render",
        rule_id="mutation_writes.plugin_root_render",
        path="src/apm_cli/adapters/client/copilot.py",
        old="raw = self._self_defined_stdio(server_info)",
        new='raw = server_info.get("_raw_stdio")',
        intent="An adapter reads raw stdio directly, writing ${CLAUDE_PLUGIN_ROOT} unrendered.",
    ),
    MutationCase(
        guard_id="hooks-integrations-user-hook-home-reference",
        rule_id="mutation_writes.user_hook_home_reference",
        path="src/apm_cli/integration/hook_integrator.py",
        old="            if home_reference is None:\n",
        new='            if home_reference is None or os.name == "nt":\n',
        intent="The hook rewriter gates home anchoring on the host OS, not the capability.",
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
        guard_id="install-deployment-agent-render-then-adopt",
        rule_id="install-deployment-agent-render-then-adopt",
        path="src/apm_cli/integration/agent_integrator.py",
        old="expected_content=rendered,",
        new="expected_content=None,",
        intent="Agent adoption compares the raw source instead of the rendered text (#3722).",
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
        guard_id="install-deployment-bundle-admission-owner",
        rule_id="install-deployment-bundle-admission-owner",
        path="src/apm_cli/commands/pack.py",
        old="            admit=apm_bundle_admission(Path(output), logger, dry_run=dry_run),\n",
        new="            admit=lambda candidate: None,\n",
        intent="apm unpack deploys a bundle without the shared bundle admission owner.",
    ),
    MutationCase(
        guard_id="install-deployment-bundle-compile-staging",
        rule_id="install-deployment-bundle-compile-staging",
        path="src/apm_cli/install/local_bundle_handler.py",
        old='staged_instructions = result.get("staged_files", [])',
        new=('staged_instructions = [f for f in deployed if "/.apm/instructions/" in f]'),
        intent="Bundle install re-derives staged instructions from deployed claim paths.",
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
        guard_id="install-deployment-bundle-user-scope-root",
        rule_id="install-deployment-bundle-user-scope-root",
        path="src/apm_cli/install/local_bundle_handler.py",
        old="    return get_apm_dir(InstallScope.USER if user_scope else InstallScope.PROJECT)\n",
        new="    return Path.home() if user_scope else Path.cwd()\n",
        intent="A -g local bundle keeps its lock under the home directory instead of APM_HOME.",
    ),
    MutationCase(
        guard_id="install-deployment-cleanup-ownership-release",
        rule_id="install-deployment-cleanup-ownership-release",
        path="src/apm_cli/integration/cleanup.py",
        old="release = user_content_only and not failed_path_retained",
        new="release = user_content_only",
        intent="A skill directory left only for user content pins its removed owner again.",
    ),
    MutationCase(
        guard_id="install-deployment-codex-agent-link-resolution",
        rule_id="install-deployment-codex-agent-link-resolution",
        path="src/apm_cli/integration/agent_integrator.py",
        old="resolve_links=self.resolve_links,",
        new="resolve_links=None,",
        intent="Codex agent conversion bypasses the shared link resolver again.",
    ),
    MutationCase(
        guard_id="install-deployment-consumer-manifest-rewrite",
        rule_id="install-deployment-consumer-manifest-rewrite",
        path="src/apm_cli/install/mcp/writer.py",
        old="dump_yaml_roundtrip(data, apm_yml_path)",
        new="dump_yaml(data, apm_yml_path)",
        intent="apm install --mcp strips user comments from apm.yml again.",
    ),
    MutationCase(
        guard_id="install-deployment-drift-claim-classification",
        rule_id="install-deployment-drift-claim-classification",
        path="src/apm_cli/install/drift.py",
        old="unmanaged = unmanaged_skill or claims.unclaimed(rel)",
        new="unmanaged = unmanaged_skill",
        intent="Audit reports an unclaimed differing file as a managed local edit.",
    ),
    MutationCase(
        guard_id="install-deployment-effective-selection",
        rule_id="install-deployment-effective-selection",
        path="src/apm_cli/commands/uninstall/engine.py",
        old="                    skill_subset=selection.skill_subset,\n",
        new="",
        intent="Uninstall re-integrates survivors without their effective skill selection.",
    ),
    MutationCase(
        guard_id="install-deployment-executable-item-admission",
        rule_id="install-deployment-executable-item-admission",
        path="src/apm_cli/security/executables.py",
        old="return entry.get(exec_type, False) is True",
        new="return bool(entry.get(exec_type, False))",
        intent="A pinned item map reads as a type-level grant for any content.",
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
        guard_id="install-deployment-frozen-flag-reader",
        rule_id="install-deployment-frozen-flag-reader",
        path="src/apm_cli/install/phases/cleanup.py",
        old="if frozen_disposition.retain_package_orphans:",
        new="if ctx.frozen:",
        intent="Orphan cleanup reads the frozen flag instead of the owner's disposition.",
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
        guard_id="install-deployment-hook-pin-frozen-audit-parity",
        rule_id="install-deployment-hook-pin-frozen-audit-parity",
        path="src/apm_cli/install/service.py",
        old="mismatches = locked_hook_pin_mismatches(lockfile, modules_dir, trust_ctx)",
        new="mismatches = []",
        intent="install --frozen stops verifying locked hook pins while audit --ci still does.",
    ),
    MutationCase(
        guard_id="install-deployment-immutable-requirements",
        rule_id="install-deployment-immutable-requirements",
        path="src/apm_cli/deps/apm_resolver.py",
        old="requirements.add(node)",
        new="requirements.add_unchecked(node)",
        intent="Resolver skips canonical immutable admission before selecting a winner.",
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
        guard_id="install-deployment-local-bundle-hook-trust",
        rule_id="install-deployment-local-bundle-hook-trust",
        path="src/apm_cli/install/local_bundle_hook_trust.py",
        old="        if None in keys:\n",
        new="        if not keys:\n",
        intent="A local-bundle hook with no recorded approval key is admitted instead of retired.",
    ),
    MutationCase(
        guard_id="install-deployment-lock-parent-placement",
        rule_id="install-deployment-lock-parent-placement",
        path="src/apm_cli/deps/why_walker.py",
        old="    legacy = lockfile.replacements is None\n",
        new=("    legacy = lockfile.replacements is None\n    parent_depth = target.depth - 1\n"),
        intent="deps why re-derives legacy lockfile parents by depth outside the owner.",
    ),
    MutationCase(
        guard_id="install-deployment-locked-content-verdict",
        rule_id="install-deployment-locked-content-verdict",
        path="src/apm_cli/install/drift.py",
        old=(
            "    if locked_content_mismatch(\n"
            "        key, lock_dep, candidate, lockfile=lockfile, package_info=package_info\n"
            "    ):\n"
        ),
        new="    if False:\n",
        intent="The audit --ci scratch replay stops asking install's locked content verdict.",
    ),
    MutationCase(
        guard_id="install-deployment-locked-copy-validity",
        rule_id="install-deployment-locked-copy-validity",
        path="src/apm_cli/install/phases/integrate.py",
        old="else locked_copy_proof(locked_dep, install_path)",
        new="else _should_skip_redownload(locked_dep, install_path) or None",
        intent="Integrate re-derives apm_modules copy validity outside drift.locked_copy_proof.",
    ),
    MutationCase(
        guard_id="install-deployment-lsp-capability-gate",
        rule_id="install-deployment-lsp-capability-gate",
        path="src/apm_cli/install/phases/integrate.py",
        old="    preflight_lsp_targets(ctx)\n",
        new="",
        intent="The integrate phase deploys packages without the LSP capability gate.",
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
        old=(
            "return BaseIntegrator.resolve_deploy_path(\n"
            "            relative_path,\n"
            "            root,\n"
            "            allowed_prefixes=(relative_path,),\n"
            "        )"
        ),
        new=(
            "return spec.path(\n"
            "            project_root,\n"
            "            user_scope=user_scope,\n"
            "        )"
        ),
        intent="LSP target config writes bypass the canonical deployment-path gate.",
    ),
    MutationCase(
        guard_id="install-deployment-manifest-snapshot",
        rule_id="install-deployment-manifest-snapshot",
        path="src/apm_cli/install/source_helpers.py",
        old="snapshot = APMPackage.from_mapping(",
        new="snapshot = APMPackage.from_apm_yml(",
        intent="Acquired metadata reuses the process-lifetime manifest path cache.",
    ),
    MutationCase(
        guard_id="install-deployment-mcp-admission",
        rule_id="install-deployment-mcp-admission",
        path="src/apm_cli/integration/mcp_config_view.py",
        old="verdict = admission.admit(subject)",
        new="verdict = McpAdmission.admit_all().admit(subject)",
        intent="The MCP view admits every dependency server whatever the command's consent.",
    ),
    MutationCase(
        guard_id="install-deployment-mcp-native-comparison",
        rule_id="install-deployment-mcp-native-comparison",
        path="src/apm_cli/integration/mcp_integrator_install.py",
        old="    if protected_clients:\n        # Native content must be checked",
        new='    if {"claude", "codex"}.intersection(target_runtimes):\n'
        "        # Native content must be checked",
        intent="Self-defined MCP comparison falls back to a per-runtime name gate.",
    ),
    MutationCase(
        guard_id="install-deployment-mcp-ownership-migration",
        rule_id="install-deployment-mcp-ownership-migration",
        path="src/apm_cli/install/mcp/ownership.py",
        old="def compare_native_mcp_connection(",
        new="def compare_native_mcp_connection_disabled(",
        intent="Native MCP connection ownership loses its canonical baseline comparator.",
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
        guard_id="install-deployment-orphan-selection",
        rule_id="install-deployment-orphan-selection",
        path="src/apm_cli/commands/prune.py",
        old="        orphaned_packages = list(decision.removed_paths)\n",
        new="        orphaned_packages = [k for k in lockfile.dependencies if k not in expected_lock_keys]\n",
        intent="Prune grows a second removal predicate beside the orphan owner.",
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
        guard_id="install-deployment-package-content-hash",
        rule_id="install-deployment-package-content-hash",
        path="src/apm_cli/utils/content_hash.py",
        old='+ b"\\0" + mode',
        new="+ mode",
        intent="The shared tree framing drops the NUL that terminates each path.",
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
        guard_id="install-deployment-policy-disable-switch-owner",
        rule_id="install-deployment-policy-disable-switch-owner",
        path="src/apm_cli/commands/audit.py",
        old="    if policy_disabled_by_env():\n        return False",
        new='    if os.environ.get("APM_POLICY_DISABLE"):\n        return False',
        intent="A command reads APM_POLICY_DISABLE directly instead of the single helper.",
    ),
    MutationCase(
        guard_id="install-deployment-policy-field-types",
        rule_id="install-deployment-policy-field-types",
        path="src/apm_cli/policy/parser.py",
        old="        if not isinstance(value, str) or value not in valid:\n",
        new="        if value not in valid:\n",
        intent="A list-valued enum field reaches the membership test and crashes the parser.",
    ),
    MutationCase(
        guard_id="install-deployment-policy-mcp-transport-projection",
        rule_id="install-deployment-policy-mcp-transport-projection",
        path="src/apm_cli/policy/policy_checks.py",
        old="_check_mcp_transport(_effective_mcp_transports(mcp_list, policy.mcp), policy.mcp)",
        new="_check_mcp_transport({m.name: frozenset({m.transport}) for m in mcp_list}, policy.mcp)",
        intent="Policy check 9 reads the raw declared transport instead of the effective one.",
    ),
    MutationCase(
        guard_id="install-deployment-policy-outcome-routing",
        rule_id="install-deployment-policy-outcome-routing",
        path="src/apm_cli/commands/audit.py",
        old="    elif route.blocks:\n",
        new='    elif route.blocks or fetch_result.outcome in ("malformed", "empty"):\n',
        intent="audit --ci classifies policy outcomes with its own list beside the routing owner.",
    ),
    MutationCase(
        guard_id="install-deployment-policy-remote-origin-owner",
        rule_id="install-deployment-policy-remote-origin-owner",
        path="src/apm_cli/policy/_remote.py",
        old='[get_git_executable(), "remote", "get-url", "origin"],',
        new=(
            '[get_git_executable(), "remote", "get-url", "origin"],\n'
            '            [get_git_executable(), "remote", "get-url", "origin"],'
        ),
        intent="A second git-remote origin read appears outside the single canonical reader.",
    ),
    MutationCase(
        guard_id="install-deployment-policy-violation-subjects",
        rule_id="install-deployment-policy-violation-subjects",
        path="src/apm_cli/install/phases/policy_target_check.py",
        old=(
            "            emit_check_violations(\n"
            "                ctx.logger,\n"
            "                check,\n"
            "                severity=severity,\n"
        ),
        new=(
            "            ctx.logger.policy_violation(\n"
            "                dep_ref=check.name,\n"
            "                reason=check.message,\n"
            "                severity=severity,\n"
        ),
        intent="An install policy phase reports the check name as the dependency (#3835).",
    ),
    MutationCase(
        guard_id="install-deployment-primitive-classification",
        rule_id="install-deployment-primitive-classification",
        path="src/apm_cli/install/primitive_classification.py",
        old="def classify_agent_source_file(",
        new="def classify_agent_source_file_disabled(",
        intent="Primitive classification loses its canonical agent-source classifier.",
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
        guard_id="install-deployment-selection-merge",
        rule_id="install-deployment-selection-merge",
        path="src/apm_cli/deps/apm_resolver.py",
        old="                        self._record_edge_selection(sub_dep, node)\n",
        new="                        self._note_edge(sub_dep, node)\n",
        intent="A transitive edge reaches queue dedup without being recorded for the merge.",
    ),
    MutationCase(
        guard_id="install-deployment-single-file-materialization",
        rule_id="install-deployment-single-file-materialization",
        path="src/apm_cli/install/phases/local_content.py",
        old=(
            "    source = read_local_package_source(dep_ref, base_dir)\n"
            "    install_path.parent.mkdir(parents=True, exist_ok=True)"
        ),
        new=(
            "    source = bypass_local_package_source(dep_ref, base_dir)\n"
            "    install_path.parent.mkdir(parents=True, exist_ok=True)"
        ),
        intent="Local copying bypasses the canonical single-file admission authority.",
    ),
    MutationCase(
        guard_id="install-deployment-skill-adoption-baseline",
        rule_id="install-deployment-skill-adoption-baseline",
        path="src/apm_cli/integration/skill_adoption.py",
        old="    if ctx.existing_lockfile is None or ctx.downloader is None:\n        return None",
        new="    if not ctx.update_refs or ctx.existing_lockfile is None:\n        return None",
        intent="Plain install after an apm.yml ref change loses the locked-source baseline.",
    ),
    MutationCase(
        guard_id="install-deployment-skill-layout",
        rule_id="install-deployment-skill-layout",
        path="src/apm_cli/commands/uninstall/engine.py",
        old="_base_targets = resolve_reintegration_targets(",
        new="_base_targets = resolve_targets(",
        intent="Uninstall rebuilds survivors without recovering the recorded skill layout.",
    ),
    MutationCase(
        guard_id="install-deployment-skill-preflight",
        rule_id="install-deployment-skill-preflight",
        path="src/apm_cli/install/phases/integrate.py",
        old="deployment_plans, local_plan = preflight_skill_materializations(ctx, materialized)",
        new="deployment_plans, local_plan = {}, None",
        intent="Integration bypasses whole-plan unmanaged skill preflight before native writes.",
    ),
    MutationCase(
        guard_id="install-deployment-skills-root-derivation",
        rule_id="install-deployment-skills-root-derivation",
        path="src/apm_cli/integration/skill_integrator.py",
        old="target_skills_root = project_root / target.skills_rel_root",
        new='target_skills_root = project_root / target.root_dir / "skills"',
        intent="Skill integration re-derives the skills root instead of using the target profile.",
    ),
    MutationCase(
        guard_id="install-deployment-source-plan",
        rule_id="install-deployment-source-plan",
        path="src/apm_cli/install/deployment_plan.py",
        old="source_plan = DeployableSourcePlan.create(",
        new="source_plan_x = DeployableSourcePlan.create(",
        intent="Deployment preparation stops binding the shared deployable source plan.",
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
        guard_id="install-deployment-trust-fingerprint",
        rule_id="install-deployment-trust-fingerprint",
        path="src/apm_cli/integration/mcp_config_view.py",
        old="from collections.abc import Callable, Mapping\n",
        new="import hashlib\nfrom collections.abc import Callable, Mapping\n",
        intent="The MCP view grows a second trust hasher beside the fingerprint owner.",
    ),
    MutationCase(
        guard_id="install-deployment-trust-policy-outcome",
        rule_id="install-deployment-trust-policy-outcome",
        path="src/apm_cli/security/executables.py",
        old=(
            "    policy = resolve_trust_policy(\n"
            "        manifest_root(project_root, user_scope=user_scope), no_policy=no_policy, "
            "logger=logger\n"
            "    )\n"
        ),
        new=(
            "    from apm_cli.policy.discovery import discover_policy_with_chain\n\n"
            '    policy = getattr(discover_policy_with_chain(project_root), "policy", None)\n'
        ),
        intent="A trust reader drops the policy discovery outcome and keeps only .policy.",
    ),
    MutationCase(
        guard_id="install-deployment-uninstall-reachability",
        rule_id="install-deployment-uninstall-reachability",
        path="src/apm_cli/install/orphans.py",
        old="    walk = compute_forward_reachable_keys(",
        new="    walk = _parallel_manifest_walk(",
        intent="The orphan owner stops consuming deps/reachability.py's forward walk.",
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
        guard_id="install-deployment-unpack-executable-refusal",
        rule_id="install-deployment-unpack-executable-refusal",
        path="src/apm_cli/bundle/unpacker.py",
        old="        _refuse_executable_content(unique_files, source_dir, bundle_path)\n",
        new="",
        intent="apm unpack copies executable bundle content without refusing it.",
    ),
    MutationCase(
        guard_id="install-deployment-update-content-integrity",
        rule_id="install-deployment-update-content-integrity",
        path="src/apm_cli/install/source_helpers.py",
        old='    return bool(commit not in (None, "", "cached") and commit == locked_commit)\n',
        new="    return False\n",
        intent="apm update treats every locked commit as moved and skips the content-hash check.",
    ),
    MutationCase(
        guard_id="install-deployment-update-registry-integrity",
        rule_id="install-deployment-update-registry-integrity",
        path="src/apm_cli/install/sources.py",
        old="                    package_info = download_registry_package(\n",
        new="                    package_info = _registry_resolver.download_package(\n",
        intent="A registry update download skips the locked-hash check for an unchanged version.",
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
        guard_id="marketplace-integrations-authoring-source",
        rule_id="marketplace-integrations-authoring-source",
        path="src/apm_cli/marketplace/authoring_source.py",
        old="config = load_marketplace_from_apm_yml(path)",
        new="config = load_yaml_str(path.read_text())",
        intent="Live authoring catalog parses apm.yml outside the canonical parser.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-catalog-freshness",
        rule_id="marketplace-integrations-catalog-freshness",
        path="src/apm_cli/commands/marketplace/__init__.py",
        old="manifest = fetch_marketplace(source, require_fresh=True)",
        new="manifest = fetch_marketplace(source, force_refresh=True)",
        intent="Named marketplace update falls back to stale cache instead of revalidating.",
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
        guard_id="marketplace-integrations-catalog-origin",
        rule_id="marketplace-integrations-catalog-origin",
        path="src/apm_cli/deps/apm_resolver.py",
        old=(
            "require_locked_catalog_origin(\n"
            "            dep_ref.marketplace_name, dep_ref.marketplace_plugin_name, "
            "self._existing_lockfile\n"
            "        )"
        ),
        new="None",
        intent="A rebound marketplace alias resolves without comparing the locked catalog origin.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-claude-ownership",
        rule_id="marketplace-integrations-claude-ownership",
        path="src/apm_cli/native_plugins/settings.py",
        old="if enabled.get(key) is True:",
        new="if key in enabled:",
        intent="Retiring a native plugin deletes the user's explicit enabledPlugins false.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-codex-native-lifecycle",
        rule_id="marketplace-integrations-codex-native-lifecycle",
        path="src/apm_cli/codex_plugins/client.py",
        old='"expectedVersion": version',
        new='"unexpectedVersion": version',
        intent="Codex native configuration loses its public compare-and-swap no-write control.",
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
        guard_id="marketplace-integrations-dependency-confusion",
        rule_id="marketplace-integrations-dependency-confusion",
        path="src/apm_cli/deps/apm_resolver.py",
        old="        admit_marketplace_resolution(resolution)\n",
        new="",
        intent="apm.yml and transitive marketplace entries skip the #1326 admission.",
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
        guard_id="marketplace-integrations-locked-association",
        rule_id="marketplace-integrations-locked-evidence",
        path="src/apm_cli/policy/ci_checks.py",
        old=(
            "display = dep_ref.get_display_name() if dep_ref.is_marketplace else key\n"
            "        locked_dep = lock.get_dependency(key, marketplace_index=marketplace_index)"
        ),
        new=(
            "display = dep_ref.get_display_name() if dep_ref.is_marketplace else key\n"
            "        locked_dep = next((dep for dep in lock.dependencies if dep.get_unique_key() == key), None)"
        ),
        intent="Ref consistency bypasses canonical marketplace declaration-to-lock association.",
    ),
    MutationCase(
        guard_id="marketplace-integrations-locked-evidence",
        rule_id="marketplace-integrations-locked-evidence",
        path="src/apm_cli/policy/ci_checks.py",
        old="marketplace_ref_mismatch(dep_ref, locked_dep)",
        new="None",
        intent="Ref consistency accepts marketplace declarations without checking locked ref evidence.",
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
        guard_id="marketplace-integrations-pack-plugin-manifests",
        rule_id="marketplace-integrations-pack-plugin-manifests",
        path="src/apm_cli/core/build_orchestrator.py",
        old='path = PLUGIN_ECOSYSTEM_PATHS.get(target, "")',
        new='path = {"codex": ".codex-plugin/plugin.json"}.get(target, "")',
        intent="The pack producer re-declares a host manifest path outside the owner.",
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
        guard_id="marketplace-integrations-source-parsing",
        rule_id="marketplace-integrations-source-parsing",
        path="src/apm_cli/marketplace/ref_resolver.py",
        old="dependency = DependencyReference.parse(source_url)",
        new="dependency = urlparse(source_url)",
        intent="Marketplace entry coordinates stop parsing through DependencyReference.",
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
        guard_id="onboarding-metadata-only",
        rule_id="onboarding-metadata-only",
        path="src/apm_cli/adopt/discovery.py",
        old="validate_apm_package(path, read_only=True)",
        new="validate_apm_package(path, read_only=False)",
        intent="Discovery allows package admission to mutate the source it is inventorying.",
    ),
    MutationCase(
        guard_id="registry-delegation-apm-home-resolution",
        rule_id="registry_delegation.apm_home_resolution",
        path="src/apm_cli/core/scope.py",
        old='configured = os.environ.get("APM_HOME")',
        new='configured = os.environ.get("APM_ROOT")',
        intent="APM user metadata stops reading its canonical environment variable.",
    ),
    MutationCase(
        guard_id="registry-delegation-audit-target-selection",
        rule_id="registry_delegation.audit_target_selection",
        path="src/apm_cli/install/drift.py",
        old="def run_replay(config: ReplayConfig, logger: CheckLogger) -> Path:",
        new=(
            "def resolve_audit_targets(project_root): return []\n\n"
            "def run_replay(config: ReplayConfig, logger: CheckLogger) -> Path:"
        ),
        intent="Drift introduces a second audit target-selection owner.",
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
        guard_id="registry-delegation-build-id-verification",
        rule_id="registry_delegation.build_id_verification",
        path="src/apm_cli/compilation/build_id.py",
        old="def has_valid_build_id(content: str) -> bool:",
        new="def _has_valid_build_id_removed(content: str) -> bool:",
        intent="The Build ID owner loses its canonical verification entry point.",
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
        guard_id="registry-delegation-harness-user-root-resolution",
        rule_id="registry_delegation.harness_user_root_resolution",
        path="src/apm_cli/integration/lsp_integrator.py",
        old="return resolve_user_root(self.runtime)",
        new='return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")',
        intent="LSP user config re-reads COPILOT_HOME outside the user-root owner.",
    ),
    MutationCase(
        guard_id="registry-delegation-install-layering",
        rule_id="registry_delegation.install_layering",
        path="src/apm_cli/install/summary.py",
        old="from apm_cli.utils.themed_console import _rich_blank_line",
        new="from apm_cli.commands._helpers import _rich_blank_line",
        intent="The install summary imports the commands layer again.",
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
        guard_id="registry-delegation-manifest-target-declaration",
        rule_id="registry_delegation.target_vocabulary",
        path="src/apm_cli/install/target_filter.py",
        old='    package = getattr(package_source, "package", package_source)\n',
        new=(
            '    package = getattr(package_source, "package", package_source)\n'
            '    raw_targets = vars(package).get("targets")\n'
        ),
        intent="Package-target authorization re-reads the raw manifest targets field.",
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
        guard_id="registry-delegation-policy-target-projection",
        rule_id="registry_delegation.policy_target_projection",
        path="src/apm_cli/policy/policy_checks.py",
        old="            manifest_decision.canonical_targets,\n",
        new="            raw_yml,\n",
        intent="Audit judges compilation-target policy from the raw manifest dict.",
    ),
    MutationCase(
        guard_id="registry-delegation-process-interrupt-signals",
        rule_id="registry_delegation.process_interrupt_signals",
        path="src/apm_cli/cli.py",
        old="    _route_sigterm_to_interrupt()\n",
        new="",
        intent="The CLI entry stops routing SIGTERM to the interrupt rollback path.",
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
        guard_id="registry-delegation-scope-manifest-path",
        rule_id="registry_delegation.scope_manifest_path",
        path="src/apm_cli/install/phases/policy_gate.py",
        old="return discover_policy_with_chain(_policy_root(ctx))",
        new="return discover_policy_with_chain(ctx.project_root)",
        intent="User-scope install policy discovery reads the deploy root again.",
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
        guard_id="registry-delegation-user-invocable-resolution",
        rule_id="registry_delegation.user_invocable_resolution",
        path="src/apm_cli/primitives/models.py",
        old="def resolve_user_invocable(metadata: dict) -> bool:",
        new="def _resolve_user_invocable_removed(metadata: dict) -> bool:",
        intent="The user-invocable owner loses its single interpretation entry point.",
    ),
    MutationCase(
        guard_id="registry-delegation-version-rendering",
        rule_id="registry_delegation.version_rendering",
        path="src/apm_cli/version_output.py",
        old="def print_version(ctx: click.Context, param: click.Parameter | None, value: bool) -> None:",
        new="def _print_version_removed(ctx: click.Context, param: click.Parameter | None, value: bool) -> None:",
        intent="The version renderer loses its canonical print_version definition.",
    ),
    MutationCase(
        guard_id="transport-platform-ado-validation-bearer-fallback",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/install/validation.py",
        old="            run_result = auth_resolver.try_with_fallback(",
        new="            run_result = _bypass_ado_auth_chain(",
        intent="Install validation bypasses the canonical ADO credential chain.",
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
        old='                    host.auth_resolver.try_with_fallback(\n                        dep_host or "dev.azure.com",',
        new='                    _run_ado_chain_locally(\n                        dep_host or "dev.azure.com",',
        intent="Clone execution bypasses AuthResolver's ADO credential chain.",
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
        guard_id="transport-platform-archive-extraction-limits",
        rule_id="transport-platform-archive-extraction-limits",
        path="src/apm_cli/deps/download_strategies.py",
        old="return read_zip_member(zf, entry, error_type=ArchiveError)",
        new='return zf.open(entry, "r").read()',
        intent="An archive reader reads a zip member directly instead of through the capped owner reader.",
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
        guard_id="transport-platform-auth-outcome-owner",
        rule_id="transport-platform-auth-outcome-owner",
        path="src/apm_cli/policy/discovery.py",
        old="        if response.status_code != 200:\n            raise AuthAttemptFailed(response=response)\n        return response\n\n    try:\n        resp = auth_resolver.try_with_fallback(",
        new="        if response.status_code in (401, 403):\n            raise AuthAttemptFailed(response=response)\n        return response\n\n    try:\n        resp = auth_resolver.try_with_fallback(",
        intent="ADO policy fetch regrows a local 401/403 auth classification.",
    ),
    MutationCase(
        guard_id="transport-platform-authenticated-egress",
        rule_id="transport-platform-authenticated-egress",
        path="src/apm_cli/marketplace/builder.py",
        old="resp = origin_bounded_request(",
        new="resp = urllib.request.urlopen(",
        intent="Credentialed marketplace metadata fetches return to urllib, outside the owners.",
    ),
    MutationCase(
        guard_id="transport-platform-authenticated-egress-credential-headers",
        rule_id="transport-platform-authenticated-egress",
        path="src/apm_cli/deps/download_strategies.py",
        old="return cfg.headers_for(scheme, host)",
        new='return {"Authorization": f"Bearer {cfg.token}"}',
        intent="Artifactory downloads assemble a credential header outside its owners.",
    ),
    MutationCase(
        guard_id="transport-platform-cache-cleanup-outcome",
        rule_id="transport-platform-cache-cleanup-outcome",
        path="src/apm_cli/cache/git_cache.py",
        old="return clean_cache_buckets((self._db_root, self._checkouts_root))",
        new="return [str(entry) for entry in self._db_root.iterdir()]",
        intent="GitCache reintroduces its own traversal and outcome authority.",
    ),
    MutationCase(
        guard_id="transport-platform-clone-connect-retry",
        rule_id="transport-platform-clone-connect-retry",
        path="src/apm_cli/deps/clone_engine.py",
        old="_clone(url, _env_for(attempt, url), target_path)",
        new="clone_action(url, _env_for(attempt, url), target_path)",
        intent="A transport attempt invokes the clone action without canonical connection recovery.",
    ),
    MutationCase(
        guard_id="transport-platform-credential-origin-scope",
        rule_id="transport-platform-credential-origin-scope",
        path="src/apm_cli/deps/download_strategies.py",
        old="return cfg.headers_for(scheme, host)",
        new="return cfg.get_headers()",
        intent="Artifactory downloads attach the registry token to any requested origin.",
    ),
    MutationCase(
        guard_id="transport-platform-credential-report-owner",
        rule_id="transport-platform-credential-report-owner",
        path="src/apm_cli/core/auth_chain.py",
        old="    elif outcome.is_verified_rejection:",
        new="    elif outcome.is_auth_failure:",
        intent="Helper credentials are erased after a 403 or a sign-in redirect, not only a 401.",
    ),
    MutationCase(
        guard_id="transport-platform-credential-value-rendering",
        rule_id="transport-platform-credential-value-rendering",
        path="src/apm_cli/commands/config.py",
        old='                _echo_setting(f"registry.{reg_name}.token", body["token"], indent="  ")\n',
        new=(
            '                _echo_setting(f"registry.{reg_name}.token", body["token"], indent="  ")\n'
            '                click.echo(body["token"])\n'
        ),
        intent="apm config echoes a stored registry token outside the owner's mask.",
    ),
    MutationCase(
        guard_id="transport-platform-egress-route-policy",
        rule_id="transport-platform-egress-route-policy",
        path="src/apm_cli/install/pipeline.py",
        old="if is_refused(EgressSurface.SOURCE_PROBE):",
        new="if False:",
        intent="The update auth preflight probes source hosts without asking the egress-route owner.",
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
        guard_id="transport-platform-git-output-decoding",
        rule_id="transport-platform-git-child-environment",
        path="src/apm_cli/deps/git_file_transport.py",
        old="            result = run_git_text(\n                cmd,\n",
        new="            result = subprocess.run(\n                cmd,\n                text=True,\n",
        intent="Sparse Git output decodes with the locale codec and crashes on cp936.",
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
        old="                    use_ssh=False,\n                    dep_ref=dep_ref,\n                )",
        new="                    use_ssh=False,\n                    dep_ref=dep_ref,\n                    token=token,\n                )",
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
        guard_id="transport-platform-gitlab-sparse-plan",
        rule_id="transport-platform-gitlab-sparse-plan",
        path="src/apm_cli/deps/download_strategies.py",
        old="        if rest_eligible:",
        new="        if True:  # bypass rest_eligible",
        intent="GitLab file downloads bypass the executed HTTPS plan gate before REST.",
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
        guard_id="transport-platform-host-reference-coordinates",
        rule_id="transport-platform-host-reference-coordinates",
        path="src/apm_cli/models/dependency/host_virtual.py",
        old="def parse_host_qualified_reference(",
        new="def parse_host_qualified_reference_disabled(",
        intent="Host-qualified reference parsing loses its canonical coordinate owner.",
    ),
    MutationCase(
        guard_id="transport-platform-http-session-construction",
        rule_id="transport-platform-http-session-construction",
        path="src/apm_cli/registry/client.py",
        old="self.session = CredentialScopedSession(ambient_credentials=False)",
        new="self.session = requests.Session()",
        intent="The MCP registry client builds an ad hoc session outside the credential-origin owner.",
    ),
    MutationCase(
        guard_id="transport-platform-ls-remote-tag-commits",
        rule_id="transport-platform-ls-remote-tag-commits",
        path="src/apm_cli/marketplace/ref_resolver.py",
        old="            if refname not in commits:",
        new='            if refname.endswith("^{}"):',
        intent="The marketplace parser interprets peeled ^{} records outside tag_commit_shas.",
    ),
    MutationCase(
        guard_id="transport-platform-marketplace-package-remote",
        rule_id="transport-platform-marketplace-package-remote",
        path="src/apm_cli/marketplace/resolver.py",
        old="            dep_ref = lookup",
        new="            dep_ref = None",
        intent="Marketplace version resolution drops the canonical package-remote handoff.",
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
        guard_id="transport-platform-origin-bounded-redirect",
        rule_id="transport-platform-origin-bounded-redirect",
        path="src/apm_cli/deps/registry/client.py",
        old="            return origin_bounded_request(\n                self._session,\n",
        new="            return self._session.request(\n",
        intent="Registry requests bypass the origin-bounded redirect owner.",
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
        guard_id="transport-platform-release-metadata-discovery",
        rule_id="transport-platform-release-metadata-discovery",
        path="src/apm_cli/utils/version_checker.py",
        old="and effective_repo == _DEFAULT_REPO",
        new="and True",
        intent="Release metadata recovery drops the exact public repository boundary.",
    ),
    MutationCase(
        guard_id="transport-platform-revision-pin-lock-consistency",
        rule_id="transport-platform-revision-pin-lock-consistency",
        path="src/apm_cli/drift.py",
        old="or revision_pin_mismatch(dep_ref.reference, locked_dep) is not None",
        new="or False",
        intent="Install drift accepts a lock row whose commit differs from a full-SHA manifest pin.",
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
        intent="Downloader skips Git-object link materialization in its sparse clone.",
    ),
    MutationCase(
        guard_id="transport-platform-tls-trust-injection",
        rule_id="transport-platform-tls-trust-injection",
        path="src/apm_cli/core/tls_trust.py",
        old="if has_explicit_ca_override(environ):",
        new="if False:",
        intent="Additive trust bypasses the explicit replacement authority.",
    ),
    MutationCase(
        guard_id="transport-platform-unix-install-ownership",
        rule_id="transport-platform-unix-install-ownership",
        path="install.sh",
        old="\napm_require_owned_bundle\n",
        new="\n:\n",
        intent="Unix installation skips bundle ownership preflight before destructive replacement.",
    ),
    MutationCase(
        guard_id="transport-platform-url-builder-credential-free",
        rule_id="transport-platform-url-builder-credential-free",
        path="src/apm_cli/utils/github_host.py",
        old="def build_ssh_url(\n    host: str,",
        new="def build_ssh_url(\n    host: str,\n    token: str,",
        intent="A clone URL builder regrows a credential parameter.",
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

# Multiple routing edges extend existing owners; they do not allocate competing
# registry guards. Execute these through the same source-override mutation seam.
USER_ROOT_ROUTING_MUTATIONS: tuple[MutationCase, ...] = (
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/user_root_context.py",
        old="dry_run=dry_run or not clean",
        new="dry_run=dry_run",
        intent="User-root cleanup no longer requires explicit clean intent.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/user_root_context.py",
        old="protected = protected_user_root_status(output_path, existing)",
        new="protected = None",
        intent="User-root overwrite bypasses the canonical protection result.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/user_root_context.py",
        old="status = clean_redundant_user_root(",
        new="status = _local_clean_redundant_user_root(",
        intent="User-root cleanup bypasses the canonical protection and deletion owner.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/compilation/user_root_context.py",
        old="_, coverage_verdict = CompiledOutputWriter().prepare(",
        new="_, coverage_verdict = _local_policy_prepare(",
        intent="Native suppression bypasses the canonical compiled-output policy owner.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/root_context_protection.py",
        old="if not has_valid_build_id(content):",
        new="if False:",
        intent="Root protection stops verifying the generated content fingerprint.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/root_context_protection.py",
        old="if existing != expected:",
        new="if False:",
        intent="Cleanup accepts an unchanged root whose instructions are not covered by native rules.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/root_context_protection.py",
        old='if dry_run:\n        return "would-remove"',
        new='if False:\n        return "would-remove"',
        intent="Cleanup no longer stops deletion for dry-run.",
    ),
    MutationCase(
        guard_id="contracts-tooling-root-context-write-eligibility",
        rule_id="contracts-tooling-root-context-write-eligibility",
        path="src/apm_cli/compilation/build_id.py",
        old="return stabilize_build_id(template) == content",
        new="return True",
        intent="Build ID verification stops routing through the canonical hash stabilizer.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/integration/instruction_integrator.py",
        old='return rule_path.read_text(encoding="utf-8") == normalize_crlf_to_lf(expected)',
        new="return rule_path.is_file()",
        intent="Native coverage accepts file existence without comparing rendered content.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/integration/instruction_integrator.py",
        old="if has_symlink_component(deploy_root, rule_path):",
        new="if False:",
        intent="Native coverage accepts symlinked rules outside the trusted projection.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/compilation/user_root_context.py",
        old="matched = integrator.deployed_rule_matches(",
        new="matched = integrator._local_rule_matches(",
        intent="Global compilation bypasses per-source native delivery verification.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/compilation/user_root_context.py",
        old="matched = False",
        new="matched = True",
        intent="Native verification failure discards the compiled fallback.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/compilation/user_root_context.py",
        old="if not matched:\n                    target_instructions.append(instruction)",
        new="if matched:\n                    target_instructions.append(instruction)",
        intent="Global compilation removes the uncovered sources instead of retaining their fallback.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/compilation/user_root_context.py",
        old='if family == "claude":\n            try:',
        new='if family != "claude":\n            try:',
        intent="Native-rule suppression leaks into non-Claude target families.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/integration/instruction_integrator.py",
        old="rendered_rules = self._prepare_rule_plan(",
        new="rendered_rules = self._local_rule_plan(",
        intent="Direct native installation forks the projection used by coverage.",
    ),
    MutationCase(
        guard_id="hooks-integrations-native-instruction-projection",
        rule_id="hooks-integrations-native-instruction-projection",
        path="src/apm_cli/integration/instruction_integrator.py",
        old="skip, adopted = self._check_adopt_or_skip(",
        new="skip, adopted = self._local_collision_check(",
        intent="Rendered rules bypass the shared adopt-or-skip collision owner.",
    ),
    MutationCase(
        guard_id="registry-delegation-scope-manifest-path",
        rule_id="registry_delegation.scope_manifest_path",
        path="src/apm_cli/policy/ci_checks.py",
        old="apm_yml_path = manifest_file(project_root)",
        new='apm_yml_path = project_root / "apm.yml"',
        intent="Policy settings re-derive apm.yml from a root outside the scope owner.",
    ),
    MutationCase(
        guard_id="registry-delegation-scope-manifest-path",
        rule_id="registry_delegation.scope_manifest_path",
        path="src/apm_cli/security/executables.py",
        old="def effective_exec_map_for_project(",
        new=(
            "def project_manifest_path(project_root):\n"
            "    return manifest_file(project_root)\n\n\n"
            "def effective_exec_map_for_project("
        ),
        intent="A root-to-manifest sibling helper returns beside the scope owner.",
    ),
    MutationCase(
        guard_id="registry-delegation-scope-manifest-path",
        rule_id="registry_delegation.scope_manifest_path",
        path="src/apm_cli/commands/update.py",
        old="manifest_root(get_deploy_root(scope), user_scope=scope is _Scope.USER)",
        new="get_deploy_root(scope)",
        intent="User-scope update discovers policy from the deploy root.",
    ),
    MutationCase(
        guard_id="registry-delegation-scope-manifest-path",
        rule_id="registry_delegation.scope_manifest_path",
        path="src/apm_cli/policy/discovery.py",
        old="    if not policy_discovery_uses_git_remote(project_root):\n",
        new="    if False:\n",
        intent="Git-remote policy discovery bypasses the one user-scope decision.",
    ),
    MutationCase(
        guard_id="transport-platform-http-session-construction",
        rule_id="transport-platform-http-session-construction",
        path="src/apm_cli/registry/client.py",
        old="CredentialScopedSession(ambient_credentials=False)",
        new="CredentialScopedSession()",
        intent="The MCP registry client lets requests attach ambient netrc credentials.",
    ),
    MutationCase(
        guard_id="install-deployment-executable-item-admission",
        rule_id="install-deployment-executable-item-admission",
        path="src/apm_cli/commands/approve.py",
        old="                declaration = native_declaration(declaration, package_dir)\n",
        new="                pass\n",
        intent="apm approve no longer sees a native plugin's root mcp.json types (#4042).",
    ),
    MutationCase(
        guard_id="install-deployment-executable-item-admission",
        rule_id="install-deployment-executable-item-admission",
        path="src/apm_cli/install/exec_gate.py",
        old=(
            '        one_off=bool(getattr(ctx, "trust_transitive_mcp", False)),\n'
            "    )\n    if ctx is not None:"
        ),
        new=(
            '        one_off=bool(getattr(ctx, "trust_transitive_mcp", False)),\n'
            '        extra_types=("mcp",),\n    )\n    if ctx is not None:'
        ),
        intent="Install adds native plugin types the approve views cannot see (#4042).",
    ),
    MutationCase(
        guard_id="install-deployment-executable-item-admission",
        rule_id="install-deployment-executable-item-admission",
        path="src/apm_cli/security/trust.py",
        old="    found.update(dict.fromkeys(agent_plugin_exec_types(root)))\n",
        new="",
        intent="The trust owner drops an Agent Plugin's root mcp.json types (#4042).",
    ),
)

EXECUTABLE_MUTATIONS = MUTATIONS + USER_ROOT_ROUTING_MUTATIONS
EXECUTABLE_CASE_IDS = CASE_IDS + tuple(
    case.intent.rstrip(".") for case in USER_ROOT_ROUTING_MUTATIONS
)


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
def baseline_violated_rule_ids(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Run every owner rule once per session, unmutated, so each case can prove attribution.

    The cases spread across xdist workers; the first worker computes the
    baseline and the others reuse it, keyed by the rule set and source hash.
    """
    rule_ids = tuple(sorted({case.rule_id for case in MUTATIONS}))

    def compute() -> list[str]:
        report = run_selected_rules(ROOT, rule_ids)
        return sorted({violation.rule_id for violation in report.violations})

    key = sources_digest(ROOT, ARCHITECTURE_SOURCE_ROOTS, rule_ids)
    return frozenset(shared_session_value(tmp_path_factory, "owner-baseline", key, compute))


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


@pytest.mark.parametrize("case", EXECUTABLE_MUTATIONS, ids=EXECUTABLE_CASE_IDS)
def test_case_targets_the_single_rule_that_owns_its_guard(case: MutationCase) -> None:
    """Each case must name the one registered rule declaring its guard ID."""
    owners = [rule for rule in registered_rules() if case.guard_id in rule.guard_ids]

    assert len(owners) == 1, f"{case.guard_id}: expected one owning rule, found {len(owners)}"
    assert owners[0].id == case.rule_id


@pytest.mark.parametrize("case", EXECUTABLE_MUTATIONS, ids=EXECUTABLE_CASE_IDS)
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


@pytest.mark.parametrize(
    ("rule_id", "path", "old", "new"),
    [
        pytest.param(
            "mutation_writes.neutral_hook_contract",
            "src/apm_cli/integration/hook_integrator.py",
            "drop_source_entries(json_config[container], owned_markers, keep_events=cleared_events)",
            "json_config.setdefault(container, {})",
            id="merged-hooks-stop-retiring-undeclared-events-3665",
        ),
        pytest.param(
            "mutation_writes.neutral_hook_contract",
            "src/apm_cli/integration/hook_integrator.py",
            "drop_source_entries(json_config[container], owned_markers, keep_events=cleared_events)",
            "drop_source_entries(json_config[container], {source_marker}, keep_events=cleared_events)",
            id="merged-hooks-retire-without-legacy-leaf-markers",
        ),
        pytest.param(
            "install-deployment-mcp-ownership-migration",
            "src/apm_cli/integration/mcp_integrator.py",
            "claude_project_enabled = project_mcp_cleanup_reachable(",
            'claude_project_enabled = (project_root_path / ".claude").is_dir() or bool(',
            id="claude-mcp-cleanup-regates-on-opt-in-dir-3664",
        ),
        pytest.param(
            "install-deployment-outcome",
            "src/apm_cli/commands/mcp.py",
            'cli.main(args=forwarded, prog_name="apm")',
            "cli.main(args=forwarded, standalone_mode=False)",
            id="mcp-install-drops-forwarded-exit-code-3666",
        ),
    ],
)
def test_lifecycle_sweep_guard_extensions_have_teeth(
    rule_id: str, path: str, old: str, new: str
) -> None:
    """Guard sub-rules added by the 0.36.0 lifecycle sweep (#3649) reject their regressions."""
    source = _source(path)
    assert source.count(old) == 1
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_claude_cleanup_guard_preserves_unresolved_path_check() -> None:
    """The profile-path guard catches normalization before symlink inspection."""
    path = "src/apm_cli/integration/mcp_integrator.py"
    source = _source(path)
    old = "unresolved_cfg = ClaudeClientAdapter.resolve_user_claude_unresolved_path()"
    new = "unresolved_cfg = ClaudeClientAdapter.resolve_user_claude_json_path()"
    assert source.count(old) == 1
    report = run_selected_rules(
        ROOT,
        ("mutation_writes.claude_mcp_paths",),
        source_overrides={path: source.replace(old, new)},
    )
    assert report.failures == ()
    assert any(
        violation.rule_id == "mutation_writes.claude_mcp_paths" for violation in report.violations
    )


_ORPHAN_RULE = "install-deployment-orphan-selection"


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "        orphaned_packages = list(decision.removed_paths)\n",
            "        orphaned_packages = [\n"
            "            key for key in lockfile.dependencies if key not in expected_lock_keys\n"
            "        ]\n",
            "Membership in expected_lock_keys is an orphan removal predicate",
            id="second-predicate",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "        reachability=None,\n        deployment_claims=None,\n    )\n    orphaned",
            "        reachability=None,\n    )\n    orphaned",
            "call omits required input(s): deployment_claims",
            id="omitted-input",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "    decision = decide_orphan_removal(\n",
            "    decision = decide_orphan_removal(\n        OrphanOperation.AUDIT,\n",
            "takes keyword-only inputs",
            id="positional-input",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "    decision = decide_orphan_removal(\n",
            "    decide = decide_orphan_removal\n    decision = decide_orphan_removal(\n",
            "never pass or rebind it",
            id="rebound-entry",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "from ._helpers import _scan_installed_packages\n",
            "from ._helpers import _scan_installed_packages\n"
            "from ..install.orphans import _find_orphaned_packages as _select\n",
            "_find_orphaned_packages is private to install/orphans.py",
            id="private-alias-import",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "from ._helpers import _scan_installed_packages\n",
            "from ._helpers import _scan_installed_packages\n"
            "from ..install import orphans as _owner\n"
            "_select = getattr(_owner, '_find_orphaned_packages')\n",
            "Dynamic read of orphan owner name",
            id="getattr-read",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "def _preflight_prune_survivors(",
            "def _find_orphan_keys():\n    return []\n\n\ndef _preflight_prune_survivors(",
            "is a second orphan removal selector",
            id="second-selector",
        ),
        # main's four bypass tests (#3502, #2926), restated in set-difference form.
        pytest.param(
            "src/apm_cli/commands/_helpers.py",
            "        return list(\n"
            "            _inspect_installed_orphans(\n"
            "                declared_deps, lockfile, apm_modules_dir, installed\n"
            "            ).removed_paths\n"
            "        )\n",
            "        return sorted(set(installed) - wanted)\n",
            "_check_orphaned_packages must",
            id="warning-bypass",
        ),
        pytest.param(
            "src/apm_cli/install/phases/cleanup.py",
            "    for relative in decision.removed_paths:\n",
            "    for relative in sorted(set(installed) - expected):\n",
            "Deleted package paths must come from the decide_orphan_removal decision",
            id="targeted-install-bypass",
        ),
        pytest.param(
            "src/apm_cli/install/phases/cleanup.py",
            "        for orphan in decision.superseded_entries(entries):\n",
            "        for orphan in sorted(set(entries) - retained):\n",
            "Removal sites must not subtract their own sets",
            id="superseded-root-trim-bypass",
        ),
        pytest.param(
            "src/apm_cli/commands/deps/cli.py",
            "        selected_orphans = set(inspection.removed_paths)\n",
            "        selected_orphans = {\n"
            "            name for _c, name, _h, _s in scanned_candidates if name not in expected_paths\n"
            "        }\n",
            "selected_orphans must come from the decide_orphan_removal decision",
            id="deps-list-bypass",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "        orphaned_packages = list(decision.removed_paths)\n",
            "        orphaned_packages = [key for key in lockfile.dependencies if key not in wanted]\n",
            "Removal sites must not filter installed or locked sets",
            id="renamed-evidence-predicate",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "def _preflight_prune_survivors(",
            "def _stale_lock_keys(lock, wanted):\n"
            "    return set(lock.dependencies) - set(wanted)\n\n\n"
            "def _preflight_prune_survivors(",
            "Subtracting from lock rows is an orphan removal predicate",
            id="new-lock-row-helper",
        ),
        pytest.param(
            "src/apm_cli/policy/ci_checks.py",
            "    from ..install.orphans import OrphanOperation, decide_orphan_removal\n",
            "    from ..commands.prune import decide_orphan_removal\n"
            "    from ..install.orphans import OrphanOperation\n",
            "never through a re-export",
            id="re-exported-entry",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "from ._helpers import _scan_installed_packages\n",
            "from ..deps.reachability import retained_lock_keys as _keep\n"
            "from ._helpers import _scan_installed_packages\n",
            "retained_lock_keys feeds removal decisions",
            id="aliased-reachability-import",
        ),
        # In-place sink growth and helpers outside the registered sites.
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "        orphaned_packages = list(decision.removed_paths)\n",
            "        orphaned_packages = list(decision.removed_paths)\n"
            "        orphaned_packages.extend(p for p in installed_packages if p not in declared_deps)\n",
            "orphaned_packages must come from the decide_orphan_removal decision",
            id="sink-extend",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "        orphaned_packages = list(decision.removed_paths)\n",
            "        orphaned_packages = list(decision.removed_paths)\n"
            "        for package in installed_packages:\n"
            "            if package not in declared_deps:\n"
            "                orphaned_packages.append(package)\n",
            "Removal sites must not filter installed or locked sets",
            id="sink-append-loop",
        ),
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "def _preflight_prune_survivors(",
            "def _sweep(apm_modules_dir, keep):\n"
            "    for package in _scan_installed_packages(apm_modules_dir):\n"
            "        if package not in keep:\n"
            "            safe_rmtree(apm_modules_dir / package, apm_modules_dir)\n\n\n"
            "def _preflight_prune_survivors(",
            "Deleted package paths must come from the decide_orphan_removal decision",
            id="unregistered-deletion-helper",
        ),
        pytest.param(
            "src/apm_cli/install/orphans.py",
            "    deployment_claims: DeploymentClaims | None,\n) -> OrphanRemovalDecision:",
            "    deployment_claims: DeploymentClaims | None = None,\n) -> OrphanRemovalDecision:",
            "must take keyword-only inputs with no defaults",
            id="defaulted-owner-input",
        ),
    ],
)
def test_orphan_owner_guard_is_closed_world(path: str, old: str, new: str, message: str) -> None:
    """Any module, under any alias, must call the orphan owner with every input (#4011)."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_ORPHAN_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == _ORPHAN_RULE and message in violation.message
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("dry_run=True,", "dry_run=False,"),
        (
            "_check_native_claude_transition(\n            reconcile_package_targets,",
            "_ignore_native_claude_transition(\n            reconcile_package_targets,",
        ),
        (
            'if reconcile_stats.get("errors") or reconcile_stats.get("files_removed"):',
            'if reconcile_stats.get("errors"):',
        ),
    ],
)
def test_claude_native_transition_guard_requires_read_only_preservation(old: str, new: str) -> None:
    path = "src/apm_cli/install/services.py"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "marketplace-integrations-claude-ownership"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/deployment_plan.py",
            "if codex_native is not None\n        else plan_native_claude_plugin(",
            "if False\n        else plan_native_claude_plugin(",
        ),
        (
            "src/apm_cli/install/services.py",
            "native_claude_plugin = deployment_plan.claude_native",
            "native_claude_plugin = None",
        ),
        (
            "src/apm_cli/install/deployment_plan.py",
            "return [target for target in targets if target.name not in native_target_names]",
            "return targets",
        ),
        (
            "src/apm_cli/claude_plugins/registrar.py",
            "and prior.pin == own.pin",
            "and False",
        ),
        (
            "src/apm_cli/claude_plugins/registrar.py",
            "name not in prior_names\n            and existing.exists()",
            "False\n            and existing.exists()",
        ),
        (
            "src/apm_cli/claude_plugins/registrar.py",
            "None if previous is None else frozenset(prior_names),",
            "frozenset(prior_names),",
        ),
        (
            "src/apm_cli/native_plugins/settings.py",
            "if current is not None and current != declaration.marketplace_value:",
            "if False:",
        ),
        (
            "src/apm_cli/native_plugins/settings.py",
            "for plugin in sorted((owned or frozenset()) - declaration.plugins):",
            "for plugin in sorted(frozenset(enabled) - declaration.plugins):",
        ),
        (
            "src/apm_cli/install/phases/claude_plugins.py",
            "previous.write(lock_path)",
            "pass",
        ),
        (
            "src/apm_cli/native_plugins/layout.py",
            "import json\n",
            "import json\nimport subprocess\n",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "elif _is_local_claude_plugin_source(dependency, package_dir):",
            "elif False:",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "if not package_dir.is_dir() or manifest_file(package_dir).exists():",
            "if not package_dir.is_dir():",
        ),
        (
            "src/apm_cli/claude_plugins/admission.py",
            "return compute_native_plugin_hash(root)",
            "return compute_package_hash(root)",
        ),
        (
            "src/apm_cli/claude_plugins/registrar.py",
            "if fingerprint_native_claude_source(source) != own.record.fingerprint:",
            "if False:",
        ),
        (
            "src/apm_cli/install/services.py",
            'result["native_claude_plugin"] = True',
            'result["native_claude_plugin"] = False',
        ),
        (
            "src/apm_cli/install/drift.py",
            'if result.get("native_plugin") and not result.get("native_claude_plugin"):',
            'if result.get("native_plugin"):',
        ),
        (
            "src/apm_cli/install/drift.py",
            'local_modules_dir=scratch_root / "apm_modules",',
            "local_modules_dir=apm_modules_dir,",
        ),
        (
            "src/apm_cli/install/drift.py",
            "verify_claude_native_cache(",
            "ignore_native_fingerprint(",
        ),
    ],
)
def test_claude_native_fingerprint_guard_cannot_fall_back_to_legacy_hash_or_mutate_cache(
    path: str, old: str, new: str
) -> None:
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "marketplace-integrations-claude-ownership"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_prune_hands_the_reachability_walk_to_the_orphan_owner() -> None:
    """Prune's retained keys still come from deps/reachability.py, via the owner (#3668)."""
    path = "src/apm_cli/commands/prune.py"
    source = _source(path)
    old = "                reachability=ReachabilityProbe(\n"
    assert source.count(old) == 1
    mutated = source.replace(
        old, "                reachability=None if True else ReachabilityProbe(\n", 1
    )
    ast.parse(mutated, filename=path)
    rule_id = "install-deployment-uninstall-reachability"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


_GIT_CHILD_ENV_RULE = "transport-platform-git-child-environment"
_SPARSE_SYMLINK_RULE = "transport-platform-sparse-symlink-validation"


@pytest.mark.parametrize(
    ("rule_id", "path", "old", "new", "message"),
    [
        (
            _GIT_CHILD_ENV_RULE,
            "src/apm_cli/deps/bare_cache.py",
            "pin_args = [*git_symlink_pin_args(), *git_line_ending_pin_args()]",
            'pin_args = [*git_symlink_pin_args(), *git_line_ending_pin_args(), "-c", "core.eol=crlf"]',
            "The core.eol pin must route through utils/git_env.git_line_ending_pin_*",
        ),
        (
            _GIT_CHILD_ENV_RULE,
            "src/apm_cli/cache/git_cache.py",
            "        *git_line_ending_pin_args(),\n",
            '        "-c",\n        "core.autocrlf=false",\n',
            "Dependency checkouts must write link text, materialize it from Git objects",
        ),
        (
            _GIT_CHILD_ENV_RULE,
            "src/apm_cli/utils/git_env.py",
            "    retained.extend(pinned)",
            "    retained.append(git_symlink_pin_entry())",
            "utils/git_env.py must own repository-state and URL rewrite safety",
        ),
        (
            _SPARSE_SYMLINK_RULE,
            "src/apm_cli/cache/git_cache.py",
            "                            for key, value in _checkout_materialization_pins()\n",
            "                            for key, value in (\n"
            "                                *git_line_ending_pin_entries(),\n"
            "                                _LINK_MATERIALIZATION_PIN,\n"
            "                            )\n",
            "git_cache checkouts must materialize links through the canonical owner",
        ),
    ],
    ids=[
        "eol-literal-outside-owner",
        "cache-bypasses-line-ending-owner",
        "snapshot-drops-pins",
        "shard-pins-bypass-shared-list",
    ],
)
def test_line_ending_pin_guard_rejects_owner_bypass(
    rule_id: str, path: str, old: str, new: str, message: str
) -> None:
    """Dependency checkouts cannot drop or re-implement the materialization pins (#3270)."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.message.startswith(message)
        for violation in report.violations
    ), [violation.message for violation in report.violations]


@pytest.mark.parametrize(
    ("path", "rule_id", "old", "new"),
    [
        (
            "src/apm_cli/install/mcp/ownership.py",
            "mutation_writes.opencode_enabled_intent",
            "opencode_enabled_matches(existing[name], expected)",
            "True",
        ),
        (
            "src/apm_cli/integration/mcp_integrator_install.py",
            "mutation_writes.mcp_target_selection",
            "_raise_strict_config_failures(unsafe_runtimes, console=console, logger=logger)",
            "_raise_strict_config_failures([], console=console, logger=logger)",
        ),
    ],
)
def test_global_mcp_consumer_guards(path: str, rule_id: str, old: str, new: str) -> None:
    """Global consumers cannot bypass native type or selected-target safety owners."""
    baseline = run_selected_rules(ROOT, (rule_id,))
    assert baseline.failures == ()
    assert baseline.violations == ()
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/mcp/ownership.py",
            "def resolve_mcp_target_servers(",
            "def bypass_resolve_mcp_target_servers(",
        ),
        (
            "src/apm_cli/integration/mcp_integrator_install.py",
            "state = compare_native_mcp_connection(\n                    client,\n"
            "                    dep.name,",
            "state = noncanonical_comparison(\n                    client,\n"
            "                    dep.name,",
        ),
        (
            "src/apm_cli/integration/mcp_integrator.py",
            "verified_keys = require_removable_mcp_connections(\n                codex_client,",
            "verified_keys = bypass_removal_owner(\n                codex_client,",
        ),
        (
            "src/apm_cli/commands/uninstall/engine.py",
            "                fail_on_write_error=True,\n"
            "                stored_configs=lockfile.mcp_configs,\n",
            "                fail_on_write_error=True,\n",
        ),
        (
            "src/apm_cli/core/conflict_detector.py",
            "class MCPConflictDetector:",
            "def compare_native_mcp_connection(*args, **kwargs):\n"
            "    return True\n\n\nclass MCPConflictDetector:",
        ),
        (
            "src/apm_cli/install/mcp/ownership.py",
            "comparison = client.project_managed_server_config(",
            "comparison = client.bypass_managed_server_config(",
        ),
        (
            "src/apm_cli/core/conflict_detector.py",
            "class MCPConflictDetector:",
            "def bypass_owner(client):\n"
            "    return client.project_managed_server_config({}, baseline={}, requested={})\n"
            "\n\nclass MCPConflictDetector:",
        ),
        (
            "src/apm_cli/install/mcp/ownership.py",
            '    """Refuse removal when recorded names do not prove unchanged native content."""',
            '    """Refuse removal when recorded names do not prove unchanged native content."""\n'
            "    client.project_managed_server_config({}, baseline={}, requested={})",
        ),
        (
            "src/apm_cli/core/conflict_detector.py",
            "class MCPConflictDetector:",
            "def mcp_native_hash(native):\n    return 'unverified'\n\n\nclass MCPConflictDetector:",
        ),
        (
            "src/apm_cli/integration/mcp_integrator.py",
            "                codex_cfg,\n                verified_keys,",
            "                codex_cfg,\n                expanded_stale,",
        ),
        (
            "src/apm_cli/adapters/client/claude.py",
            "fingerprint = mcp_native_hash(",
            "fingerprint = unverified_hash(",
        ),
        (
            "src/apm_cli/install/mcp/integration.py",
            "retain_deferred_mcp_claims(managed_targets, previous_targets, new_names)",
            "bypass_deferred_claims(managed_targets, previous_targets, new_names)",
        ),
        (
            "src/apm_cli/integration/mcp_integrator_install.py",
            "state = compare_native_mcp_connection(\n                            client,\n"
            "                            dep,",
            "state = noncanonical_comparison(\n                            client,\n"
            "                            dep,",
        ),
        (
            "src/apm_cli/install/phases/lockfile.py",
            "native_hashes=DeploymentLedgerCodec.mcp_native_hashes(",
            "native_hashes=DeploymentLedgerCodec.unverified_mcp_native_hashes(",
        ),
    ],
    ids=[
        "legacy-resolver",
        "install-routing",
        "removal-routing",
        "removal-baseline",
        "second-owner",
        "projection-bypass",
        "projection-external",
        "projection-cleanup",
        "second-fingerprint-owner",
        "cleanup-expanded-alias",
        "actual-write-fingerprint",
        "bundle-deferred-claims",
        "registry-install-routing",
        "rebuild-receipt-routing",
    ],
)
def test_native_mcp_ownership_boundary_mutations(path: str, old: str, new: str) -> None:
    """Native collision and cleanup callers cannot bypass or duplicate the baseline owner."""
    rule_id = "install-deployment-mcp-ownership-migration"
    baseline = run_selected_rules(ROOT, (rule_id,))
    assert baseline.failures == ()
    assert baseline.violations == ()
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == rule_id for item in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new", "rule_id"),
    [
        (
            "src/apm_cli/commands/install.py",
            "allow_pending_mcp=ctx.install_mode != InstallMode.MCP,",
            "allow_pending_mcp=True,",
            "install-deployment-frozen-mutation-eligibility",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "if InstallService.frozen_disposition(ctx).replay and ctx.existing_lockfile is not None:",
            "if False:",
            "install-deployment-frozen-mutation-eligibility",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "InstallService.enforce_hydrated_mcp(",
            "InstallService.skip_hydrated_mcp(",
            "install-deployment-frozen-mutation-eligibility",
        ),
        (
            "src/apm_cli/install/service.py",
            "allow_pending=False,",
            "allow_pending=True,",
            "install-deployment-frozen-mutation-eligibility",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "require_locked=True,",
            "require_locked=False,",
            "mutation_writes.mcp_declaration_scope",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "active_package_keys=frozenset(ctx.update_plan_complete_dep_keys),",
            "active_package_keys=frozenset(ctx.intended_dep_keys),",
            "install-deployment-frozen-mutation-eligibility",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "active_package_keys is not None and package_key not in active_package_keys",
            "False",
            "mutation_writes.mcp_declaration_scope",
        ),
    ],
)
def test_frozen_mcp_verification_cannot_bypass_owners(
    path: str, old: str, new: str, rule_id: str
) -> None:
    """Cold restoration must retain acquisition evidence and pre-deployment verification."""
    baseline = run_selected_rules(ROOT, (rule_id,))
    assert baseline.violations == ()
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/phases/lockfile.py",
            "if lockfile_only or frozen_disposition.withhold_lockfile_writes",
            'if lockfile_only or getattr(self.ctx, "frozen", False)',
            id="getattr-read",
        ),
        pytest.param(
            "src/apm_cli/deps/apm_resolver.py",
            "frozen=self._frozen,",
            'frozen=vars(self)["frozen"],',
            id="subscript-read-outside-install",
        ),
        pytest.param(
            "src/apm_cli/install/service_integration.py",
            "    frozen_disposition: FrozenDisposition,\n",
            "    frozen_disposition: FrozenDisposition | None = None,\n",
            id="disposition-fails-open-when-omitted",
        ),
        pytest.param(
            "src/apm_cli/install/phases/cleanup.py",
            "    frozen_disposition: FrozenDisposition,\n) -> None:",
            "    frozen_disposition: FrozenDisposition,\n    frozen: bool = False,\n) -> None:",
            id="new-bare-frozen-parameter",
        ),
        pytest.param(
            "src/apm_cli/install/phases/cleanup.py",
            "    frozen_disposition = InstallService.frozen_disposition(ctx)\n",
            (
                "    match vars(ctx):\n"
                '        case {"frozen": replay}:\n'
                "            del replay\n"
                "    frozen_disposition = InstallService.frozen_disposition(ctx)\n"
            ),
            id="mapping-pattern-read",
        ),
        pytest.param(
            "src/apm_cli/install/phases/cleanup.py",
            "    frozen_disposition = InstallService.frozen_disposition(ctx)\n",
            "    ctx.frozen |= False\n    frozen_disposition = InstallService.frozen_disposition(ctx)\n",
            id="augmented-assignment-read",
        ),
    ],
)
def test_frozen_flag_reads_outside_owner_fail_guard(path: str, old: str, new: str) -> None:
    """Only InstallService reads the frozen flag; consumers require its disposition."""
    rule_id = "install-deployment-frozen-flag-reader"
    assert run_selected_rules(ROOT, (rule_id,)).violations == ()
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_frozen_flag_writes_and_defaults_are_not_reads() -> None:
    """Writes, deletes and a get/pop default named "frozen" do not read the flag."""
    rule_id = "install-deployment-frozen-flag-reader"
    path = "src/apm_cli/install/phases/cleanup.py"
    old = "    frozen_disposition = InstallService.frozen_disposition(ctx)\n"
    new = (
        '    options = {"frozen": False}\n'
        '    options["frozen"] = False\n'
        '    del options["frozen"]\n'
        '    options.get("mode", "frozen")\n'
        '    options.pop("mode", "frozen")\n'
        f"{old}"
    )
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert report.violations == ()


def test_frozen_preflight_uses_selected_lockfile_store() -> None:
    """Scoped frozen preflight must not substitute a source-side lockfile."""
    path = "src/apm_cli/install/service.py"
    source = _source(path)
    old = "project_dir = get_lockfile_dir(request.scope)"
    assert source.count(old) == 1
    mutated = source.replace(old, "project_dir = Path(manifest_path)", 1)
    ast.parse(mutated, filename=path)
    rule_id = "install-deployment-frozen-mutation-eligibility"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/commands/install.py",
            "ctx.with_resource(InstallService.frozen_writes(frozen))",
            "ctx.with_resource(contextlib.nullcontext())",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "InstallService.enforce_frozen_services(",
            "InstallService.disabled_service_guard(",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "prepared=prepared_mcp,",
            "prepared=None,",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "prepared=prepared_lsp,",
            "prepared=None,",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "current_mcp=prepared_mcp.current_view,",
            "current_mcp=None,",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "    return ServiceIntegrationResult(mcp_count, lsp_count, target_decision)",
            "    raise FrozenInstallError('second frozen authority')",
        ),
        (
            "src/apm_cli/install/outcome.py",
            'complete_dep_keys = getattr(ctx, "update_plan_complete_dep_keys", None)',
            'complete_dep_keys = getattr(ctx, "intended_dep_keys", None)',
        ),
        (
            "src/apm_cli/commands/install.py",
            "install_result.active_package_keys if install_result is not None else None",
            "None",
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "allow_pending=False,",
            "allow_pending=None,",
        ),
        (
            "src/apm_cli/install/mcp/integration.py",
            "allow_pending=allow_pending,",
            "allow_pending=None,",
        ),
    ],
)
def test_frozen_native_service_boundary_guards(path: str, old: str, new: str) -> None:
    """Command lifetime, prepared evidence and eligibility retain one owner."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "install-deployment-frozen-mutation-eligibility"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_remote_shape_guard_rejects_retired_rows_without_native_comparison() -> None:
    """A row with retired shapes cannot heal on an adapter that skips native comparison."""
    path = "src/apm_cli/adapters/client/hermes.py"
    old = "    supports_user_scope: bool = True\n"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, old + "    compares_native_connections: bool = False\n", 1)
    ast.parse(mutated, filename=path)
    rule_id = "mutation_writes.mcp_remote_shape_table"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and "compares_native_connections" in violation.message
        for violation in report.violations
    )


def test_ref_freshness_guard_rejects_unconditional_cache_publication() -> None:
    """A checkout must not promote a lock pin into a fresh named observation."""
    path = "src/apm_cli/deps/github_downloader.py"
    source = _source(path)
    old = "resolver.remotely_resolved(dep_ref, locked_sha) is True"
    assert source.count(old) == 1
    mutated = source.replace(old, "True", 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(
        ROOT,
        ("transport-platform-ref-freshness",),
        source_overrides={path: mutated},
    )
    assert report.failures == ()
    assert any(
        violation.rule_id == "transport-platform-ref-freshness" for violation in report.violations
    )


def test_git_semver_guard_rejects_bypassing_selected_attempt_requested_url() -> None:
    """AC13 must retain the selected transport attempt as the requested-URL owner."""
    path = "src/apm_cli/install/helpers/ref_reuse.py"
    source = _source(path)
    old = "    requested_url = selected_attempt.requested_url"
    assert source.count(old) == 1
    mutated = source.replace(old, "    requested_url = None  # bypass selected attempt", 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(
        ROOT,
        ("transport-platform-git-semver-preflight",),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(
        violation.rule_id == "transport-platform-git-semver-preflight"
        for violation in report.violations
    )


@pytest.mark.parametrize("argument", ["raw", "entry"])
def test_neutral_hook_contract_guard_rejects_split_cursor_field_validation(argument: str) -> None:
    """Both source and native-output validation must route through the same predicates."""
    path = "src/apm_cli/integration/hook_native_formats.py"
    source = _source(path)
    call = f"_check_cursor_field_types({argument})"
    assert source.count(call) == 1
    mutated = source.replace(call, "pass", 1)
    ast.parse(mutated, filename=path)
    rule_id = "mutation_writes.neutral_hook_contract"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


def test_neutral_hook_contract_guard_rejects_cached_cursor_preflight_gate() -> None:
    """A lexical "preflight call is present" check cannot catch a cache-gated
    reintroduction of the #3129 reused-plan bypass: the literal
    ``preflight_cursor_hooks(`` call stays present in the file whether or
    not it is wrapped in an ``if not plan.cursor_preflight_done:`` guard.
    This proves the dedicated token-forbidding sub-check (not just the
    existing presence check) is the thing that actually catches that
    specific regression shape.
    """
    path = "src/apm_cli/integration/hook_integrator.py"
    source = _source(path)
    old = (
        '        if config.target_key in {"cursor", "claude"}:\n'
        "            preflight_cursor_hooks(\n"
    )
    assert source.count(old) == 1
    mutated = source.replace(
        old,
        (
            '        if config.target_key in {"cursor", "claude"} and not getattr(\n'
            '            source_plan, "cursor_preflight_done", False\n'
            "        ):\n"
            "            preflight_cursor_hooks(\n"
        ),
        1,
    )
    ast.parse(mutated, filename=path)

    rule_id = "mutation_writes.neutral_hook_contract"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and "cursor_preflight_done" in violation.message
        for violation in report.violations
    )


def test_neutral_hook_contract_guard_rejects_removed_preflight_call_site() -> None:
    """A file-wide ``preflight_cursor_hooks(`` presence check cannot catch
    the exact per-write call being deleted from
    ``HookIntegrator._integrate_merged_hooks``: the separate, unrelated
    upfront ``preflight_hooks_for_targets`` call site leaves that substring
    present elsewhere in the file even after this one is removed. Only the
    structural, call-site-specific sub-check catches this regression shape.
    """
    path = "src/apm_cli/integration/hook_integrator.py"
    source = _source(path)
    old = (
        '        if config.target_key in {"cursor", "claude"}:\n'
        "            preflight_cursor_hooks(\n"
        "                self,\n"
        "                package_info,\n"
        "                project_root,\n"
        "                hook_sources,\n"
        "                _HOOK_EVENT_MAP,\n"
        "                user_scope=user_scope,\n"
        "                retiring_targets=retiring_targets,\n"
        "            )\n"
    )
    assert source.count(old) == 1
    mutated = source.replace(
        old,
        '        if config.target_key in {"cursor", "claude"}:\n            pass\n',
        1,
    )
    assert "preflight_cursor_hooks(" in mutated, "unrelated call site must survive the mutation"
    ast.parse(mutated, filename=path)

    rule_id = "mutation_writes.neutral_hook_contract"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id
        and "direct, unconditional statement" in violation.message
        and "preflight_cursor_hooks" in violation.message
        for violation in report.violations
    )


def test_neutral_hook_contract_guard_rejects_renamed_cache_predicate_rewrap() -> None:
    """The reused-plan bypass can be reintroduced under any predicate name,
    not just ``cursor_preflight_done``. Nesting the per-write call in a
    second conditional -- regardless of what the cache flag is called --
    must fail the structural call-site check even when the token-ban
    sub-check has nothing to match.
    """
    path = "src/apm_cli/integration/hook_integrator.py"
    source = _source(path)
    old = (
        '        if config.target_key in {"cursor", "claude"}:\n'
        "            preflight_cursor_hooks(\n"
    )
    assert source.count(old) == 1
    mutated = source.replace(
        old,
        (
            '        if config.target_key in {"cursor", "claude"}:\n'
            '            if not getattr(source_plan, "_already_rendered_once", False):\n'
            "                preflight_cursor_hooks(\n"
        ),
        1,
    )
    assert "cursor_preflight_done" not in mutated
    ast.parse(mutated, filename=path)

    rule_id = "mutation_writes.neutral_hook_contract"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})

    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id
        and "direct, unconditional statement" in violation.message
        and "preflight_cursor_hooks" in violation.message
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("if host is None and offline:", "if host is None:"),
        ('resolved_host = host or default_host() or "github.com"', 'resolved_host = "github.com"'),
        ("key = (resolved_host, org)", "key = (host, org)"),
        (
            "resolve_auth_for_host(\n                    resolved_host,",
            "resolve_auth_for_host(\n                    host,",
        ),
        ("host=resolved_host,", "host=host,"),
        ("auth_target=resolved_host,", "auth_target=host,"),
    ],
)
def test_marketplace_check_guard_rejects_default_host_auth_bypass(old: str, new: str) -> None:
    """The credential-owner guard must defend shorthand routing, not just ADO."""
    path = "src/apm_cli/commands/marketplace/check.py"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "transport-platform-host-credential-resolution"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize("case", EXECUTABLE_MUTATIONS, ids=EXECUTABLE_CASE_IDS)
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


# ---------------------------------------------------------------------------
# Targeted version-rendering / entrypoint guard mutations
# ---------------------------------------------------------------------------

_VR_RULE = "registry_delegation.version_rendering"


def test_version_rendering_guard_rejects_eager_entrypoint_import() -> None:
    """entrypoint.py must not eagerly import apm_cli.cli at top level."""
    path = "src/apm_cli/entrypoint.py"
    source = _source(path)
    old = "import sys"
    new = "import sys\nfrom apm_cli.cli import main as cli_main"
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_forbidden_import_in_renderer() -> None:
    """version_output.py must not import from apm_cli.commands."""
    path = "src/apm_cli/version_output.py"
    source = _source(path)
    old = "from .version import get_build_sha, get_version"
    new = "from .version import get_build_sha, get_version\nfrom .commands import install"
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_gitpython_in_renderer() -> None:
    """version_output.py must not import GitPython."""
    path = "src/apm_cli/version_output.py"
    source = _source(path)
    old = "import click"
    new = "import click\nimport git"
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_dropped_helpers_reexport() -> None:
    """_helpers.py must maintain its reexport from version_output."""
    path = "src/apm_cli/commands/_helpers.py"
    source = _source(path)
    old = "from ..version_output import print_version  # noqa: F401 -- compatibility re-export"
    new = "# print_version reexport removed"
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_entrypoint_manifest_bypass() -> None:
    """pyproject.toml must point apm to apm_cli.entrypoint:main."""
    path = "pyproject.toml"
    source = _source(path)
    old = 'apm = "apm_cli.entrypoint:main"'
    new = 'apm = "apm_cli.cli:main"'
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_dropped_lazy_delegation() -> None:
    """entrypoint.py must preserve lazy delegation to apm_cli.cli.main."""
    path = "src/apm_cli/entrypoint.py"
    source = _source(path)
    old = "from apm_cli.cli import main as cli_main\n\n    cli_main()"
    new = "import apm_cli.commands.install  # bypass"
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


def test_version_rendering_guard_rejects_second_renderer_in_helpers() -> None:
    """A second print_version definition in _helpers.py must be caught."""
    path = "src/apm_cli/commands/_helpers.py"
    source = _source(path)
    old = "from ..version_output import print_version  # noqa: F401 -- compatibility re-export"
    new = (
        "from ..version_output import print_version as _canonical_print_version"
        "  # noqa: F401\n\n\n"
        "def print_version(ctx, param, value):\n"
        '    """Local override."""\n'
        "    _canonical_print_version(ctx, param, value)\n"
    )
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_VR_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _VR_RULE for v in report.violations)


# ---------------------------------------------------------------------------
# Targeted proxy-route equivalence mutations (#3657)
# ---------------------------------------------------------------------------

_HOST_CRED_RULE = "transport-platform-host-credential-resolution"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/drift.py",
            "    return ArtifactoryRouter.routes_to_locked_proxy(dep_ref, locked_dep)",
            "    return bool(getattr(locked_dep, 'registry_prefix', None))",
        ),
        (
            "src/apm_cli/deps/artifactory_orchestrator.py",
            "        target = cls.routing_target(dep_ref)",
            "        target = (locked_dep.host, locked_prefix, 'https', True)",
        ),
        (
            "src/apm_cli/install/artifactory_resolver.py",
            "def _resolve_artifactory_boundary(",
            "def _proxy_routing_target(dep_ref):\n    return None\n\n\n"
            "def _resolve_artifactory_boundary(",
        ),
    ],
    ids=["drift-local-predicate", "router-skips-routing-target", "resolver-private-copy"],
)
def test_host_credential_guard_rejects_proxy_route_bypass(path: str, old: str, new: str) -> None:
    """Proxy-route equivalence must reuse ArtifactoryRouter.routing_target."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_HOST_CRED_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _HOST_CRED_RULE for v in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/phases/policy_target_check.py",
            "effective_target=target_decision.canonical_targets,",
            'effective_target=getattr(ctx.apm_package, "target", None),',
            id="install-gate-raw-manifest",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/command.py",
            "effective_target=target_decision.canonical_targets,",
            "effective_target=target_decision.value,",
            id="mcp-install-raw-selection",
        ),
        pytest.param(
            "src/apm_cli/install/bundle_admission.py",
            "effective_target=target_decision.canonical_targets,",
            "effective_target=[resolved.name for resolved in targets],",
            id="local-bundle-raw-names",
        ),
        pytest.param(
            "src/apm_cli/core/target_detection.py",
            "def canonical_policy_target_name(value: str | None) -> str | None:",
            "def normalize_policy_targets(value):\n    return value\n\n\n"
            "def canonical_policy_target_name(value: str | None) -> str | None:",
            id="sibling-normalizer",
        ),
    ],
)
def test_policy_target_projection_guard_rejects_raw_selections(
    path: str, old: str, new: str
) -> None:
    """Every compilation-target policy seam judges the canonical projection (#3732)."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "registry_delegation.policy_target_projection"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/summary.py",
            "from apm_cli.utils.themed_console import _rich_blank_line",
            "import apm_cli.commands._helpers\n"
            "from apm_cli.utils.themed_console import _rich_blank_line",
            id="plain-import-of-commands",
        ),
        pytest.param(
            "src/apm_cli/commands/_helpers.py",
            "from ..models.manifest_bootstrap import bootstrap_config as _get_bootstrap_config\n",
            "def _get_bootstrap_config(*args, **kwargs):\n    return {}\n",
            id="retired-bootstrap-writer-redefined",
        ),
    ],
)
def test_install_layering_guard_rejects_bypasses(path: str, old: str, new: str) -> None:
    """install/** never imports apm_cli.commands, in any import form (#3879)."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "registry_delegation.install_layering"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/policy/policy_checks.py",
            "    manifest_decision = EffectiveTargetDecision(",
            '    raw_target = (raw_yml or {}).get("target")\n'
            "    manifest_decision = EffectiveTargetDecision(",
            id="audit-raw-get",
        ),
        pytest.param(
            "src/apm_cli/install/phases/policy_target_check.py",
            "    target_decision = ctx.target_decision\n",
            "    target_decision = ctx.target_decision\n"
            '    config_target = getattr(ctx.apm_package, "target", None)\n',
            id="install-gate-getattr",
        ),
        pytest.param(
            "src/apm_cli/commands/compile/cli.py",
            "        if isinstance(config_target, list) and len(config_target) == 1:\n",
            "        config_target = parse_targets_field({})\n"
            "        if isinstance(config_target, list) and len(config_target) == 1:\n",
            id="compile-reparse",
        ),
        pytest.param(
            "src/apm_cli/policy/parser.py",
            "# architecture-authority-exempt: policy-schema",
            "# architecture-authority-exempt: anything-goes",
            id="unknown-exemption-concept",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_bootstrap.py",
            '        apm_yml_data["targets"] = list(config["targets"])\n',
            '        apm_yml_data["targets"] = list(config["targets"])\n'
            "        leaked = package.targets\n",
            id="exempt-block-does-not-cover-nested-read",
        ),
        pytest.param(
            "src/apm_cli/bundle/local_bundle.py",
            '    target = pack.get("target")  # architecture-authority-exempt: bundle-pack-metadata\n',
            '    target = pack.get("target")  # architecture-authority-exempt: bundle-pack-metadata\n'
            '    leaked = lockfile.get("targets")\n',
            id="trailing-marker-does-not-cover-next-line",
        ),
        pytest.param(
            "src/apm_cli/install/target_filter.py",
            '    package = getattr(package_source, "package", package_source)\n',
            '    package = getattr(package_source, "package", package_source)\n'
            "    if package.targets:\n"
            "        # architecture-authority-exempt: bootstrap-writer\n"
            "        pass\n",
            id="body-marker-does-not-cover-header",
        ),
        pytest.param(
            "src/apm_cli/install/target_filter.py",
            '    package = getattr(package_source, "package", package_source)\n',
            '    package = getattr(package_source, "package", package_source)\n'
            '    raw_targets = package_source["targets"]\n',
            id="raw-subscript",
        ),
        pytest.param(
            "src/apm_cli/install/target_filter.py",
            '    package = getattr(package_source, "package", package_source)\n',
            '    package = getattr(package_source, "package", package_source)\n'
            '    declared = "targets" in vars(package)\n',
            id="raw-key-test",
        ),
        pytest.param(
            "src/apm_cli/install/target_filter.py",
            '    package = getattr(package_source, "package", package_source)\n',
            '    package = getattr(package_source, "package", package_source)\n'
            '    raw_targets = dict(vars(package)).pop("targets", None)\n',
            id="raw-pop",
        ),
        pytest.param(
            "src/apm_cli/install/pipeline.py",
            "import builtins\n",
            "import builtins\n\n_NOTICE = \"apm.yml uses the deprecated 'target:' key.\"\n",
            id="deprecation-warning-outside-owner",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_targets.py",
            "    replacement = declaration.targets_replacement(user_scope=user_scope)\n",
            "    replacement = tuple(declaration.authored_names())\n",
            id="warning-bypasses-replacement-owner",
        ),
        pytest.param(
            "src/apm_cli/install/pipeline.py",
            "import builtins\n",
            "import builtins\n\n\ndef targets_replacement(value):\n    return value\n",
            id="second-target-replacement-mapping",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_targets.py",
            "            return rewrite_target_names(self) or ()\n",
            "            return tuple(self.canonical) or ()\n",
            id="warning-suggestion-bypasses-rewrite-mapping",
        ),
        pytest.param(
            "src/apm_cli/commands/init.py",
            "rewrite_target_names(ManifestTargetDeclaration.from_mapping(data, apm_yml_path))",
            "ManifestTargetDeclaration.from_mapping(data, apm_yml_path).selection",
            id="reinit-bypasses-rewrite-mapping",
        ),
        pytest.param(
            "src/apm_cli/commands/init.py",
            "    apm_yml_path = manifest_file(project_root)\n",
            "    apm_yml_path = manifest_file(project_root)\n"
            "    _names = ManifestTargetDeclaration().authored_names()\n",
            id="authored-names-outside-rewrite-owner",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_bootstrap.py",
            "    verify_rewritten_manifest(out_file, content)\n",
            "    pass\n",
            id="scaffold-writes-unverified-rewrite",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/command.py",
            'config["targets"] = list(bootstrap_target_names(supported_runtimes))',
            'config["targets"] = list(supported_runtimes)',
            id="user-manifest-bootstrap-writes-raw-runtimes",
        ),
        pytest.param(
            "src/apm_cli/commands/init.py",
            "targets = option_target_names(target_flag)",
            "targets = list(target_flag or []) or None",
            id="init-target-flag-bypasses-owner",
        ),
        pytest.param(
            "src/apm_cli/install/manifest_helpers.py",
            "return option_target_names(target)",
            "return [target] if isinstance(target, str) else target",
            id="package-bootstrap-bypasses-owner",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_targets.py",
            "    _refuse_ide_targets(selected)\n",
            "",
            id="mcp-bootstrap-records-ide",
        ),
        pytest.param(
            "src/apm_cli/core/target_detection.py",
            "def normalize_target_list(",
            "def manifest_targets_from_target_option(target):\n"
            "    return target\n\n\n"
            "def normalize_target_list(",
            id="second-target-option-authority-returns",
        ),
        pytest.param(
            "src/apm_cli/commands/install.py",
            "initial_manifest_config = _bootstrap(mcp_manifest_path, supported_runtimes, target, runtime)",
            'initial_manifest_config = {"name": "me", "targets": supported_runtimes}',
            id="install-command-bypasses-bootstrap-owner",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_bootstrap.py",
            "    verify_rewritten_manifest(out_file, content)\n",
            "    if False:\n        verify_rewritten_manifest(out_file, content)\n",
            id="scaffold-verification-made-optional",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_targets.py",
            "def rewrite_target_names(",
            "def _rewrite_target_names_removed(",
            id="declaration-owner-loses-rewrite-mapping",
        ),
    ],
)
def test_manifest_target_declaration_guard_rejects_raw_readers(
    path: str, old: str, new: str
) -> None:
    """Every package-target reader routes through ManifestTargetDeclaration (#3732).

    Every apm.yml target rewrite routes through the one rewrite mapping and
    verifies the rewritten manifest before writing it (#3857).
    """
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "registry_delegation.target_vocabulary"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/models/manifest_io.py",
            "    reject_manifest_anchors(manifest_path, text)\n",
            "",
            id="read-owner-stops-rejecting-anchors",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_io.py",
            "return check_manifest_text(path, Path(path).read_text(",
            "return (lambda text: text)(Path(path).read_text(",
            id="read-skips-manifest-checks",
        ),
        pytest.param(
            "src/apm_cli/models/manifest_targets.py",
            "data = load_manifest(manifest_path)",
            "data = load_yaml(manifest_path)",
            id="target-declaration-read-bypasses-owner",
        ),
        pytest.param(
            "src/apm_cli/commands/audit.py",
            "        read_manifest_text(manifest_path)\n",
            '        manifest_path.read_text(encoding="utf-8")\n',
            id="audit-skips-manifest-checks",
        ),
        pytest.param(
            "src/apm_cli/utils/yaml_io.py",
            "data, Dumper=_NoAliasSafeDumper,",
            "data, Dumper=yaml.SafeDumper,",
            id="yaml-writer-emits-anchors",
        ),
        pytest.param(
            "src/apm_cli/utils/yaml_io.py",
            "    rt.representer.ignore_aliases = lambda _data: True\n",
            "",
            id="roundtrip-writer-emits-anchors",
        ),
    ],
)
def test_consumer_manifest_guard_rejects_anchor_bypasses(path: str, old: str, new: str) -> None:
    """Every apm.yml read refuses YAML anchors and aliases (req-mf-020(b), #3858)."""
    source = _source(path)
    assert source.count(old) >= 1
    mutated = source.replace(old, new)
    ast.parse(mutated, filename=path)
    rule_id = "install-deployment-consumer-manifest-rewrite"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/core/errors.py",
            '        "No harness detected\\n"\n',
            '        "[x] No harness detected\\n"\n',
            id="renderer-embeds-status-symbol",
        ),
        pytest.param(
            "src/apm_cli/core/apm_yml.py",
            "    \"'targets:' in apm.yml is empty\\n\"\n",
            "    \"[x] 'targets:' in apm.yml is empty\\n\"\n",
            id="empty-targets-message-embeds-status-symbol",
        ),
        pytest.param(
            "src/apm_cli/commands/init.py",
            'f"one: {detail.strip()}"',
            "f\"one: {detail.strip().removeprefix('[x] ')}\"",
            id="caller-strips-status-symbol",
        ),
        pytest.param(
            "src/apm_cli/commands/init.py",
            'f"one: {detail.strip()}"',
            "f\"one: {detail.strip().replace('[x] ', '')}\"",
            id="caller-replaces-status-symbol",
        ),
        pytest.param(
            "src/apm_cli/commands/outdated.py",
            "console=new_console(),",
            "console=Console(),",
            id="command-builds-unicode-rich-console",
        ),
        pytest.param(
            "src/apm_cli/commands/outdated.py",
            "from apm_cli.utils.console import new_console\n",
            "from rich.console import Console as new_console\n",
            id="command-imports-rich-console",
        ),
    ],
)
def test_output_diagnostics_guard_keeps_target_errors_plain(path: str, old: str, new: str) -> None:
    """Target error messages carry no status symbol; the CLI owner adds it (#3871)."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    rule_id = "registry_delegation.output_diagnostics"
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


# ---------------------------------------------------------------------------
# Helper credential report side doors (#3765)
# ---------------------------------------------------------------------------

_CRED_REPORT_RULE = "transport-platform-credential-report-owner"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/core/auth.py",
            "            if cred:\n",
            "            if cred:\n                subprocess.run(['git', 'credential', 'approve'])\n",
            id="raw-argv-outside-runner",
        ),
        pytest.param(
            "src/apm_cli/core/auth.py",
            "            if cred:\n",
            "            if cred:\n"
            "                self._token_manager.report_git_helper_credential(cred, accepted=True)\n",
            id="runner-called-outside-chain",
        ),
        pytest.param(
            "src/apm_cli/core/auth_outcome.py",
            "rejection_verified=credential_presented and status == 401,",
            "rejection_verified=credential_presented,",
            id="403-counts-as-verified",
        ),
        pytest.param(
            "src/apm_cli/core/auth_chain.py",
            "    if not _HELPER_REPORTS.decide(key, action, credential.password):\n        return\n",
            "",
            id="no-once-per-key",
        ),
    ],
)
def test_credential_report_guard_rejects_side_doors(path: str, old: str, new: str) -> None:
    """approve/reject stays in the chain owner, once per key, reject only on a 401."""
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (_CRED_REPORT_RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(v.rule_id == _CRED_REPORT_RULE for v in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/factory.py",
            '        if key == "copilot" and not user_scope:\n',
            '        if key == "copilot" and user_scope is None:\n',
            id="factory-stops-routing-project-copilot",
        ),
        pytest.param(
            "src/apm_cli/adapters/client/copilot_project.py",
            'PROJECT_MCP_JSON = ".mcp.json"\n',
            'PROJECT_MCP_JSON = ".github/mcp.json"\n',
            id="project-destination-moves-back-to-github",
        ),
        pytest.param(
            "src/apm_cli/adapters/client/copilot_project.py",
            "class CopilotProjectClientAdapter(ClaudeClientAdapter):\n",
            "class CopilotProjectClientAdapter(CopilotClientAdapter):\n",
            id="project-adapter-drops-shared-claude-shape",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/ownership.py",
            'frozenset({"claude", "copilot"})',
            "frozenset()",
            id="mcp-json-ownership-not-shared",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "import builtins\n",
            'import builtins\n\n_LEGACY = ".github/mcp.json"\n',
            id="second-module-names-retired-destination",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "    heal_basis = pre_install_lock if pre_install_lock is not None else previous_lock\n",
            "    heal_basis = previous_lock\n",
            id="heal-gate-reads-the-stamped-lock",
        ),
        pytest.param(
            "src/apm_cli/install/service_integration.py",
            "            pre_install_lock=existing_lock,\n",
            "",
            id="install-drops-the-pre-install-lock",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "    retired = split_mcp_claims(\n",
            "    retired = RetiredMcpClaims({}, {}) or split_mcp_claims(\n",
            id="bundle-reads-retired-claims-as-live",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/command.py",
            "                    retired = split_mcp_claims(\n",
            "                    retired = RetiredMcpClaims({}, {}) or split_mcp_claims(\n",
            id="install-mcp-reads-retired-claims-as-live",
        ),
        pytest.param(
            "src/apm_cli/commands/uninstall/engine.py",
            "    retired = split_mcp_claims(\n",
            "    retired = RetiredMcpClaims({}, {}) or split_mcp_claims(\n",
            id="uninstall-reads-retired-claims-as-live",
        ),
        pytest.param(
            "src/apm_cli/commands/update.py",
            "            pre_install_lock=existing_lock,\n",
            "",
            id="update-drops-the-pre-install-lock",
        ),
    ],
)
def test_copilot_project_mcp_destination_guard_rejects_mutations(
    path: str, old: str, new: str
) -> None:
    """#3866: project copilot MCP goes to the shared workspace-root .mcp.json."""
    rule_id = "mutation_writes.copilot_cli_mcp_paths"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            '            target_subset=canonical_target_subset(data.get("target_subset")),\n',
            '            target_subset=list(data.get("target_subset") or []),\n',
            id="lock-read-skips-target-canonicalisation",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile.py",
            "            target_subset=canonical_target_subset(dep_ref.target_subset)\n",
            "            target_subset=sorted(dep_ref.target_subset)\n",
            id="lock-write-skips-target-canonicalisation",
        ),
        pytest.param(
            "src/apm_cli/models/dependency/subsets.py",
            "        if canonical_target_name(name) in valid_set:\n"
            "            name = canonical_target_name(name)\n",
            "",
            id="edge-parse-keeps-alias-spelling",
        ),
    ],
)
def test_lock_target_canonicalisation_guard_rejects_mutations(
    path: str, old: str, new: str
) -> None:
    """#3866: lock target names are canonical; a 0.36 lock recording vscode reads as copilot."""
    rule_id = "install-deployment-selection-merge"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/integration/mcp_integrator_install.py",
            "    dep_runtimes = {id(dep): mcp_dependency_runtimes(dep, target_runtimes) for dep",
            "    dep_runtimes = {id(dep): list(target_runtimes) for dep",
            id="install-writes-every-server-to-every-target",
        ),
        pytest.param(
            "src/apm_cli/integration/mcp_integrator_install.py",
            "managed_target_servers[target] &= selected[target]",
            "managed_target_servers[target] &= set(managed_target_servers[target])",
            id="claims-ignore-dependency-targets",
        ),
        pytest.param(
            "src/apm_cli/integration/mcp_integrator_install.py",
            "        transfer_deselected_shared_claims(\n",
            "        (lambda *args, **kwargs: None)(\n",
            id="retarget-drops-shared-file-claim",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "admitted = mcp_servers_for_runtime(mcp_deps, owned_target)",
            "admitted = new_mcp_servers",
            id="pruning-ignores-dependency-targets",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "mcp_servers_for_runtime(mcp_deps, owned_target, named_only=True) | bundle_owned",
            "mcp_servers_for_runtime([], owned_target, named_only=True) | bundle_owned",
            id="target-scoped-install-retires-other-targets",
        ),
        pytest.param(
            "src/apm_cli/install/service_integration.py",
            "mcp_servers_for_runtime(prepared_mcp.dependencies, target)",
            "set(MCPIntegrator.get_server_names(prepared_mcp.dependencies))",
            id="frozen-claims-every-server-on-every-target",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "target_decision is not None and target_decision.from_manifest",
            'target_decision is not None and target_decision.source == "apm.yml"',
            id="manifest-drop-keyed-on-display-label",
        ),
        pytest.param(
            "src/apm_cli/install/mcp/integration.py",
            "            stale_for_target |= deselected_mcp_claims(\n",
            "            stale_for_target |= set().union(\n",
            id="owned-path-forks-deselection-algebra",
        ),
    ],
)
def test_mcp_dependency_target_selection_guard_rejects_mutations(
    path: str, old: str, new: str
) -> None:
    """#2606: per-dependency MCP targets route through the one selection owner."""
    rule_id = "mutation_writes.mcp_target_selection"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "                status, allowed_types = current_exec_decision(",
            "                status, allowed_types = package_exec_decision(",
            id="update-probe-reads-verdicts-outside-the-owner",
        ),
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "    def for_dependency(\n",
            "    def for_dependency_replay(\n",
            id="unregistered-staleness-reader",
        ),
    ],
)
def test_exec_decision_guard_rejects_second_readers(path: str, old: str, new: str) -> None:
    """#3990/#3991: one owner decides exec status and allowed types; one compares them."""
    rule_id = "install-deployment-executable-trust-context"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/commands/lock.py",
            'require_current_cli(project_root, command="apm lock")',
            'check_current_cli(project_root, command="apm lock")',
            id="command-renders-its-own-refusal",
        ),
        pytest.param(
            "src/apm_cli/deps/lockfile_generator.py",
            "        raise refusal_exit(exc) from exc",
            "        raise RuntimeError(str(exc)) from exc",
            id="gate-skips-the-shared-renderer",
        ),
    ],
)
def test_lockfile_refusal_guard_keeps_one_renderer(path: str, old: str, new: str) -> None:
    """#3967: every command renders the newer-lockfile refusal through one owner."""
    rule_id = "contracts-tooling-lockfile-generator-gate"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/integration/mcp_integrator_install.py",
            "        target_runtimes = filter_excluded_mcp_runtimes(target_runtimes, exclude)\n",
            "        target_runtimes = [rt for rt in target_runtimes if rt != exclude]\n",
            id="project-exclusion-filters-the-raw-spelling",
        ),
        pytest.param(
            "src/apm_cli/integration/mcp_integrator_install.py",
            "        target_runtimes = filter_excluded_mcp_runtimes(target_runtimes, exclude)\n",
            "        exclusions = {exclude}\n"
            "        target_runtimes = filter_excluded_mcp_runtimes(target_runtimes, exclude)\n",
            id="project-exclusion-keeps-a-second-set",
        ),
        pytest.param(
            "src/apm_cli/integration/lsp_integrator.py",
            "            candidates = filter_excluded_mcp_runtimes(candidates, exclude)\n",
            "            candidates = [target for target in candidates if target != exclude]\n",
            id="lsp-exclusion-filters-the-raw-spelling",
        ),
        pytest.param(
            "src/apm_cli/integration/lsp_integrator.py",
            "            candidates = filter_excluded_mcp_runtimes(candidates, exclude)\n",
            "            exclusions = {exclude}\n"
            "            candidates = filter_excluded_mcp_runtimes(candidates, exclude)\n",
            id="lsp-exclusion-keeps-a-second-set",
        ),
    ],
)
def test_exclusion_guard_routes_mcp_and_lsp_through_the_normalizer(
    path: str, old: str, new: str
) -> None:
    """C4 (#3967): MCP and LSP ``--exclude`` read through the one runtime normalizer."""
    rule_id = "mutation_writes.mcp_target_selection"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/commands/install.py",
            "            refusal_exit(e).show()\n",
            "            InstallLogger(verbose=verbose, dry_run=dry_run).error(str(e))\n",
            id="install-renders-the-refusal-on-stdout",
        ),
    ],
)
def test_lockfile_refusal_guard_routes_install_through_the_renderer(
    path: str, old: str, new: str
) -> None:
    """#3967: ``apm install`` renders its newer-lockfile refusal through the shared owner."""
    rule_id = "contracts-tooling-lockfile-generator-gate"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/bundle/packer.py",
            '    write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)',
            '    write_text_lf(bundle_dir / "apm.lock.yaml", yaml.safe_dump(lockfile.to_dict()))',
            id="bundle-lock-from-a-dict-dump",
        ),
        pytest.param(
            "src/apm_cli/bundle/packer.py",
            '    write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)',
            '    shutil.copy2(project_root / "apm.lock.yaml", bundle_dir / "apm.lock.yaml")',
            id="bundle-lock-copied-from-the-project",
        ),
        pytest.param(
            "src/apm_cli/bundle/packer.py",
            '    write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)',
            "    write_text_lf(\n"
            "        bundle_dir / LOCKFILE_NAME, yaml.safe_dump(lockfile.to_dict())\n"
            "    )",
            id="bundle-lock-dict-dump-to-the-name-constant",
        ),
        pytest.param(
            "src/apm_cli/bundle/packer.py",
            '    write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)',
            "    shutil.copy2(get_lockfile_path(project_root), bundle_dir / LOCKFILE_NAME)",
            id="bundle-lock-copied-to-the-name-constant",
        ),
        pytest.param(
            "src/apm_cli/bundle/lockfile_enrichment.py",
            '        data["apm_version"] = installed_apm_version()\n',
            "        pass\n",
            id="bundle-lock-keeps-the-input-stamp",
        ),
    ],
)
def test_bundle_lock_stamp_guard_rejects_other_writers(path: str, old: str, new: str) -> None:
    """#3962: every packed lock names the packer as its generator."""
    rule_id = "contracts-tooling-lockfile-generator-stamp"
    source = _source(path)
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/commands/uninstall/engine.py",
            "def _sync_integrations_after_uninstall(",
            "def _survivor_trust(project_root):\n"
            '    return exec_trust_context_for_project(project_root / "apm.yml", policy=None)\n\n\n'
            "def _sync_integrations_after_uninstall(",
            id="unregistered-global-uninstall-caller",
        ),
        pytest.param(
            "src/apm_cli/security/trust.py",
            "                grants_manifest, policy=None, logger=None, include_user=include_user",
            '                Path(project_root) / "apm.yml",\n'
            "                policy=None,\n"
            "                logger=None,\n"
            "                include_user=include_user,",
            id="registered-caller-derives-apm-yml-from-root",
        ),
    ],
)
def test_trust_owner_guard_rejects_root_derived_callers(path: str, old: str, new: str) -> None:
    """#3954/#3986: a caller that derives apm.yml from a root cannot reach the trust owner."""
    rule_id = "install-deployment-executable-trust-context"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "    servers.update(authored)\n",
            "    del authored\n",
            id="root-manifest-declarations-dropped",
        ),
        pytest.param(
            "        if ClientFactory.project_destination_is_root_mcp_json(str(runtime)) is not False:\n",
            "        if True:\n",
            id="any-runtime-claim-hides-root-entries",
        ),
    ],
)
def test_pack_mcp_reads_only_authored_sources(old: str, new: str) -> None:
    """#3966: pack ships apm.yml declarations; only root-destination claims hide root entries."""
    path = "src/apm_cli/core/plugin_manifest.py"
    rule_id = "marketplace-integrations-pack-plugin-manifests"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/template.py",
            "    trust_ctx = exec_trust_context_for_project(\n        manifest_path,\n",
            "    trust_ctx = exec_trust_context_for_project(\n        project_root,\n",
            id="registered-caller-passes-a-root",
        ),
        pytest.param(
            "src/apm_cli/security/executables.py",
            "            scope_manifest_path(project_root, user_scope=user_scope),\n",
            "            Path(project_root),\n",
            id="registered-caller-passes-a-path-built-from-a-root",
        ),
        pytest.param(
            "src/apm_cli/install/exec_gate.py",
            "            manifest, policy=policy, migrate_user_legacy=False\n",
            "            self._project_root, policy=policy, migrate_user_legacy=False\n",
            id="registered-method-passes-its-root-attribute",
        ),
    ],
)
def test_trust_owner_guard_requires_a_manifest_argument(path: str, old: str, new: str) -> None:
    """A8: a registered caller handing the trust owner a directory fails the guard."""
    rule_id = "install-deployment-executable-trust-context"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    clean = run_selected_rules(ROOT, (rule_id,))
    assert not [v for v in clean.violations if v.rule_id == rule_id]
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id
        and violation.path == path
        and "not a directory" in violation.message
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/integration/mcp_config_view.py",
            "        and name in deployed\n",
            "",
            id="bundle-claim-without-deployment-receipt",
        ),
        pytest.param(
            "src/apm_cli/policy/policy_checks.py",
            "        mcp_deps = list(view.kept_dependencies())\n",
            "        mcp_deps = manifest.get_all_mcp_dependencies()\n",
            id="audit-policy-judges-only-apm-yml-servers",
        ),
        pytest.param(
            "src/apm_cli/policy/policy_checks.py",
            "        mcp_brought_in_by=mcp_brought_in_by,\n",
            "",
            id="audit-policy-drops-transitive-declarers",
        ),
    ],
)
def test_bundle_baseline_guard_rejects_unjudged_or_unreceipted_servers(
    path: str, old: str, new: str
) -> None:
    """#3989: a bundle: claim needs its receipt, and audit policy judges every kept server."""
    rule_id = "mutation_writes.mcp_declaration_scope"
    assert not run_selected_rules(ROOT, (rule_id,)).violations
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/commands/install.py",
            "                    _bundle_project_root,\n                    user_scope=global_,\n",
            "                    _bundle_project_root,\n                    user_scope=False,\n",
            id="global-bundle-install-reads-cwd-grants",
        ),
        pytest.param(
            "src/apm_cli/install/local_bundle_handler.py",
            "    grants_manifest = scope_manifest_path(project_root, user_scope=user_scope)\n",
            "    grants_manifest = manifest_file(project_root)\n",
            id="bundle-grants-skip-the-scope-owner",
        ),
        pytest.param(
            "src/apm_cli/install/local_bundle_handler.py",
            "def _filter_bundle_executables(",
            "def _cwd_allow_map(root):\n"
            "    return effective_exec_map_for_project(root, policy=None)\n\n\n"
            "def _filter_bundle_executables(",
            id="root-wrapper-called-with-a-raw-root",
        ),
    ],
)
def test_bundle_trust_guard_rejects_scope_blind_grants(path: str, old: str, new: str) -> None:
    """#3989: under -g a local bundle reads grants from the user manifest, never the CWD."""
    rule_id = "install-deployment-executable-trust-context"
    assert not [v for v in run_selected_rules(ROOT, (rule_id,)).violations if v.rule_id == rule_id]
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/deps/apm_resolver.py",
            "            refusal = catalog_metadata_refusal(dep_ref, resolved, "
            "self._existing_lockfile)\n",
            "            refusal = None\n",
            id="catalog-metadata-re-read-skips-the-lock-pin",
        ),
        pytest.param(
            "src/apm_cli/drift.py",
            "        and not detect_ref_change(resolved, locked_dep)\n",
            "        and resolved.reference == locked_dep.resolved_ref\n",
            id="pin-comparison-bypasses-the-ref-change-owner",
        ),
        pytest.param(
            "src/apm_cli/install/plan.py",
            "def lockfile_satisfies_manifest(",
            "from apm_cli.drift import MARKETPLACE_CATALOG_METADATA as _METADATA\n\n\n"
            "def lockfile_satisfies_manifest(",
            id="second-catalog-metadata-consumer",
        ),
    ],
)
def test_catalog_metadata_guard_keeps_the_lock_pin(path: str, old: str, new: str) -> None:
    """#3989: a catalog edit cannot silently re-pin a locked server-owning package."""
    rule_id = "marketplace-integrations-locked-evidence"
    assert not run_selected_rules(ROOT, (rule_id,)).violations
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == rule_id and violation.path == path for violation in report.violations
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "        drifted, _unresolved = verify_deployed_file_hashes(project_root, lockfile, targets)\n",
            "        drifted, _unresolved = [], []\n",
            id="plain-audit-skips-bundle-hashes",
        ),
    ],
)
def test_drift_reads_bundle_hashes_through_the_deployed_hash_verifier(old: str, new: str) -> None:
    """#3989 P11: plain audit and --ci judge bundle files through one verifier."""
    path = "src/apm_cli/install/drift.py"
    rule_id = "install-deployment-frozen-mutation-eligibility"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (rule_id,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule_id for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/install/phases/integrate.py",
            "framed=_dep_locked_chk.content_hash_framed",
            "framed=True",
            id="integrate-row-stream",
        ),
        pytest.param(
            "src/apm_cli/install/sources.py",
            "record_package_digests(ctx, dep_key, local_digests)",
            "ctx.package_hashes[dep_key] = local_digests.framed",
            id="second-recorder",
        ),
        pytest.param(
            "src/apm_cli/install/phases/resolve.py",
            "compute_package_hash(candidate_path, framed=True)",
            "compute_package_hash(candidate_path)",
            id="unnamed-stream",
        ),
        pytest.param(
            "src/apm_cli/install/source_helpers.py",
            'framed = records_framed_content_hashes(getattr(ctx, "existing_lockfile", None))\n    ctx',
            (
                'existing = getattr(ctx, "existing_lockfile", None)\n'
                "    framed = existing is None or existing.framed_content_hashes\n    ctx"
            ),
            id="rederived-stream-rule",
        ),
        pytest.param(
            "src/apm_cli/install/phases/content_hash_stream.py",
            "elif recorded_framed and dep.content_hash_framed:",
            "elif dep.content_hash_framed:",
            id="framed-flag-gate",
        ),
    ],
)
def test_package_content_hash_guard_rejects_second_streams_and_hashers(
    path: str, old: str, new: str
) -> None:
    """Hash framing hardening keeps one hasher, one recorder and the row's stream (#4000)."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    rule = "install-deployment-package-content-hash"
    report = run_selected_rules(ROOT, (rule,), source_overrides={path: source.replace(old, new, 1)})
    assert report.failures == ()
    assert any(violation.rule_id == rule for violation in report.violations)
