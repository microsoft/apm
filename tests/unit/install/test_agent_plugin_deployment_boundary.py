"""Fail-closed tests for the native Agent Plugin deployment boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginDeploymentBoundaryError,
)
from apm_cli.cli import cli
from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.commands.uninstall.engine import (
    _preflight_uninstall_survivors,
    _sync_integrations_after_uninstall,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.outcome import finalize_install_result
from apm_cli.install.services import (
    IntegratorBundle,
    integrate_local_bundle,
    integrate_package_primitives,
)
from apm_cli.install.sources import Materialization
from apm_cli.install.template import run_integration_template
from apm_cli.integration.skill_integrator import SkillIntegrator, get_effective_type
from apm_cli.models.apm_package import APMPackage, PackageContentType, PackageInfo
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.results import InstallResult
from apm_cli.models.validation import PackageType, validate_apm_package
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


def _write_adversarial_agent_plugin(root: Path, outside: Path) -> PackageInfo:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": "blocked.native",
                "description": "Must stop before deployment",
            }
        ),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "native": {
                        "type": "stdio",
                        "command": "./bin/native",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    files = {
        "skills/native/SKILL.md": "---\nname: native\ndescription: blocked\n---\n",
        "bin/native": "#!/bin/sh\nexit 0\n",
        "agents/native.md": "agent\n",
        "commands/native.md": "command\n",
        "hooks/native.json": "{}\n",
        "lsp.json": '{"languageServers":{"native":{"command":"./bin/native"}}}\n',
        "extensions/native/extension.mjs": "export default {};\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    validation = validate_apm_package(root)
    assert validation.is_valid
    assert validation.package is not None
    assert validation.package.agent_plugin is not None
    assert validation.package_type is PackageType.AGENT_PLUGIN

    outside.write_text("outside\n", encoding="utf-8")
    nested = root / "skills" / "native" / "nested"
    nested.mkdir()
    (nested / "outside-link").symlink_to(outside)
    return PackageInfo(
        package=validation.package,
        install_path=root,
        package_type=validation.package_type,
    )


def _write_known_good_state(project: Path) -> None:
    files = {
        ".github/skills/known-good/SKILL.md": "known good\n",
        ".github/agents/known-good.agent.md": "known good\n",
        ".github/commands/known-good.md": "known good\n",
        ".claude/settings.json": '{"hooks":{"SessionStart":[]}}\n',
        ".mcp.json": '{"mcpServers":{"known-good":{"command":"safe"}}}\n',
        ".lsp.json": '{"languageServers":{"known-good":{"command":"safe"}}}\n',
        ".apm/deployment-ledger.json": '{"rows":["known-good"]}\n',
        "apm.lock.yaml": "lockfile_version: '1'\ndependencies: {}\n",
    }
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    snapshot: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", str(path.readlink()).encode())
        elif path.is_dir():
            snapshot[relative] = ("dir", None)
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _integrators() -> IntegratorBundle:
    return IntegratorBundle(
        prompt=MagicMock(name="prompt"),
        agent=MagicMock(name="agent"),
        skill=MagicMock(name="skill"),
        instruction=MagicMock(name="instruction"),
        command=MagicMock(name="command"),
        hook=MagicMock(name="hook"),
        canvas=MagicMock(name="canvas"),
    )


def _assert_integrators_not_invoked(integrators: IntegratorBundle) -> None:
    for integrator in (
        integrators.prompt,
        integrators.agent,
        integrators.skill,
        integrators.instruction,
        integrators.command,
        integrators.hook,
        integrators.canvas,
    ):
        assert integrator.mock_calls == []


@pytest.mark.parametrize(
    ("force", "trust_bin", "skill_subset", "dry_run"),
    [
        (False, None, None, False),
        (True, True, None, False),
        (True, True, ("native",), False),
        (True, True, ("native",), True),
    ],
)
def test_services_gate_precedes_all_target_and_integrator_mutation(
    tmp_path: Path,
    force: bool,
    trust_bin: bool | None,
    skill_subset: tuple[str, ...] | None,
    dry_run: bool,
) -> None:
    package_info = _write_adversarial_agent_plugin(
        tmp_path / "source",
        tmp_path / "outside.txt",
    )
    project = tmp_path / "project"
    _write_known_good_state(project)
    before = _tree_snapshot(project)
    integrators = _integrators()
    logger = MagicMock()
    logger.dry_run = dry_run

    with pytest.raises(
        AgentPluginDeploymentBoundaryError,
        match="Native Agent Plugin components are not enabled yet",
    ):
        integrate_package_primitives(
            package_info,
            project,
            targets=[MagicMock(name="target")],
            integrators=integrators,
            force=force,
            managed_files={"apm.lock.yaml"},
            diagnostics=DiagnosticCollector(),
            package_name="blocked/native",
            logger=logger,
            skill_subset=skill_subset,
            allow_executables={"blocked/native": {"hooks": True, "bin": True}},
            trust_bin=trust_bin,
        )

    assert _tree_snapshot(project) == before
    _assert_integrators_not_invoked(integrators)
    assert logger.mock_calls == []


def test_services_gate_rejects_native_type_without_canonical_ir(tmp_path: Path) -> None:
    package_info = PackageInfo(
        package=APMPackage(name="missing-ir", version="1.0.0"),
        install_path=tmp_path / "source",
        package_type=PackageType.AGENT_PLUGIN,
    )
    project = tmp_path / "project"
    project.mkdir()
    integrators = _integrators()

    with pytest.raises(
        AgentPluginDeploymentBoundaryError,
        match="canonical IR is missing",
    ):
        integrate_package_primitives(
            package_info,
            project,
            targets=[MagicMock(name="target")],
            integrators=integrators,
            force=True,
            managed_files=set(),
            diagnostics=DiagnosticCollector(),
        )

    assert _tree_snapshot(project) == {}
    _assert_integrators_not_invoked(integrators)


def test_materialization_without_package_metadata_preserves_no_target_noop(tmp_path: Path) -> None:
    diagnostics = DiagnosticCollector()
    ctx = SimpleNamespace(
        project_root=tmp_path,
        targets=[],
        diagnostics=diagnostics,
        logger=None,
        package_deployed_files={},
        skill_subset_from_cli=False,
        skill_subset=None,
    )
    source = _MaterializedSource(
        ctx=ctx,
        dep_ref=SimpleNamespace(is_local=False, local_path=None),
        materialization=Materialization(
            package_info=None,
            install_path=tmp_path / "cache",
            dep_key="owner/package",
            deltas={"installed": 0},
        ),
        error_prefix="Failed to integrate primitives",
    )

    deltas = run_integration_template(source)

    assert deltas == {"installed": 0}
    assert ctx.package_deployed_files == {"owner/package": []}
    assert diagnostics.error_count == 0


class _MaterializedSource:
    def __init__(
        self,
        *,
        ctx: SimpleNamespace,
        dep_ref: SimpleNamespace,
        materialization: Materialization,
        error_prefix: str,
    ) -> None:
        self.ctx = ctx
        self.dep_ref = dep_ref
        self._materialization = materialization
        self.INTEGRATE_ERROR_PREFIX = error_prefix

    def acquire(self) -> Materialization:
        return self._materialization


@pytest.mark.parametrize(
    ("shape", "is_local", "source_kind", "initial_installed", "error_prefix"),
    [
        ("local", True, "local", 1, "Failed to integrate primitives from local package"),
        ("cached", False, "git", 0, "Failed to integrate primitives from cached package"),
        ("fresh", False, "git", 1, "Failed to integrate primitives from downloaded package"),
        ("registry", False, "registry", 1, "Failed to integrate primitives from cached package"),
    ],
)
def test_every_materialization_shape_fails_without_committing_state(
    tmp_path: Path,
    shape: str,
    is_local: bool,
    source_kind: str,
    initial_installed: int,
    error_prefix: str,
) -> None:
    package_info = _write_adversarial_agent_plugin(
        tmp_path / f"{shape}-source",
        tmp_path / f"{shape}-outside.txt",
    )
    project = tmp_path / f"{shape}-project"
    _write_known_good_state(project)
    before = _tree_snapshot(project)
    diagnostics = DiagnosticCollector()
    integrator_map = {
        name: MagicMock(name=name)
        for name in ("prompt", "agent", "skill", "instruction", "command", "hook", "canvas")
    }
    ctx = SimpleNamespace(
        project_root=project,
        targets=[MagicMock(name="target")],
        diagnostics=diagnostics,
        logger=MagicMock(),
        package_deployed_files={},
        skill_subset_from_cli=True,
        skill_subset=("native",),
        installed_count=0,
        total_prompts_integrated=0,
        total_agents_integrated=0,
        package_types={},
        force=True,
        integrators=integrator_map,
    )
    dep_ref = SimpleNamespace(
        is_local=is_local,
        local_path=str(package_info.install_path) if is_local else None,
        source=source_kind,
        skill_subset=None,
    )
    source = _MaterializedSource(
        ctx=ctx,
        dep_ref=dep_ref,
        materialization=Materialization(
            package_info=package_info,
            install_path=package_info.install_path,
            dep_key=f"blocked/{shape}",
            deltas={"installed": initial_installed},
        ),
        error_prefix=error_prefix,
    )

    deltas = run_integration_template(source)

    assert deltas is not None
    assert deltas["installed"] == 0
    assert ctx.package_deployed_files == {f"blocked/{shape}": []}
    assert diagnostics.error_count == 1
    assert (
        "Native Agent Plugin components are not enabled yet" in diagnostics._diagnostics[0].message
    )
    result = finalize_install_result(
        InstallResult(installed_count=deltas["installed"], diagnostics=diagnostics),
        force=True,
    )
    assert result.exit_code == 1
    assert _tree_snapshot(project) == before
    for integrator in integrator_map.values():
        assert integrator.mock_calls == []


def test_skill_integrator_direct_entry_points_reject_native_package(tmp_path: Path) -> None:
    package_info = _write_adversarial_agent_plugin(
        tmp_path / "source",
        tmp_path / "outside.txt",
    )
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(project)

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        SkillIntegrator.available_skill_names(package_info)
    with pytest.raises(AgentPluginDeploymentBoundaryError):
        SkillIntegrator().integrate_package_skill(
            package_info,
            project,
            force=True,
            targets=[MagicMock(name="target")],
            skill_subset=("native",),
            skip_bin=False,
            trust_bin=True,
        )

    assert _tree_snapshot(project) == before


def test_marketplace_plugin_remains_on_legacy_skill_route(tmp_path: Path) -> None:
    normalized_skill = tmp_path / ".apm" / "skills" / "legacy"
    normalized_skill.mkdir(parents=True)
    (normalized_skill / "SKILL.md").write_text("legacy\n", encoding="utf-8")
    package_info = PackageInfo(
        package=APMPackage(name="legacy", version="1.0.0"),
        install_path=tmp_path,
        package_type=PackageType.MARKETPLACE_PLUGIN,
    )

    assert get_effective_type(package_info) is PackageContentType.SKILL
    assert SkillIntegrator.available_skill_names(package_info) == frozenset({"legacy"})


def _write_ordinary_package(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "apm.yml").write_text(
        "name: ordinary\nversion: 1.0.0\n",
        encoding="ascii",
    )
    skill = root / ".apm" / "skills" / "safe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: safe\ndescription: safe\n---\n",
        encoding="ascii",
    )


@pytest.mark.parametrize("native_first", (False, True))
@pytest.mark.parametrize(
    "extra_args",
    (
        (),
        ("--force",),
        ("--dry-run",),
        ("--skill", "safe"),
    ),
)
def test_mixed_dependency_batch_is_atomic_at_cli_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_first: bool,
    extra_args: tuple[str, ...],
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    ordinary = workspace / "ordinary"
    native = workspace / "native"
    project.mkdir(parents=True)
    _write_ordinary_package(ordinary)
    _write_adversarial_agent_plugin(native, workspace / "outside.txt")
    (native / "skills" / "native" / "nested" / "outside-link").unlink()
    dependencies = [str(native), str(ordinary)] if native_first else [str(ordinary), str(native)]
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": dependencies},
                "target": "copilot",
            }
        ),
        encoding="ascii",
    )
    _write_known_good_state(project)
    LockFile().write(project / "apm.lock.yaml")
    before = _tree_snapshot(project)
    native_integrator_calls: list[str] = []
    original_integrate = SkillIntegrator.integrate_package_skill
    original_available = SkillIntegrator.available_skill_names

    def tracked_integrate(self, package_info, *args, **kwargs):
        if package_info.package_type is PackageType.AGENT_PLUGIN:
            native_integrator_calls.append("integrate")
        return original_integrate(self, package_info, *args, **kwargs)

    def tracked_available(package_info):
        if package_info.package_type is PackageType.AGENT_PLUGIN:
            native_integrator_calls.append("available")
        return original_available(package_info)

    monkeypatch.setattr(SkillIntegrator, "integrate_package_skill", tracked_integrate)
    monkeypatch.setattr(SkillIntegrator, "available_skill_names", tracked_available)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "copilot", *extra_args],
        catch_exceptions=False,
    )

    assert result.exit_code == 1, result.output
    assert _tree_snapshot(project) == before, result.output
    assert native_integrator_calls == []
    output = " ".join(result.output.split())
    assert "Installed 1 APM" not in output
    assert "Native Agent Plugin components are not enabled yet" in output
    assert "apm plugin init --claude-plugin" in output
    assert "apm pack --claude-plugin" in output
    assert "ask the publisher for a legacy-compatible package" in output


def _write_uninstall_fixture(project: Path, native_source: Path) -> None:
    removed = project / "apm_modules" / "owner" / "removed"
    survivor = project / "apm_modules" / "owner" / "native"
    _write_ordinary_package(removed)
    _write_adversarial_agent_plugin(survivor, native_source)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/removed", "owner/native"]},
                "target": "copilot",
            }
        ),
        encoding="ascii",
    )
    dependencies = {}
    for repo_url in ("owner/removed", "owner/native"):
        dependency = LockedDependency(repo_url=repo_url)
        dependencies[dependency.get_unique_key()] = dependency
    LockFile(dependencies=dependencies).write(project / "apm.lock.yaml")
    _write_known_good_state(project)
    LockFile(dependencies=dependencies).write(project / "apm.lock.yaml")


def test_uninstall_survivor_preflight_rejects_native_directly(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_uninstall_fixture(project, tmp_path / "outside.txt")
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    before = _tree_snapshot(project)

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        _preflight_uninstall_survivors(
            ["owner/native"],
            project / "apm_modules",
            lockfile=lockfile,
            excluded_keys={"owner/removed"},
        )

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        _sync_integrations_after_uninstall(
            APMPackage.from_apm_yml(project / "apm.yml"),
            project,
            set(),
            MagicMock(),
            lockfile=lockfile,
            modules_dir=project / "apm_modules",
        )

    assert _tree_snapshot(project) == before


@pytest.mark.parametrize(
    ("installed_native", "surviving_native"),
    ((False, True), (True, False)),
)
def test_uninstall_preflight_uses_declared_local_source_for_shared_slot(
    tmp_path: Path,
    installed_native: bool,
    surviving_native: bool,
) -> None:
    project = tmp_path / "project"
    modules_dir = project / "apm_modules"
    survivor_source = tmp_path / "survivor" / "shared"
    installed_slot = modules_dir / "_local" / "shared"
    project.mkdir()
    if surviving_native:
        _write_adversarial_agent_plugin(survivor_source, tmp_path / "outside-source")
        (survivor_source / "skills" / "native" / "nested" / "outside-link").unlink()
    else:
        _write_ordinary_package(survivor_source)
    if installed_native:
        _write_adversarial_agent_plugin(installed_slot, tmp_path / "outside-installed")
        (installed_slot / "skills" / "native" / "nested" / "outside-link").unlink()
    else:
        _write_ordinary_package(installed_slot)
    survivor = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path=str(survivor_source),
    )
    before = _tree_snapshot(project)

    if surviving_native:
        with pytest.raises(AgentPluginDeploymentBoundaryError):
            _preflight_uninstall_survivors(
                [survivor],
                modules_dir,
                source_root=project,
            )
    else:
        plan = _preflight_uninstall_survivors(
            [survivor],
            modules_dir,
            source_root=project,
        )
        assert plan == []

    assert _tree_snapshot(project) == before


def test_uninstall_cli_blocks_native_survivor_before_scripts_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_uninstall_fixture(project, tmp_path / "outside.txt")
    before = _tree_snapshot(project)
    monkeypatch.chdir(project)
    fire_scripts = MagicMock()
    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._fire_uninstall_scripts",
        fire_scripts,
    )

    result = CliRunner().invoke(uninstall, ["owner/removed"], catch_exceptions=False)

    assert result.exit_code == 1
    assert _tree_snapshot(project) == before
    assert fire_scripts.mock_calls == []
    assert "Native Agent Plugin components are not enabled yet" in " ".join(result.output.split())


def test_uninstall_allows_native_transitive_orphan_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    parent = project / "apm_modules" / "owner" / "parent"
    native_orphan = project / "apm_modules" / "owner" / "native-child"
    project.mkdir()
    _write_ordinary_package(parent)
    _write_adversarial_agent_plugin(native_orphan, tmp_path / "outside")
    (native_orphan / "skills" / "native" / "nested" / "outside-link").unlink()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/parent"]},
                "target": "copilot",
            }
        ),
        encoding="ascii",
    )
    parent_dep = LockedDependency(repo_url="owner/parent", depth=1)
    child_dep = LockedDependency(
        repo_url="owner/native-child",
        depth=2,
        resolved_by="owner/parent",
    )
    LockFile(
        dependencies={
            parent_dep.get_unique_key(): parent_dep,
            child_dep.get_unique_key(): child_dep,
        }
    ).write(project / "apm.lock.yaml")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(uninstall, ["owner/parent"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert not parent.exists()
    assert not native_orphan.exists()


def test_native_local_bundle_is_blocked_before_opaque_deployment(tmp_path: Path) -> None:
    from apm_cli.bundle.formats import BundleFormat
    from apm_cli.bundle.local_bundle import LocalBundleInfo

    bundle = tmp_path / "bundle"
    project = tmp_path / "project"
    bundle.mkdir()
    project.mkdir()
    (bundle / "skills").mkdir()
    (bundle / "skills" / "native.md").write_text("blocked\n", encoding="ascii")
    info = LocalBundleInfo(
        source_dir=bundle,
        plugin_json={"name": "native"},
        package_id="native",
        lockfile=None,
        format=BundleFormat.AGENT_PLUGIN.value,
    )
    before = _tree_snapshot(project)

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        integrate_local_bundle(
            info,
            project,
            targets=[MagicMock()],
        )

    assert _tree_snapshot(project) == before
