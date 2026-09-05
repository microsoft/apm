"""Per-subcheck mutation proof for compound install/deployment rules."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.xdist_group(name="architecture_install_compound_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CompoundMutation:
    name: str
    rule_id: str
    path: str
    replacements: tuple[tuple[str, str], ...] = ()
    append: str = ""


def _replace(old: str, new: str) -> tuple[tuple[str, str], ...]:
    return ((old, new),)


BASE_RULE = "install-deployment-base-integrator"
CONTRACTION_RULE = "install-deployment-target-file-contraction"
AUDIT_RULE = "install-deployment-audit-replay"
UNINSTALL_RULE = "install-deployment-uninstall-selection"
REPLACEMENT_RULE = "install-deployment-resolution-replacement"
TARGET_RULE = "install-deployment-package-target-authorization"
REQUEST_DEFAULTS_RULE = "install-deployment-request-defaults"

MUTATIONS: tuple[CompoundMutation, ...] = (
    CompoundMutation(
        "target-owner-ignores-nested-mask",
        TARGET_RULE,
        "src/apm_cli/install/target_filter.py",
        (
            (
                "    declared_targets = canonical_package_targets(package)\n",
                "    declared_targets = tuple(canonical_package_targets(package))\n",
            ),
            (
                "def resolve_effective_package_targets(\n",
                "def _dead_target_owner_mask():\n"
                "    def resolve_effective_package_targets():\n"
                "        declared_targets = canonical_package_targets(package)\n"
                "        effective_targets = consumer_targets\n"
                "    return resolve_effective_package_targets\n"
                "\n"
                "\n"
                "def resolve_effective_package_targets(\n",
            ),
        ),
    ),
    CompoundMutation(
        "target-owner-rejects-competing-declared-assignment",
        TARGET_RULE,
        "src/apm_cli/install/target_filter.py",
        _replace(
            "    declared_targets = canonical_package_targets(package)\n",
            "    declared_targets = canonical_package_targets(package)\n"
            "    declared_targets = tuple(declared_targets)\n",
        ),
    ),
    CompoundMutation(
        "target-owner-rejects-competing-effective-assignment",
        TARGET_RULE,
        "src/apm_cli/install/target_filter.py",
        _replace(
            "\n\n    if (\n        diagnostics is not None",
            "\n    effective_targets = tuple(effective_targets)\n"
            "\n"
            "    if (\n"
            "        diagnostics is not None",
        ),
    ),
    CompoundMutation(
        "target-owner-rejects-dead-canonical-wrapped-live-assignment",
        TARGET_RULE,
        "src/apm_cli/install/target_filter.py",
        _replace(
            "    declared_targets = canonical_package_targets(package)\n",
            "    if False:\n"
            "        declared_targets = canonical_package_targets(package)\n"
            "    declared_targets = tuple(canonical_package_targets(package))\n",
        ),
    ),
    CompoundMutation(
        "target-owner-rejects-shadowed-canonical-package-helper",
        TARGET_RULE,
        "src/apm_cli/install/target_filter.py",
        _replace(
            "from apm_cli.models.apm_package import canonical_package_targets\n",
            "from apm_cli.models.apm_package import canonical_package_targets\n"
            'canonical_package_targets = lambda package: ("all",)\n',
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-rogue-resolver-import",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        _replace(
            "from .target_filter import resolve_effective_package_targets\n",
            "from .rogue import resolve_effective_package_targets\n",
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-dead-canonical-evidence-for-rogue-binding",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        _replace(
            "    target_selection = resolve_effective_package_targets(\n",
            "    target_selection = rogue_resolve_effective_package_targets()\n"
            "    if False:\n"
            "        target_selection = resolve_effective_package_targets(\n",
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-harmless-evidence-before-rogue-dispatch",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        (
            (
                "    targets = list(target_selection.targets)\n",
                "    targets = list(target_selection.targets)\n"
                "    for _authorized_target in target_selection.targets:\n"
                "        pass\n",
            ),
            ("        for _target in targets:\n", "        for _target in rogue_targets:\n"),
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-unrelated-getattr-sink",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        (
            (
                "        for _target in targets:\n",
                "        for _target in targets:\n"
                '            getattr(observer, "record")(_target)\n',
            ),
            (
                "            _int_result = getattr(_integrator, _entry.integrate_method)(\n"
                "                _target,\n",
                "            _int_result = getattr(_integrator, _entry.integrate_method)(\n"
                "                rogue_target,\n",
            ),
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-false-and-dispatch",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        _replace(
            "            _int_result = getattr(_integrator, _entry.integrate_method)(\n",
            "            _int_result = False and getattr(_integrator, _entry.integrate_method)(\n",
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-additional-rogue-dispatch",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        _replace(
            '            result["links_resolved"] += _int_result.links_resolved\n',
            "            getattr(_integrator, _entry.integrate_method)(rogue_target)\n"
            '            result["links_resolved"] += _int_result.links_resolved\n',
        ),
    ),
    CompoundMutation(
        "target-consumer-rejects-in-place-target-expansion",
        TARGET_RULE,
        "src/apm_cli/install/services.py",
        _replace(
            "    targets = list(target_selection.targets)\n",
            "    targets = list(target_selection.targets)\n"
            "    targets.append(target_selection.excluded_targets[0])\n",
        ),
    ),
    CompoundMutation(
        "target-hook-rejects-harmless-evidence-before-rogue-dispatch",
        TARGET_RULE,
        "src/apm_cli/integration/hook_integrator.py",
        (
            (
                "            rebuild_plan.append((dep_ref, pkg_info, target_selection, source_plan))\n",
                "            rebuild_plan.append((dep_ref, pkg_info, target_selection, source_plan))\n"
                "            for _authorized_target in target_selection.targets:\n"
                "                pass\n",
            ),
            (
                "                for target in target_selection.targets:\n",
                "                for target in rogue_targets:\n",
            ),
        ),
    ),
    CompoundMutation(
        "target-hook-rejects-if-zero-canonical-sink",
        TARGET_RULE,
        "src/apm_cli/integration/hook_integrator.py",
        (
            (
                "                for target in target_selection.targets:\n",
                "                for target in target_selection.targets:\n"
                "                    if []:\n"
                "                        self.integrate_hooks_for_target(\n"
                "                            target, pkg_info, project_root\n"
                "                        )\n",
            ),
            (
                "                    self.integrate_hooks_for_target(\n"
                "                        target,\n",
                "                    self.integrate_hooks_for_target(\n"
                "                        rogue_target,\n",
            ),
        ),
    ),
    CompoundMutation(
        "target-hook-rejects-additional-rogue-dispatch",
        TARGET_RULE,
        "src/apm_cli/integration/hook_integrator.py",
        _replace(
            "                        source_plan=source_plan,\n                    )\n",
            "                        source_plan=source_plan,\n"
            "                    )\n"
            "                    self.integrate_hooks_for_target(\n"
            "                        rogue_target, pkg_info, project_root\n"
            "                    )\n",
        ),
    ),
    CompoundMutation(
        "target-hook-rejects-in-place-selection-expansion",
        TARGET_RULE,
        "src/apm_cli/integration/hook_integrator.py",
        _replace(
            "                dep_ref.get_identity(),\n"
            "            )\n"
            "            source_plan = build_hook_reintegration_source_plan(\n",
            "                dep_ref.get_identity(),\n"
            "            )\n"
            '            object.__setattr__(target_selection, "targets", rogue_targets)\n'
            "            source_plan = build_hook_reintegration_source_plan(\n",
        ),
    ),
    CompoundMutation(
        "target-uninstall-rejects-authorized-collection-not-used-by-dispatch",
        TARGET_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "                targets=authorized_targets,\n",
            "                targets=rogue_targets,\n",
        ),
    ),
    CompoundMutation(
        "target-uninstall-rejects-if-zero-canonical-sink",
        TARGET_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        (
            (
                "        try:\n            integration_result = integrate_package_primitives(\n",
                "        try:\n"
                "            if 0:\n"
                "                integrate_package_primitives(targets=authorized_targets)\n"
                "            integration_result = integrate_package_primitives(\n",
            ),
            (
                "                targets=authorized_targets,\n",
                "                targets=rogue_targets,\n",
            ),
        ),
    ),
    CompoundMutation(
        "target-uninstall-rejects-additional-rogue-dispatch",
        TARGET_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "        try:\n            integration_result = integrate_package_primitives(\n",
            "        try:\n"
            "            integrate_package_primitives(targets=rogue_targets)\n"
            "            integration_result = integrate_package_primitives(\n",
        ),
    ),
    CompoundMutation(
        "target-uninstall-rejects-in-place-target-expansion",
        TARGET_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "            authorized_targets.append(target)\n",
            "            authorized_targets.append(target)\n"
            "            authorized_targets.append(target_selection.excluded_targets[0])\n",
        ),
    ),
    CompoundMutation(
        "base-class",
        BASE_RULE,
        "src/apm_cli/integration/base_integrator.py",
        _replace("class BaseIntegrator:", "class BaseIntegratorDisabled:"),
    ),
    *(
        CompoundMutation(
            f"base-method-{method}",
            BASE_RULE,
            "src/apm_cli/integration/base_integrator.py",
            _replace(f"    def {method}(", f"    def {method}_disabled("),
        )
        for method in (
            "check_collision",
            "sync_remove_files",
            "cleanup_empty_parents",
            "validate_deploy_path",
        )
    ),
    CompoundMutation(
        "contraction-owner-definition",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        _replace(
            "def reconcile_target_deployed_files(",
            "def reconcile_target_deployed_files_disabled(",
        ),
    ),
    CompoundMutation(
        "contraction-state-delegation",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        _replace(
            "    changed = reconcile_target_deployed_files(\n        project_root=project_root,",
            "    changed = reconcile_target_deployed_files_disabled(\n"
            "        project_root=project_root,",
        ),
    ),
    CompoundMutation(
        "contraction-cleanup-delegation",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        (
            (
                "        files, hashes = reconcile_deployed_block(",
                "        files, hashes = reconcile_deployed_block_disabled(",
            ),
            (
                "        local_files, local_hashes = reconcile_deployed_block(",
                "        local_files, local_hashes = reconcile_deployed_block_disabled(",
            ),
        ),
    ),
    CompoundMutation(
        "contraction-delete-chokepoint",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        _replace(
            "    cleanup = remove_stale_deployed_files(",
            "    cleanup = remove_stale_deployed_files_disabled(",
        ),
    ),
    CompoundMutation(
        "contraction-lockfile-direct-delete",
        CONTRACTION_RULE,
        "src/apm_cli/install/phases/lockfile.py",
        _replace(
            "        if lockfile is None:\n            return",
            "        remove_stale_deployed_files(set(), self.ctx.project_root)\n"
            "        if lockfile is None:\n"
            "            return",
        ),
    ),
    CompoundMutation(
        "contraction-lockfile-route",
        CONTRACTION_RULE,
        "src/apm_cli/install/phases/lockfile.py",
        _replace(
            "        changed = reconcile_target_deployed_files(",
            "        changed = reconcile_target_deployed_files_disabled(",
        ),
    ),
    CompoundMutation(
        "contraction-post-local-direct-delete",
        CONTRACTION_RULE,
        "src/apm_cli/install/phases/post_deps_local.py",
        _replace(
            "    _files, _hashes = reconcile_deployed_block(",
            "    remove_stale_deployed_files(set(), ctx.project_root)\n"
            "    _files, _hashes = reconcile_deployed_block(",
        ),
    ),
    CompoundMutation(
        "contraction-post-local-route",
        CONTRACTION_RULE,
        "src/apm_cli/install/phases/post_deps_local.py",
        _replace(
            "    _files, _hashes = reconcile_deployed_block(",
            "    _files, _hashes = reconcile_deployed_block_disabled(",
        ),
    ),
    CompoundMutation(
        "contraction-uninstall-route",
        CONTRACTION_RULE,
        "src/apm_cli/commands/uninstall/cli.py",
        _replace(
            "            reconcile_target_deployed_files(",
            "            reconcile_target_deployed_files_disabled(",
        ),
    ),
    CompoundMutation(
        "contraction-shared-reconciler",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        _replace(
            "    ).reconcile(\n        DeploymentLedger(records=prior_records),",
            "    ).reconcile_disabled(\n        DeploymentLedger(records=prior_records),",
        ),
    ),
    CompoundMutation(
        "contraction-generic-row-supersession",
        CONTRACTION_RULE,
        "src/apm_cli/install/manifest_reconcile.py",
        append=(
            "\n\ndef _rogue_generic_row_supersession(rows):\n"
            "    for record in rows.records.values():\n"
            "        if record.locator.target and record.locator.value:\n"
            "            return True\n"
            "    return False\n"
        ),
    ),
    CompoundMutation(
        "audit-replay-integration",
        AUDIT_RULE,
        "src/apm_cli/install/drift.py",
        _replace("integrate_package_primitives(", "integrate_package_primitives_disabled("),
    ),
    CompoundMutation(
        "audit-replay-subset-keyword",
        AUDIT_RULE,
        "src/apm_cli/install/drift.py",
        _replace(
            "skill_subset=tuple(package_info.dependency_ref.skill_subset or ()) or None,",
            "subset=tuple(package_info.dependency_ref.skill_subset or ()) or None,",
        ),
    ),
    CompoundMutation(
        "audit-replay-subset-source",
        AUDIT_RULE,
        "src/apm_cli/install/drift.py",
        _replace(
            "package_info.dependency_ref.skill_subset",
            "package_info.dependency_ref.skills",
        ),
    ),
    CompoundMutation(
        "audit-replay-owner",
        AUDIT_RULE,
        "src/apm_cli/install/audit_replay.py",
        _replace("def prepare_ci_audit_replay(", "def prepare_ci_audit_replay_disabled("),
    ),
    CompoundMutation(
        "audit-replay-command-delegation",
        AUDIT_RULE,
        "src/apm_cli/commands/audit.py",
        (
            (
                "prepared_replay = prepare_ci_audit_replay(",
                "prepared_replay = prepare_ci_audit_replay_disabled(",
            ),
        ),
    ),
    CompoundMutation(
        "audit-replay-command-direct-replay",
        AUDIT_RULE,
        "src/apm_cli/commands/audit.py",
        _replace(
            "                    prepared_replay = prepare_ci_audit_replay(",
            "                    run_replay(None, None)\n"
            "                    prepared_replay = prepare_ci_audit_replay(",
        ),
    ),
    CompoundMutation(
        "audit-replay-config-root",
        AUDIT_RULE,
        "src/apm_cli/policy/ci_checks.py",
        _replace("prepared_replay.modules_root", "prepared_replay.project_root"),
    ),
    CompoundMutation(
        "uninstall-select-owner",
        UNINSTALL_RULE,
        "src/apm_cli/models/dependency/selection.py",
        _replace("def select_manifest_dependency(", "def select_manifest_dependency_disabled("),
    ),
    CompoundMutation(
        "uninstall-parse-owner",
        UNINSTALL_RULE,
        "src/apm_cli/models/dependency/selection.py",
        _replace("def parse_dependency_entry(", "def parse_dependency_entry_disabled("),
    ),
    CompoundMutation(
        "uninstall-owner-delegation",
        UNINSTALL_RULE,
        "src/apm_cli/models/dependency/selection.py",
        _replace(
            "            dependency = parse_dependency_entry(entry)",
            "            dependency = DependencyReference.parse_from_dict(entry)",
        ),
    ),
    CompoundMutation(
        "uninstall-engine-delegation",
        UNINSTALL_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "        selection = select_manifest_dependency(",
            "        selection = select_manifest_dependency_disabled(",
        ),
    ),
    CompoundMutation(
        "uninstall-duplicate-owner",
        UNINSTALL_RULE,
        "src/apm_cli/commands/uninstall/cli.py",
        append="\n\ndef parse_dependency_entry(entry):\n    return entry\n",
    ),
    CompoundMutation(
        "uninstall-duplicate-validator",
        UNINSTALL_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        append=("\n\ndef _validate_uninstall_packages(*args, **kwargs):\n    return [], []\n"),
    ),
    CompoundMutation(
        "uninstall-dependency-iteration",
        UNINSTALL_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "    packages_to_remove = []",
            "    for dep_entry in current_deps:\n        pass\n    packages_to_remove = []",
        ),
    ),
    CompoundMutation(
        "uninstall-identity-bypass",
        UNINSTALL_RULE,
        "src/apm_cli/commands/uninstall/engine.py",
        _replace(
            "    packages_to_remove = []",
            "    current_deps.get_identity()\n    packages_to_remove = []",
        ),
    ),
    *(
        CompoundMutation(
            f"replacement-owner-{method}",
            REPLACEMENT_RULE,
            "src/apm_cli/install/resolution_staging.py",
            _replace(f"    def {method}(", f"    def {method}_disabled("),
        )
        for method in (
            "prepare_replacement",
            "publish_replacement",
            "discard_replacement",
        )
    ),
    CompoundMutation(
        "replacement-duplicate-owner",
        REPLACEMENT_RULE,
        "src/apm_cli/install/phases/resolve.py",
        append="\n\ndef discard_replacement(path):\n    return path\n",
    ),
    CompoundMutation(
        "replacement-prepare-route",
        REPLACEMENT_RULE,
        "src/apm_cli/install/phases/resolve.py",
        _replace(
            "staging_session.prepare_replacement(install_path)",
            "staging_session.reserve_replacement(install_path)",
        ),
    ),
    CompoundMutation(
        "replacement-eager-live-path",
        REPLACEMENT_RULE,
        "src/apm_cli/install/phases/resolve.py",
        _replace(
            "staging_session.prepare_replacement(install_path)",
            "staging_session.prepare_path(install_path)",
        ),
    ),
    CompoundMutation(
        "replacement-activation-route",
        REPLACEMENT_RULE,
        "src/apm_cli/install/phases/resolve.py",
        _replace("_activate_validated_candidate,", "_activate_unvalidated_candidate,"),
    ),
)


def _mutate(case: CompoundMutation) -> str:
    source = (ROOT / case.path).read_text(encoding="utf-8")
    for old, new in case.replacements:
        assert source.count(old) >= 1, f"{case.name}: missing {old!r}"
        source = source.replace(old, new, 1)
    source += case.append
    ast.parse(source, filename=case.path)
    return source


@pytest.mark.parametrize("case", MUTATIONS, ids=[case.name for case in MUTATIONS])
def test_each_install_compound_subcheck_has_mutation_proof(
    case: CompoundMutation,
) -> None:
    report = run_selected_rules(
        ROOT,
        (case.rule_id,),
        source_overrides={case.path: _mutate(case)},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert any(violation.rule_id == case.rule_id for violation in report.violations)


def test_request_defaults_accepts_wrapper_without_positional_defaults() -> None:
    """A zero-default wrapper has no trailing defaulted positional arguments."""
    path = "src/apm_cli/commands/install.py"
    source = """
def _install_apm_dependencies(context, package):
    request = InstallRequest()
    return request
""".lstrip()

    report = run_selected_rules(
        ROOT,
        (REQUEST_DEFAULTS_RULE,),
        source_overrides={path: source},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert report.violations == ()
