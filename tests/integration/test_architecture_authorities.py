"""Integration guardrails for canonical architecture authorities."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


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
