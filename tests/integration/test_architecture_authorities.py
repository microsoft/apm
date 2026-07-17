"""Integration guardrails for canonical architecture authorities."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


def test_hook_rewrite_scope_has_single_owner() -> None:
    """Native hook paths must consume HookIntegrator's scope decision."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/integration/hook_integrator.py").read_text()
    kiro = (root / "src/apm_cli/integration/kiro_hook_integrator.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert owner.count("def _deploy_root_for_hook_rewrite(") == 1
    assert owner.count("self._deploy_root_for_hook_rewrite(") == 2
    assert "integrator._deploy_root_for_hook_rewrite(project_root, user_scope)" in kiro
    assert "Hook rewrite scope must route through HookIntegrator" in guard


def test_hook_rewrite_scope_guard_rejects_parallel_decision(tmp_path: Path) -> None:
    """The boundary lint must reject scope decisions outside HookIntegrator."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    kiro_path = sandbox / "src/apm_cli/integration/kiro_hook_integrator.py"
    kiro_source = kiro_path.read_text(encoding="utf-8")
    kiro_path.write_text(
        kiro_source.replace(
            "integrator._deploy_root_for_hook_rewrite(project_root, user_scope)",
            "project_root if user_scope else None",
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Hook rewrite scope must route through HookIntegrator" in result.stdout


def test_policy_resolution_failure_outcomes_have_single_owner() -> None:
    """Approval fallback outcomes must come from policy outcome routing."""
    from apm_cli.policy.outcome_routing import POLICY_RESOLUTION_FAILURE_OUTCOMES

    root = Path(__file__).parents[2]
    approve_source = (root / "src/apm_cli/commands/approve.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    expected = {
        "cache_miss_fetch_fail",
        "garbage_response",
        "hash_mismatch",
        "incomplete_chain",
        "malformed",
    }

    assert frozenset(expected) == POLICY_RESOLUTION_FAILURE_OUTCOMES
    assert (
        "from ..policy.outcome_routing import POLICY_RESOLUTION_FAILURE_OUTCOMES" in approve_source
    )
    assert not any(f'"{outcome}"' in approve_source for outcome in expected)
    assert "Approval fallback outcomes must use policy/outcome_routing.py" in guard


def test_object_git_dependency_fields_have_single_owner() -> None:
    """Fixture authoring must consume the product parser's field vocabulary."""
    root = Path(__file__).parents[2]
    object_fields = (root / "src/apm_cli/models/dependency/object_fields.py").read_text()
    parser = (root / "src/apm_cli/models/dependency/reference.py").read_text()
    fixture = (root / "tests/utils/local_package.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "def reject_unknown_git_fields" in object_fields
    assert "reject_unknown_git_fields(entry, parent=True)" in parser
    assert "reject_unknown_git_fields(entry, parent=False)" in parser
    assert "reject_unknown_fields" not in fixture
    assert "_GIT_DEPENDENCY_FIELDS" not in fixture
    assert "Object-form Git dependency fields must come from the product parser" in guard


@pytest.mark.lifecycle_smoke
def test_ado_lock_coordinates_have_single_owner() -> None:
    """AC14 derives ADO coordinates without provider-specific lock fields."""
    import inspect

    from apm_cli.deps.lockfile import LockedDependency
    from apm_cli.models.dependency.reference import DependencyReference

    root = Path(__file__).parents[2]
    lockfile_source = (root / "src/apm_cli/deps/lockfile.py").read_text()
    ref_resolver_source = (root / "src/apm_cli/marketplace/ref_resolver.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    reconstruction = inspect.getsource(LockedDependency.to_dependency_ref)

    assert hasattr(DependencyReference, "canonical_ado_coordinates")
    assert hasattr(DependencyReference, "with_derived_provider_coordinates")
    assert "with_derived_provider_coordinates" in reconstruction
    assert "ado_organization" not in lockfile_source
    assert "ado_project" not in lockfile_source
    assert "ado_repo" not in lockfile_source
    assert "DependencyReference.canonical_ado_coordinates" in ref_resolver_source
    assert "repo_url.split" not in reconstruction
    assert "owner_repo.split" not in ref_resolver_source
    assert "AC14: ADO lock-coordinate authority" in guard
    assert "ADO coordinates must be derived by DependencyReference, never persisted" in guard


def test_packed_marketplace_source_parsing_has_single_owner() -> None:
    """Packed marketplace URL/ref/path parsing must use DependencyReference."""
    root = Path(__file__).parents[2]
    resolver = (root / "src/apm_cli/marketplace/resolver.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    helper = resolver.split(
        "def _dependency_reference_from_packed_source(",
        maxsplit=1,
    )[1].split("\ndef ", maxsplit=1)[0]
    assert "DependencyReference.parse_from_dict(entry)" in helper
    assert "Packed marketplace sources must use DependencyReference.parse_from_dict" in guard


def test_packed_marketplace_source_owner_guard_rejects_parallel_parser(
    tmp_path: Path,
) -> None:
    """AC10 must reject bypassing the canonical dependency parser."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    resolver_path = sandbox / "src/apm_cli/marketplace/resolver.py"
    resolver_source = resolver_path.read_text(encoding="utf-8")
    resolver_path.write_text(
        resolver_source.replace(
            "dependency = DependencyReference.parse_from_dict(entry)",
            "dependency = DependencyReference(repo_url=remote.strip())",
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Packed marketplace sources must use DependencyReference.parse_from_dict" in (
        result.stdout
    )


def test_cleanup_current_claim_protection_has_single_owner() -> None:
    """Cleanup must route current deployed-file claims through the reconciler."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/core/deployment_state.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    checker = _load_cleanup_claim_owner_checker(root)

    assert "def current_claimed_paths" in owner
    assert checker.analyze_path(root / "src/apm_cli/install/phases/cleanup.py") == []
    assert "scripts/check_cleanup_claim_owner.py" in guard
    assert "Cleanup current-claim protection must use DeploymentReconciler" in guard


def test_deployment_owner_reconciliation_has_single_owner() -> None:
    """Prune and audit must consume canonical owner and cleanup decisions."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    checker_path = root / "scripts/check_deployment_owner_boundaries.py"

    assert checker_path.is_file()
    assert "scripts/check_deployment_owner_boundaries.py" in guard
    assert "Deployment ownership must route through DeploymentLedgerCodec" in guard


def test_shared_target_contraction_has_single_reconciler_owner() -> None:
    """Generic shared-root supersession must remain inside DeploymentReconciler."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/core/deployment_state.py").read_text()
    consumer = (root / "src/apm_cli/install/manifest_reconcile.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    checker = _load_shared_target_contraction_owner_checker(root)

    assert "def _superseding_generic_proofs" in owner
    assert "generic_governed_values" in owner
    assert "DeploymentReconciler(" in consumer
    assert checker.analyze_path(root / "src/apm_cli/install/manifest_reconcile.py") == []
    assert "Shared target contraction must use DeploymentReconciler" in guard


def test_shared_target_contraction_guard_rejects_missing_reconciler_delegation(
    tmp_path: Path,
) -> None:
    """The boundary guard rejects a consumer that bypasses canonical reconciliation."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    consumer_path = sandbox / "src/apm_cli/install/manifest_reconcile.py"
    source = consumer_path.read_text(encoding="utf-8")
    consumer_path.write_text(
        source.replace(").reconcile(", ").reconcile_without_owner(", 1),
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Shared target contraction must use DeploymentReconciler" in result.stdout


def test_local_bundle_replay_provenance_has_single_owner() -> None:
    """Bundle persistence and drift exclusion must consume the deployment ledger."""
    root = Path(__file__).parents[2]
    handler = (root / "src/apm_cli/install/local_bundle_handler.py").read_text()
    drift = (root / "src/apm_cli/install/drift.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "DeploymentLedgerCodec.record_local_bundle_files" in handler
    assert "DeploymentLedgerCodec.local_bundle_paths" in drift
    assert "Local-bundle replay provenance must route through DeploymentLedgerCodec" in guard


def test_ac13_git_ref_transport_selection_has_single_owner() -> None:
    """AC13 makes Git ref enumeration consume canonical transport selection."""
    root = Path(__file__).parents[2]
    ref_reuse = (root / "src/apm_cli/install/helpers/ref_reuse.py").read_text()
    ref_resolver = (root / "src/apm_cli/marketplace/ref_resolver.py").read_text()
    git_ref_resolver = (root / "src/apm_cli/deps/git_reference_resolver.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "transport_plan = transport_selector.select(" in ref_reuse
    assert "transport_scheme=transport_scheme" in ref_reuse
    assert "transport_plan = host._transport_selector.select(" in git_ref_resolver
    assert "build_ssh_url(" in ref_resolver
    assert "from apm_cli.deps.transport_selection import" not in ref_resolver
    assert "TransportSelector(" not in ref_resolver
    assert "AC13: Git ref transport selection authority" in guard
    assert "Git ref transport must route through TransportSelector into RefResolver" in guard


def test_local_bundle_policy_uses_shared_preflight_owner() -> None:
    """Imperative bundle deploys must not bypass policy outcome routing."""
    root = Path(__file__).parents[2]
    handler = (root / "src/apm_cli/install/local_bundle_handler.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    assert "from ..policy.install_preflight import run_policy_preflight" in handler
    assert "policy_fetch, _enforcement_active = run_policy_preflight(" in handler
    assert "cache_only=True" in handler
    assert "mcp_deps=bundle_mcp_deps" in handler
    assert "require_hashes_enabled(" in handler
    assert "Local bundle installs must route policy through install_preflight.py" in guard
    assert "require_hashes enforcement must route through install/integrity.py" in guard


def test_hook_file_routing_dep_targets_gate_has_static_guard() -> None:
    """Per-file hook routing must compose with dependency target filtering."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    assert "Per-file hook routing must not be gated by dep_targets_active" in guard
    assert "scripts/check_hook_file_routing_owner.py" in guard


def test_hook_file_routing_guard_rejects_dep_targets_gate(tmp_path: Path) -> None:
    """AC6 must reject restoring the dependency-target bypass."""
    root = Path(__file__).parents[2]
    hook_integrator = tmp_path / "hook_integrator.py"
    hook_integrator.write_text(
        (root / "src/apm_cli/integration/hook_integrator.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with hook_integrator.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n\ndef _architecture_test_dep_target_gate() -> None:\n"
            "    if dep_targets_active is False:\n"
            "        _filter_hook_files_for_target([])\n"
        )

    result = subprocess.run(
        (sys.executable, "scripts/check_hook_file_routing_owner.py", str(hook_integrator)),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "dep_targets_active gates _filter_hook_files_for_target" in result.stdout


def test_local_bundle_owner_guard_rejects_parallel_marker_interpretation(
    tmp_path: Path,
) -> None:
    """AC4 must reject a consumer that interprets the persisted marker itself."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    drift_path = sandbox / "src/apm_cli/install/drift.py"
    with drift_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n\ndef _parallel_bundle_owner(record):\n"
            '    return record.active_owner != "local-bundle"\n'
        )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Local-bundle replay provenance must route through DeploymentLedgerCodec" in (
        result.stdout
    )


def _load_cleanup_claim_owner_checker(root: Path) -> ModuleType:
    """Import the semantic cleanup claim-authority checker."""
    module_name = "check_cleanup_claim_owner"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_shared_target_contraction_owner_checker(root: Path) -> ModuleType:
    """Import the semantic generic deployment-row owner checker."""
    module_name = "check_shared_target_contraction_owner"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_target_instruction_contraction_owner_checker(root: Path) -> ModuleType:
    """Import the target-specific instruction contraction owner checker."""
    module_name = "check_target_instruction_contraction_owner"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_skill_subset_owner_checker() -> ModuleType:
    """Import scripts/check_skill_subset_owner.py as a standalone module.

    The AST checker is the single detection owner for the semantic
    renamed-helper case (see tests/unit/scripts/test_check_skill_subset_owner.py
    for its own unit coverage); this integration test reuses it rather than
    re-implementing any part of its algorithm.
    """
    root = Path(__file__).parents[2]
    script_path = root / "scripts" / "check_skill_subset_owner.py"
    spec = importlib.util.spec_from_file_location("check_skill_subset_owner", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_windows_stable_path_checker(root: Path) -> ModuleType:
    """Import scripts/check_windows_stable_path_owner.py as a module.

    This is the single scan owner for the Windows stable executable
    path boundary (owner presence + duplicate-derivation detection).
    Both this test and scripts/lint-architecture-boundaries.sh (AC8)
    consume it directly instead of re-implementing its regexes, globs,
    or exemption handling.
    """
    module_name = "check_windows_stable_path_owner"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_test_contract_checker(root: Path) -> ModuleType:
    """Import the single scanner for executable test contract owners."""
    module_name = "check_test_contract_authorities"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_diagnostic_ascii_owner_checker(root: Path) -> ModuleType:
    """Import the printable agent-diagnostic authority checker."""
    module_name = "check_diagnostic_ascii_owner"
    script_path = root / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_plural_targets_drive_bundle_filtering(tmp_path: Path) -> None:
    """The canonical manifest target list must control bundle packing."""
    from apm_cli.bundle.packer import pack_bundle
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    (tmp_path / "apm.yml").write_text(
        "name: target-authority\nversion: 1.0.0\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    claude_file = ".claude/commands/keep.md"
    copilot_file = ".github/prompts/drop.prompt.md"
    for relative in (claude_file, copilot_file):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")
    lockfile = LockFile(
        dependencies={
            "owner/dep": LockedDependency(
                repo_url="https://github.com/owner/dep",
                deployed_files=[claude_file, copilot_file],
            )
        }
    )
    (tmp_path / "apm.lock.yaml").write_text(lockfile.to_yaml(), encoding="utf-8")

    result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True)

    assert result.files == [claude_file]


def test_target_catalog_matches_native_profiles() -> None:
    """Every deployable target capability must have one native profile."""
    from apm_cli.core.target_catalog import TARGET_CAPABILITIES
    from apm_cli.integration.targets import KNOWN_TARGETS

    expected = {
        capability.name
        for capability in TARGET_CAPABILITIES.values()
        if capability.primitive_profile is not None and not capability.mcp_only
    }
    assert set(KNOWN_TARGETS) == expected


@pytest.mark.parametrize(
    ("target_flag", "expected_targets"),
    (
        ("claude,copilot", ["claude", "copilot"]),
        ("agents", ["copilot"]),
    ),
)
def test_init_persists_only_install_accepted_catalog_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_flag: str,
    expected_targets: list[str],
) -> None:
    """Every target accepted by init must produce an installable manifest."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.utils.yaml_io import load_yaml

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    runner = CliRunner()

    initialized = runner.invoke(cli, ["init", "--yes", "--target", target_flag])

    assert initialized.exit_code == 0, initialized.output
    manifest = load_yaml(tmp_path / "apm.yml")
    assert manifest["targets"] == expected_targets
    assert APMPackage.from_apm_yml(tmp_path / "apm.yml").canonical_targets == tuple(
        expected_targets
    )

    installed = runner.invoke(cli, ["install"])
    assert installed.exit_code == 0, installed.output


def test_host_provider_registry_drives_auth_and_backends() -> None:
    """Auth classification and native backends must cover one provider set."""
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.host_providers import (
        HOST_PROVIDERS,
        host_backend_factory,
    )

    samples = {
        "github": ("github.com", None),
        "ghe_cloud": ("tenant.ghe.com", None),
        "ado": ("dev.azure.com", None),
        "gitlab": ("code.example.test", "gitlab"),
        "generic": ("git.example.test", None),
    }
    for kind, (host, host_type) in samples.items():
        info = AuthResolver.classify_host(host, host_type=host_type)
        assert info.kind == kind
        assert host_backend_factory(kind)(host_info=info).kind == kind
    assert set(samples).issubset(HOST_PROVIDERS)


def test_host_type_hint_cannot_override_recognized_provider() -> None:
    """Manifest hints must not redirect credentials across known hosts."""
    from apm_cli.core.auth import AuthResolver

    for host in ("github.com", "tenant.ghe.com", "dev.azure.com"):
        try:
            AuthResolver.classify_host(host, host_type="gitlab")
        except ValueError as exc:
            assert "conflicts" in str(exc)
        else:
            raise AssertionError(f"host type override unexpectedly accepted for {host}")


def test_runtime_registry_drives_factory_manager_cli_and_runner() -> None:
    """Every runtime consumer must project the canonical descriptors."""
    from apm_cli.commands.runtime import setup
    from apm_cli.core.script_runner import ScriptRunner
    from apm_cli.runtime.factory import RuntimeFactory
    from apm_cli.runtime.manager import RuntimeManager
    from apm_cli.runtime.registry import adapter_descriptors, runtime_names

    names = runtime_names()
    manager = RuntimeManager()
    runtime_argument = next(param for param in setup.params if param.name == "runtime_name")
    cli_choices = tuple(runtime_argument.type.choices)
    adapter_classes = tuple(
        descriptor.adapter for descriptor in adapter_descriptors() if descriptor.adapter is not None
    )

    assert tuple(manager.supported_runtimes) == names
    assert manager.get_runtime_preference() == list(names)
    assert set(cli_choices) == set(names)
    assert RuntimeFactory.adapter_classes() == adapter_classes
    runner = ScriptRunner()
    assert all(runner._detect_runtime(f"{name} run") == name for name in names)


def test_target_profile_owns_external_locator_encoding(tmp_path: Path) -> None:
    """Install helpers must use target locator metadata without name branches."""
    from apm_cli.install.deployed_paths import deployed_path_entry
    from apm_cli.install.manifest_reconcile import install_governance
    from apm_cli.integration.targets import KNOWN_TARGETS

    deploy_root = tmp_path / "OneDrive" / "Documents" / "Cowork" / "skills"
    target = replace(
        KNOWN_TARGETS["copilot-cowork"],
        resolved_deploy_root=deploy_root,
    )
    deployed = deploy_root / "demo" / "SKILL.md"

    assert (
        deployed_path_entry(deployed, tmp_path / "project", [target])
        == "cowork://skills/demo/SKILL.md"
    )
    _, schemes = install_governance([target])
    assert schemes == {"cowork://"}


def test_lockfile_builder_delegates_package_claim_policy() -> None:
    """Lockfile assembly must consume the deployment owner's decision."""
    root = Path(__file__).parents[2]
    source = (root / "src/apm_cli/install/phases/lockfile.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "DeploymentReconciler.reconcile_package_claims" in source
    assert "Deployment claim handoff belongs to DeploymentReconciler" in guard
    for duplicate in (
        "def reconcile_cross_package_deployed_files",
        "all_current_deployed",
        "other_current",
    ):
        assert duplicate not in source


def test_target_instruction_contraction_uses_manifest_reconciliation() -> None:
    """Install lifecycle routing must not own target-file deletion itself."""
    root = Path(__file__).parents[2]
    checker = _load_target_instruction_contraction_owner_checker(root)
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    architecture = (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )

    assert checker.analyze_paths(root) == []
    assert "AC15a: target-specific instruction contraction authority" in guard
    assert (
        "Target-specific instruction contraction must route through manifest_reconcile.py" in guard
    )
    assert "Target-scoped deployed-file contraction" in architecture


def test_dependency_winner_selection_has_one_algorithm() -> None:
    """Dispatch and flattening must consume one deterministic selector."""
    root = Path(__file__).parents[2]
    source = (root / "src/apm_cli/deps/apm_resolver.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert source.count("_select_dependency_winners(") == 3
    assert "Dependency ref winner selection must use one helper" in guard
    for duplicate in (
        "download_winners",
        "level_winners",
        "seen_keys",
        "nodes_at_depth.sort",
    ):
        assert duplicate not in source


def test_existing_path_ref_rechecks_have_one_owner() -> None:
    """Resolver gates must share the canonical ref-drift decision."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/drift.py").read_text()
    resolver = (root / "src/apm_cli/deps/apm_resolver.py").read_text()
    phase = (root / "src/apm_cli/install/phases/resolve.py").read_text()
    legacy_test = (root / "tests/unit/test_install_update_refs.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "def should_force_ref_recheck(" in owner
    assert "should_force_ref_recheck(" in resolver
    assert "should_force_ref_recheck(" in phase
    assert "_force_semver_resolve" not in resolver
    assert "_force_semver_resolve" not in phase
    assert "def _force_semver_resolve" not in legacy_test
    assert "Existing-path ref rechecks must use drift.py::should_force_ref_recheck" in guard


def test_skill_subset_filtering_has_one_canonical_owner() -> None:
    """Install and pack must share one flattened skill-subset matcher."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/models/dependency/subsets.py").read_text()
    integrator = (root / "src/apm_cli/integration/skill_integrator.py").read_text()
    exporter = (root / "src/apm_cli/bundle/plugin_exporter.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "def skill_subset_filter_tokens(" in owner
    assert "skill_subset_filter_tokens(skill_subset)" in integrator
    assert "skill_subset_filter_tokens(dep.skill_subset)" in exporter
    assert "Skill subset filter tokens must come from models/dependency/subsets.py" in guard
    assert "def _skill_subset_name_filter" not in integrator


def test_cached_update_resolution_stays_with_downloader_owner() -> None:
    """Cached branch planning must reuse the production ref resolver."""
    root = Path(__file__).parents[2]
    ref_reuse = (root / "src/apm_cli/install/helpers/ref_reuse.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "resolved = downloader.resolve_git_reference(dep_ref)" in ref_reuse
    assert "Cached update planning must resolve refs through the downloader owner" in guard


def test_claude_skill_lock_metadata_has_one_canonical_owner() -> None:
    """Full and cached paths must share Claude Skill lock metadata logic."""
    root = Path(__file__).parents[2]
    validation = (root / "src/apm_cli/models/validation.py").read_text()
    sources = (root / "src/apm_cli/install/sources.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "def _validate_claude_skill(" in validation
    assert 'version="unknown"' in validation
    assert "load_frontmatter" in validation
    assert "pkg_type == PackageType.CLAUDE_SKILL" in sources
    assert "validate_apm_package(install_path)" in sources
    assert "Cached Claude Skill is invalid" in sources
    assert "build_claude_skill_package" not in sources
    assert "Cached/frozen Claude Skill lock metadata must route through validation.py" in guard


def test_skill_subset_ast_checker_is_wired_into_the_boundary_guard() -> None:
    """The Bash guard must invoke the semantic AST checker, not only grep.

    A lexical grep alone was empirically evaded by a renamed helper
    containing the same normalization algorithm; the guard must also run
    scripts/check_skill_subset_owner.py over both consumer files.
    """
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "check_skill_subset_owner.py" in guard
    assert "src/apm_cli/integration/skill_integrator.py" in guard
    assert "src/apm_cli/bundle/plugin_exporter.py" in guard


def test_skill_subset_ast_checker_passes_on_real_consumers() -> None:
    """The real consumer files must be clean under the AST checker today.

    This delegates entirely to scripts/check_skill_subset_owner.py
    (imported directly, see tests/unit/scripts/test_check_skill_subset_owner.py
    for the checker's own unit coverage of the renamed-helper detection
    algorithm) so this test does not duplicate any of that logic.
    """
    root = Path(__file__).parents[2]
    checker = _load_skill_subset_owner_checker()
    integrator = root / "src/apm_cli/integration/skill_integrator.py"
    exporter = root / "src/apm_cli/bundle/plugin_exporter.py"

    violations = checker.find_violations([integrator, exporter])

    assert violations == []


def test_policy_cache_writer_routes_through_canonical_serializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.policy import discovery
    from apm_cli.policy.schema import ApmPolicy

    serialized = "name: serializer-owner\n"
    calls: list[ApmPolicy] = []

    def serialize(policy: ApmPolicy) -> str:
        calls.append(policy)
        return serialized

    monkeypatch.setattr(discovery, "_serialize_policy", serialize)
    policy = ApmPolicy(name="original")
    repo_ref = "owner/.github"

    discovery._write_cache(repo_ref, policy, tmp_path)

    cache_file = discovery._get_cache_dir(tmp_path) / f"{discovery._cache_key(repo_ref)}.yml"
    assert cache_file.read_text(encoding="utf-8") == serialized
    assert calls == [policy]


def test_policy_cache_serializer_boundary_is_registered() -> None:
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    owner_row = (
        "| Cached policy shape | policy/discovery.py (_policy_to_dict via _serialize_policy) |"
    )
    assert ("Cached policy shape must route through policy/discovery.py::_policy_to_dict") in guard
    for token in ("_policy_to_dict", "_serialize_policy", "_write_cache"):
        assert token in guard
    assert owner_row in (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )


def test_windows_stable_executable_path_has_one_canonical_owner() -> None:
    """install.ps1 alone may define the stable current/apm.exe location.

    The Windows stable-path boundary (owner presence + duplicate
    derivation) is scanned by exactly one checker,
    scripts/check_windows_stable_path_owner.py. This test imports and
    calls that checker directly -- it must not re-implement its
    regexes, globs, or exemption handling -- and separately asserts
    that the Bash AC8 guard actually shells out to it rather than
    retaining a parallel scan.
    """
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "Windows stable executable path belongs to install.ps1" in guard
    assert "check_windows_stable_path_owner.py" in guard

    checker = _load_windows_stable_path_checker(root)

    assert checker.check(root) == []


def test_executable_test_contracts_have_one_canonical_owner() -> None:
    """Binary selection and rendered parity must use their canonical helpers."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "Integration binary selection and rendered CLI parity require canonical owners" in guard
    assert "check_test_contract_authorities.py" in guard

    checker = _load_test_contract_checker(root)

    assert checker.check(root) == []


def test_agent_diagnostic_names_have_one_printable_ascii_owner() -> None:
    """Codex and OpenCode diagnostic names must use the diagnostics owner."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    checker = _load_diagnostic_ascii_owner_checker(root)

    assert "AC12: diagnostic printable-ASCII authority" in guard
    assert "check_diagnostic_ascii_owner.py" in guard
    assert "Agent diagnostic names must use utils/diagnostics.py::printable_ascii_text" in guard
    assert checker.check(root) == []


def test_agent_diagnostic_ascii_guard_rejects_local_reimplementation(
    tmp_path: Path,
) -> None:
    """AC12 must fail when a consumer shadows the canonical sanitizer."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    consumer = sandbox / "src/apm_cli/integration/opencode_frontmatter.py"
    source = consumer.read_text(encoding="utf-8")
    source = source.replace(
        "def validate_opencode_frontmatter(",
        "def _display_safe(value: str) -> str:\n"
        '    return re.sub(r"[^ -~]", "?", value)\n\n\n'
        "def validate_opencode_frontmatter(",
    )
    source = source.replace(
        "safe_name = printable_ascii_text(source.name)",
        "safe_name = printable_ascii_text(source.name)\n    safe_name = _display_safe(source.name)",
    )
    consumer.write_text(source, encoding="utf-8")

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert (
        "Agent diagnostic names must use utils/diagnostics.py::printable_ascii_text"
        in result.stdout
    )


def test_quality_ratchets_route_through_shared_authorities() -> None:
    """Ratchet file discovery and baseline writes must have one owner each."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    checker = _load_test_contract_checker(root)

    assert "check_test_contract_authorities.py" in guard
    assert checker.find_ratchet_authority_violations(root) == []


def test_windows_owner_row_stays_synced_source_deployed_and_lockfile() -> None:
    """The new owner-table row must not silently drop on the next deploy.

    ``.github/instructions/architecture.instructions.md`` is a compiled
    artifact: ``.apm/instructions/architecture.instructions.md`` is its
    canonical compile source (see docs/src/content/docs/producer/compile.md),
    and apm.lock.yaml records a content hash of the deployed copy. If the
    deployed file gains a row that the source lacks, the next
    ``apm compile`` / ``apm install`` would regenerate the deployed file
    from the (stale) source and silently remove the row; a stale lockfile
    hash would additionally make ``apm audit`` report drift. This guards
    all three legs of that contract using the project's own lockfile codec
    and content-hash function rather than a bespoke comparison.
    """
    root = Path(__file__).parents[2]
    source = root / ".apm/instructions/architecture.instructions.md"
    deployed = root / ".github/instructions/architecture.instructions.md"

    owner_rows = (
        "| Windows stable executable path | install.ps1 ($currentDir / $currentExe) |",
        "| Cached policy shape | policy/discovery.py (_policy_to_dict via _serialize_policy) |",
    )
    source_text = source.read_text(encoding="utf-8")
    for owner_row in owner_rows:
        assert owner_row in source_text

    # Source and deployed must be byte-identical: the deployed file is a
    # compiled copy of the source, not an independently edited artifact.
    assert source.read_bytes() == deployed.read_bytes()

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.utils.content_hash import compute_file_hash

    lockfile = LockFile.load_or_create(root / "apm.lock.yaml")
    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    locator_key = "copilot||project|.github/instructions/architecture.instructions.md"
    record = ledger.records.get(locator_key)

    assert record is not None, "lockfile must track the deployed architecture instruction"
    assert record.content_hash == compute_file_hash(deployed), (
        "apm.lock.yaml content_hash is stale relative to the deployed file; "
        "the next 'apm audit' would report hash drift"
    )


def test_tls_injection_has_one_canonical_authority() -> None:
    """Only the parent TLS owner and standalone child bootstrap may inject."""
    root = Path(__file__).parents[2]
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()
    allowed = {
        root / "src/apm_cli/core/tls_trust.py",
        root / "src/apm_cli/core/_child_tls/_apm_tls_bootstrap.py",
    }
    duplicate_owners = [
        path.relative_to(root).as_posix()
        for path in (root / "src/apm_cli").rglob("*.py")
        if path not in allowed and "truststore.inject_into_ssl(" in path.read_text()
    ]

    assert "TLS trust injection belongs to canonical owners" in guard
    assert duplicate_owners == []


def test_link_resolver_owns_dependency_deployment_frame_mapping() -> None:
    """Dependency asset links must use the canonical resolver frame mapping."""
    root = Path(__file__).parents[2]
    source = (root / "src/apm_cli/compilation/link_resolver.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "candidate_in_deployment = ctx.deployment_package_root / package_relative" in source
    assert "Dependency deployment-frame mapping belongs to UnifiedLinkResolver" in guard


def test_ac11_cache_url_normalizer_owns_repository_cache_identity() -> None:
    """AC11 keeps every cache tier behind the complete URL identity owner."""
    from scripts.check_repository_cache_identity_owner import check

    root = Path(__file__).parents[2]
    downloader = (root / "src/apm_cli/deps/github_downloader.py").read_text()
    shared_cache = (root / "src/apm_cli/deps/shared_clone_cache.py").read_text()
    tiered_resolver = (root / "src/apm_cli/deps/tiered_ref_resolver.py").read_text()
    normalizer = (root / "src/apm_cli/cache/url_normalize.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert "def normalize_repo_url(" in normalizer
    assert "def cache_shard_key(" in normalizer
    assert check(root) == []
    assert "AC10: marketplace source parsing authority" in guard
    assert "Packed marketplace sources must use DependencyReference.parse_from_dict" in guard
    assert "AC11: Git repository cache identity authority" in guard
    assert "check_repository_cache_identity_owner.py" in guard
    assert "repository = normalize_repo_url(repository_url)" in shared_cache
    assert "repository_url = dep_ref.to_github_url()" in downloader
    assert (
        "_persistent_cache.get_checkout(\n                    dep_ref.to_github_url(),"
        in downloader
    )
    assert "cache_shard_key(dep_ref.to_github_url())" in tiered_resolver
    assert "cache_shard_key(dep_ref.repo_url)" not in tiered_resolver
    assert tiered_resolver.count("_repository_cache_identity(dep_ref)") >= 2
    assert "return normalize_repo_url(dep_ref.to_github_url())" in tiered_resolver
    assert "key = (dep_ref.repo_url, ref)" not in tiered_resolver
    assert "Repository cache identity must not truncate repository paths" in guard
    assert "to_repository_cache_url" not in downloader
    for retired_derivation in ("cache_owner", "cache_repo", '_canonical_url = f"https://'):
        assert retired_derivation not in downloader


def _load_hook_config_write_owner_checker() -> ModuleType:
    """Import scripts/check_hook_config_write_owner.py as a standalone module.

    The semantic AST checker is the single detection owner for the
    "composed path bypasses HookIntegrator" case (see
    tests/unit/scripts/test_check_hook_config_write_owner.py for its own
    unit coverage); this integration test reuses it rather than
    re-implementing any part of its algorithm.
    """
    root = Path(__file__).parents[2]
    script_path = root / "scripts" / "check_hook_config_write_owner.py"
    spec = importlib.util.spec_from_file_location("check_hook_config_write_owner", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hook_config_write_guard_rejects_composed_path_outside_hook_integrator(
    tmp_path: Path,
) -> None:
    """AC15 must reject a competing owner writing merge-hook config via an
    assigned-variable composed path, even though it never references either
    private HookIntegrator symbol (``_MERGE_HOOK_TARGETS``/
    ``_APM_HOOKS_SIDECAR``) -- proving the semantic AST checker closes the
    bypass a lexical/private-symbol-only guard would miss."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    manifest_reconcile_path = sandbox / "src/apm_cli/install/manifest_reconcile.py"
    manifest_reconcile_source = manifest_reconcile_path.read_text(encoding="utf-8")
    bypass = (
        "\n\ndef _rogue_hook_cleanup(project_root):\n"
        '    hook_path = project_root / ".codex" / "hooks.json"\n'
        '    hook_path.write_text("{}")\n'
    )
    manifest_reconcile_path.write_text(manifest_reconcile_source + bypass, encoding="utf-8")

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "must stay owned by HookIntegrator" in result.stdout


def test_hook_ownership_guard_rejects_prune_calling_contraction_api(
    tmp_path: Path,
) -> None:
    """AC15 must reject `apm prune`/`apm uninstall` calling the
    target-contraction hook-cleanup API directly -- that stays exclusively
    the install/compile/update-lifecycle owner's job (#2250/#2252 scope)."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    prune_path = sandbox / "src/apm_cli/commands/prune.py"
    prune_source = prune_path.read_text(encoding="utf-8")
    bypass = (
        "\n\ndef _rogue_prune_hook_cleanup(project_root):\n"
        "    from apm_cli.install.manifest_reconcile import "
        "reconcile_dropped_merge_hook_targets\n"
        "    reconcile_dropped_merge_hook_targets(project_root, "
        "active_targets=[], declared_targets=None)\n"
    )
    prune_path.write_text(prune_source + bypass, encoding="utf-8")

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "#2250 scope" in result.stdout


def test_hook_config_write_ast_checker_passes_on_real_consumers() -> None:
    """The real, fixed src/apm_cli tree must be clean under the AST checker
    today -- a cheap, non-sandboxed positive control (mirrors
    test_skill_subset_ast_checker_passes_on_real_consumers) proving the
    checker does not false-positive on the actual codebase, including the
    new HookIntegrator.reconcile_dropped_targets method itself, without
    paying for a full repo copy on every run."""
    root = Path(__file__).parents[2]
    checker = _load_hook_config_write_owner_checker()

    violations = checker.find_violations(root)

    assert violations == []


def test_ac15_uninstall_reachability_has_single_owner() -> None:
    """AC15 keeps post-uninstall dependency reachability behind one owner."""
    root = Path(__file__).parents[2]
    engine = (root / "src/apm_cli/commands/uninstall/engine.py").read_text(encoding="utf-8")
    reachability = (root / "src/apm_cli/deps/reachability.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    architecture_doc = (root / ".github/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )

    assert "def compute_forward_reachable_keys(" in reachability
    assert "from ...deps.reachability import compute_forward_reachable_keys" in engine
    assert "compute_forward_reachable_keys" in engine
    assert "AC16: post-uninstall reachability owner authority" in guard
    assert "compute_forward_reachable_keys" in guard
    assert "get_apm_dependencies" in guard
    assert "resolve_local_dep_dir" in guard
    assert "Post-uninstall dependency reachability" in architecture_doc
    assert "deps/reachability.py" in architecture_doc


def test_ac15_reachability_owner_guard_rejects_manifest_bypass(tmp_path: Path) -> None:
    """AC15 must reject a manifest-parsing bypass reintroduced in commands/uninstall."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    engine_path = sandbox / "src/apm_cli/commands/uninstall/engine.py"
    engine_source = engine_path.read_text(encoding="utf-8")
    # Simulate a bypass: re-derive reachability inline by parsing a nested
    # package's own manifest directly inside commands/uninstall, instead of
    # going through the single deps/reachability.py owner.
    bypass_source = engine_source.replace(
        "def _compute_actual_orphans(",
        (
            "def _bypass_manifest_scan(apm_package):\n"
            "    return list(apm_package.get_apm_dependencies())\n"
            "\n"
            "\n"
            "def _compute_actual_orphans("
        ),
        1,
    )
    assert bypass_source != engine_source
    engine_path.write_text(bypass_source, encoding="utf-8")

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert (
        "Only deps/reachability.py may walk an installed package's own manifest dependencies"
        in (result.stdout)
    )


def test_ac15_reachability_owner_guard_rejects_parallel_local_walk(tmp_path: Path) -> None:
    """AC15 must reject re-deriving a parallel local-anchor reachability walk."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    engine_path = sandbox / "src/apm_cli/commands/uninstall/engine.py"
    engine_source = engine_path.read_text(encoding="utf-8")
    bypass_source = engine_source.replace(
        "def _compute_actual_orphans(",
        (
            "def _bypass_local_walk(dep_ref, lockfile, project_root):\n"
            "    return resolve_local_dep_dir(dep_ref, lockfile, project_root)\n"
            "\n"
            "\n"
            "def _compute_actual_orphans("
        ),
        1,
    )
    assert bypass_source != engine_source
    engine_path.write_text(bypass_source, encoding="utf-8")

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Uninstall must not re-derive a parallel local-anchor reachability walk" in (
        result.stdout
    )


def test_github_throttle_classification_has_single_owner() -> None:
    """Rate-header interpretation belongs only to deps/github_rate_limit.py."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/deps/github_rate_limit.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    architecture_doc = (root / ".github/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )

    assert "def classify_github_throttle(" in owner
    assert "class GitHubThrottleError" in owner
    assert "AC17: GitHub API throttle classification authority" in guard
    assert "GitHub throttle signals must be classified only by deps/github_rate_limit.py" in guard
    assert "GitHub API throttle classification" in architecture_doc
    assert "src/apm_cli/deps/github_rate_limit.py" in architecture_doc


def test_github_throttle_owner_guard_rejects_parallel_header_parsing(tmp_path: Path) -> None:
    """AC17 must reject an ad-hoc rate-header parser outside the owner."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    consumer = sandbox / "src/apm_cli/deps/download_strategies.py"
    consumer.write_text(
        consumer.read_text(encoding="utf-8")
        + "\n\ndef _parallel_rate_header_parser(response):\n"
        + '    return response.headers.get("X-RateLimit-Remaining")\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "GitHub throttle signals must be classified only by deps/github_rate_limit.py" in (
        result.stdout
    )


def test_bootstrap_project_names_have_single_owner() -> None:
    """All generated manifest names must route through the shared resolver."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/core/project_name.py").read_text(encoding="utf-8")
    init_source = (root / "src/apm_cli/commands/init.py").read_text(encoding="utf-8")
    install_source = (root / "src/apm_cli/commands/install.py").read_text(encoding="utf-8")
    runner_source = (root / "src/apm_cli/core/script_runner.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    architecture_doc = (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )

    assert 'DEFAULT_BOOTSTRAP_PROJECT_NAME = "my-project"' in owner
    assert "def resolve_bootstrap_project_name(" in owner
    assert "_resolve_bootstrap_project_name(derived_project_name)" in init_source
    assert "_resolve_bootstrap_project_name(derived_project_name)" in install_source
    assert "resolve_bootstrap_project_name(Path.cwd().name)" in runner_source
    assert "project_name = DEFAULT_BOOTSTRAP_PROJECT_NAME" in (
        root / "src/apm_cli/commands/deps/cli.py"
    ).read_text(encoding="utf-8")
    assert "AC18: bootstrap project-name authority" in guard
    assert "Manifest bootstrap names must route through core/project_name.py" in guard
    assert "Bootstrap project-name validation and fallback" in architecture_doc


def test_bootstrap_project_name_guard_rejects_variable_bypass(tmp_path: Path) -> None:
    """AC18 must reject a variable-mediated resolver bypass."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    for relative_path in (
        "scripts/lint-bootstrap-project-name.py",
        "src/apm_cli/core/project_name.py",
        "src/apm_cli/core/script_runner.py",
        "src/apm_cli/commands/init.py",
        "src/apm_cli/commands/install.py",
        "src/apm_cli/commands/deps/cli.py",
    ):
        source = root / relative_path
        destination = sandbox / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    runner_path = sandbox / "src/apm_cli/core/script_runner.py"
    runner_source = runner_path.read_text(encoding="utf-8")
    runner_path.write_text(
        runner_source.replace(
            '"name": resolve_bootstrap_project_name(Path.cwd().name),',
            '"name": derived_project_name,',
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        (sys.executable, "scripts/lint-bootstrap-project-name.py"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "ScriptRunner bootstrap name must be the resolver result" in result.stdout
