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
    AgentPluginLegacyBoundaryError,
)
from apm_cli.agent_plugins.errors import AgentPluginTargetExcludedError
from apm_cli.cli import cli
from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.commands.uninstall.engine import (
    _preflight_uninstall_survivors,
)
from apm_cli.copilot_plugins.registrar import catalog_path_for
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.outcome import finalize_install_result
from apm_cli.install.services import (
    IntegratorBundle,
    integrate_local_bundle,
    integrate_package_primitives,
)
from apm_cli.install.sources import Materialization
from apm_cli.install.template import run_integration_template
from apm_cli.integration.hook_integrator import HookIntegrator
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
        "skills/native/SKILL.md": (
            "---\nname: native\ndescription: blocked\n---\n\nUse the native plugin.\n"
        ),
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
        match=r"effective target selection does not include 'copilot'",
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
    assert ctx.package_deployed_files == {}
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
def test_every_materialization_shape_skips_without_committing_state_when_target_excludes_copilot(
    tmp_path: Path,
    shape: str,
    is_local: bool,
    source_kind: str,
    initial_installed: int,
    error_prefix: str,
) -> None:
    """A non-Copilot target set is a routine, non-fatal skip for every shape.

    ``ctx.targets`` here is a mock target that never resolves to ``copilot``,
    so the capability owner reports ``AgentPluginTargetExcludedError`` --
    never a fatal boundary error, and never dependent on whether a Copilot
    binary exists on this host. This holds across every materialization
    shape (local/cached/fresh/registry): the package is skipped with one
    warning, no integrator ever mutates the project tree, and the batch
    still exits 0.
    """
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
    assert diagnostics.error_count == 0
    assert len(diagnostics._diagnostics) == 1
    assert diagnostics._diagnostics[0].category == "warning"
    result = finalize_install_result(
        InstallResult(installed_count=deltas["installed"], diagnostics=diagnostics),
        force=True,
    )
    assert result.exit_code == 0
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
    from apm_cli.deps.plugin_parser import _map_plugin_artifacts

    skill = tmp_path / "skills" / "legacy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("legacy\n", encoding="utf-8")
    _map_plugin_artifacts(tmp_path, tmp_path / ".apm", {"skills": ["./skills/legacy"]})
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
def test_target_exclusion_skips_the_plugin_without_touching_the_rest_of_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_first: bool,
    extra_args: tuple[str, ...],
) -> None:
    """A batch with an otherwise-valid native plugin excluded by target.

    Admission is a pure function of resolved target names, so excluding
    ``copilot`` is the ONLY way a canonical Agent Plugin is refused -- and it
    is always non-fatal (Item 4): the plugin is skipped with one warning and
    the rest of the batch installs, regardless of dependency ordering or
    which install flags are in play.
    """
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
                "target": "claude",
            }
        ),
        encoding="ascii",
    )
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
        ["install", "--no-policy", "--target", "claude", *extra_args],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    # The excluded plugin is never handed to any integrator -- native or
    # legacy -- it is dropped outright, not decomposed.
    assert native_integrator_calls == []
    output = " ".join(result.output.split())
    if "--dry-run" not in extra_args:
        # The dry-run preflight is reject-only (fatal errors abort the
        # preview); it does not render the per-package skip diagnostic a
        # real install does, so only check for it on non-dry-run runs.
        assert "Agent Plugins v1.0.0" in output
        assert "Re-run with --target copilot" in output
        # The refusal must not hand a consumer producer-side repack advice --
        # that fix contradicts "select the copilot target".
        assert "apm pack --claude-plugin" not in output
    assert "ask the publisher for a legacy-compatible package" not in output


@pytest.mark.parametrize("include_ordinary", (False, True))
def test_dry_run_target_exclusion_is_non_fatal_like_a_real_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_ordinary: bool,
) -> None:
    """``--dry-run`` must not fatal-fail on an outcome a real install tolerates.

    Admission is a pure function of resolved target names. A real install
    skips an excluded native plugin with one non-fatal warning and installs
    the rest of the batch (Item 4); the dry-run preflight
    (``preflight_agent_plugin_dry_run``) evaluates the SAME
    ``enforce_agent_plugin_deployment_boundary`` call and must swallow the
    same ``AgentPluginTargetExcludedError`` instead of aborting the whole
    preview -- regression coverage for a real dry-run/real-install exit-code
    parity bug this refactor uncovered and fixed.
    """
    workspace = tmp_path / "workspace"
    native_sources = [workspace / "native-a", workspace / "native-b"]
    for index, source in enumerate(native_sources):
        _write_adversarial_agent_plugin(source, workspace / f"outside-{index}.txt")
        (source / "skills" / "native" / "nested" / "outside-link").unlink()
        # Distinguish the two plugin names -- this test is about exclusion
        # parity, not the unrelated ambiguous-name collision guard.
        plugin_json = source / "plugin.json"
        document = json.loads(plugin_json.read_text(encoding="utf-8"))
        document["name"] = f"blocked.native.{index}"
        plugin_json.write_text(json.dumps(document), encoding="utf-8")
    ordinary = workspace / "ordinary"
    if include_ordinary:
        _write_ordinary_package(ordinary)

    dependencies = [str(native_sources[0])]
    if include_ordinary:
        dependencies.append(str(ordinary))
    dependencies.append(str(native_sources[1]))
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
    source_snapshots = {
        source: _tree_snapshot(source)
        for source in [*native_sources, *([ordinary] if include_ordinary else [])]
    }
    outputs = {}
    for mode, extra_args in (("real", ()), ("dry-run", ("--dry-run",))):
        project = workspace / mode
        project.mkdir()
        (project / "apm.yml").write_text(
            json.dumps(
                {
                    "name": f"consumer-{mode}",
                    "version": "1.0.0",
                    "dependencies": {"apm": dependencies},
                    "target": "claude",
                }
            ),
            encoding="ascii",
        )
        monkeypatch.chdir(project)

        result = CliRunner().invoke(
            cli,
            ["install", "--no-policy", "--target", "claude", *extra_args],
            catch_exceptions=False,
        )

        # The exit-code parity is the whole point of this regression: before
        # the fix, dry-run raised AgentPluginTargetExcludedError uncaught
        # (exit 1) for the exact scenario a real install tolerates (exit 0).
        assert result.exit_code == 0, result.output
        for source, snapshot in source_snapshots.items():
            assert _tree_snapshot(source) == snapshot
        outputs[mode] = " ".join(result.output.split())

    assert native_integrator_calls == []
    # Only the real install renders the per-package skip warning; the
    # dry-run preflight is reject-only and does not render diagnostics.
    assert outputs["real"].count("Re-run with --target copilot") == 2
    assert outputs["dry-run"].count("Re-run with --target copilot") == 0


def test_dry_run_target_exclusion_uses_the_explicit_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run admission must not replace --target with directory detection."""
    from apm_cli.agent_plugins import errors as agent_plugin_errors

    project = tmp_path / "project"
    native = tmp_path / "native"
    project.mkdir()
    _write_adversarial_agent_plugin(native, tmp_path / "outside.txt")
    (native / "skills" / "native" / "nested" / "outside-link").unlink()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": [str(native)]},
                "target": "claude",
            }
        ),
        encoding="ascii",
    )
    original = agent_plugin_errors.enforce_agent_plugin_deployment_boundary
    exclusions: list[str] = []

    def tracked_boundary(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except AgentPluginTargetExcludedError as exc:
            exclusions.append(str(exc))
            raise

    monkeypatch.setattr(
        "apm_cli.install.template.enforce_agent_plugin_deployment_boundary",
        tracked_boundary,
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        cli,
        ["install", "--dry-run", "--no-policy", "--target", "claude"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert len(exclusions) == 1
    assert "selected target(s): claude" in exclusions[0]


def test_dry_run_native_preflight_skips_apm_when_only_mcp_is_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    native = tmp_path / "native"
    project.mkdir()
    _write_adversarial_agent_plugin(native, tmp_path / "outside.txt")
    (native / "skills" / "native" / "nested" / "outside-link").unlink()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": [str(native)]},
            }
        ),
        encoding="ascii",
    )
    before = _tree_snapshot(project)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        cli,
        ["install", "--dry-run", "--only", "mcp", "--no-policy"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert _tree_snapshot(project) == before
    assert "Agent Plugins v1.0.0" not in result.output


def test_dry_run_native_detection_does_not_normalize_legacy_plugin_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    legacy = tmp_path / "legacy"
    project.mkdir()
    legacy.mkdir()
    (legacy / "plugin.json").write_text(
        json.dumps(
            {
                "name": "legacy",
                "version": "1.0.0",
                "description": "Explicit Claude legacy package",
            }
        ),
        encoding="ascii",
    )
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": [str(legacy)]},
                "target": "claude",
            }
        ),
        encoding="ascii",
    )
    source_before = _tree_snapshot(legacy)
    project_before = _tree_snapshot(project)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        cli,
        ["install", "--dry-run", "--no-policy"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert _tree_snapshot(legacy) == source_before
    assert _tree_snapshot(project) == project_before
    assert not (legacy / "apm.yml").exists()


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


def test_uninstall_survivor_preflight_tolerates_target_excluded_native(tmp_path: Path) -> None:
    # A native survivor whose effective targets exclude ``copilot`` (there is
    # no published capability outside a CLI command scope, so this direct
    # unit-level call sees "not supported") must NOT brick an unrelated
    # uninstall. The survivor is dropped from the rebuild plan and its bytes
    # are left untouched instead of aborting. Target exclusion is routine
    # and silent -- no warning is emitted (Item 4).
    project = tmp_path / "project"
    project.mkdir()
    _write_uninstall_fixture(project, tmp_path / "outside.txt")
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    before = _tree_snapshot(project)

    warnings: list[str] = []
    logger = MagicMock()
    logger.warning = warnings.append
    plan = _preflight_uninstall_survivors(
        ["owner/native"],
        project / "apm_modules",
        lockfile=lockfile,
        excluded_keys={"owner/removed"},
        logger=logger,
    )

    assert [dep.get_unique_key() for dep, _ in plan] == []
    assert warnings == []
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

    plan = _preflight_uninstall_survivors(
        [survivor],
        modules_dir,
        source_root=project,
    )
    # Local survivors are validated then excluded from the rebuild plan when
    # called directly (no published capability outside a CLI command scope
    # means "not supported"), so the plan is empty either way and nothing is
    # written.
    assert plan == []

    assert _tree_snapshot(project) == before


def test_uninstall_cli_succeeds_with_offline_native_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Uninstalling an unrelated package must SUCCEED regardless of what
    # happens to an unrelated native survivor's registration -- admission is
    # a pure function of resolved target names and never a fatal surprise
    # partway through an uninstall.
    project = tmp_path / "project"
    project.mkdir()
    _write_uninstall_fixture(project, tmp_path / "outside.txt")
    survivor_before = _tree_snapshot(project / "apm_modules" / "owner" / "native")
    monkeypatch.chdir(project)
    fire_scripts = MagicMock()
    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._fire_uninstall_scripts",
        fire_scripts,
    )

    result = CliRunner().invoke(uninstall, ["owner/removed"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # The native survivor's bytes are left untouched -- only APM's own view of
    # the removed package changes.
    assert _tree_snapshot(project / "apm_modules" / "owner" / "native") == survivor_before
    assert not (project / "apm_modules" / "owner" / "removed").exists()


def test_uninstall_honors_declared_non_copilot_target_despite_github_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic .github directory must not activate Copilot registration."""
    project = tmp_path / "project"
    project.mkdir()
    _write_uninstall_fixture(project, tmp_path / "outside.txt")
    manifest_path = project / "apm.yml"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target"] = "claude"
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._fire_uninstall_scripts",
        MagicMock(),
    )

    result = CliRunner().invoke(uninstall, ["owner/removed"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert not catalog_path_for(project / "apm_modules").exists()
    assert not (project / ".github" / "copilot" / "settings.local.json").exists()


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


def _write_prune_fixture(project: Path, outside: Path) -> None:
    orphan = project / "apm_modules" / "owner" / "orphan"
    survivor = project / "apm_modules" / "owner" / "native"
    _write_ordinary_package(orphan)
    _write_adversarial_agent_plugin(survivor, outside)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/native"]},
                "target": "claude",
            }
        ),
        encoding="ascii",
    )
    dependencies = {}
    for repo_url in ("owner/orphan", "owner/native"):
        dependency = LockedDependency(repo_url=repo_url)
        dependencies[dependency.get_unique_key()] = dependency
    _write_known_good_state(project)
    LockFile(dependencies=dependencies).write(project / "apm.lock.yaml")


def test_prune_succeeds_with_target_excluded_native_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prune of an unrelated orphan must succeed when a native survivor is not
    # selected for Copilot. The survivor is dropped from reintegration, so no
    # escape write happens.
    project = tmp_path / "project"
    project.mkdir()
    _write_prune_fixture(project, tmp_path / "outside.txt")
    survivor_before = _tree_snapshot(project / "apm_modules" / "owner" / "native")
    integrate = MagicMock()
    monkeypatch.setattr(HookIntegrator, "integrate_hooks_for_target", integrate)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert _tree_snapshot(project / "apm_modules" / "owner" / "native") == survivor_before
    assert integrate.mock_calls == []


def test_prune_dry_run_does_not_reintegrate_native_survivors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_prune_fixture(project, tmp_path / "outside.txt")
    before = _tree_snapshot(project)
    sync = MagicMock()
    integrate = MagicMock()
    monkeypatch.setattr(HookIntegrator, "sync_integration", sync)
    monkeypatch.setattr(HookIntegrator, "integrate_hooks_for_target", integrate)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli, ["prune", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert _tree_snapshot(project) == before
    assert sync.mock_calls == []
    assert integrate.mock_calls == []
    assert "Agent Plugins v1.0.0" not in result.output


def test_direct_hook_reconciliation_tolerates_target_excluded_native_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A native survivor with an unqualified client must not abort hook
    # reconciliation for the rest of the tree: it is dropped from the rebuild
    # plan and its bytes are left untouched.
    project = tmp_path / "project"
    project.mkdir()
    _write_prune_fixture(project, tmp_path / "outside.txt")
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    lockfile.dependencies.pop("owner/orphan")
    before = _tree_snapshot(project)
    sync = MagicMock()
    integrate = MagicMock()
    monkeypatch.setattr(HookIntegrator, "sync_integration", sync)
    monkeypatch.setattr(HookIntegrator, "integrate_hooks_for_target", integrate)

    HookIntegrator().reconcile_after_removal(
        APMPackage.from_apm_yml(project / "apm.yml"),
        project,
        lockfile=lockfile,
    )

    # The native survivor is never reintegrated (dropped from the plan), so no
    # escape write is possible and the tree is unchanged.
    assert _tree_snapshot(project) == before
    assert integrate.mock_calls == []


def test_prune_allows_native_true_orphan_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    native_orphan = project / "apm_modules" / "owner" / "native"
    project.mkdir()
    _write_adversarial_agent_plugin(native_orphan, tmp_path / "outside.txt")
    (native_orphan / "apm.yml").write_text(
        "name: native\nversion: 1.0.0\n",
        encoding="ascii",
    )
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm: []\n",
        encoding="ascii",
    )
    dependency = LockedDependency(repo_url="owner/native")
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(
        project / "apm.lock.yaml"
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
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


def test_native_local_bundle_cannot_reach_legacy_mcp_interpretation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.bundle.formats import BundleFormat
    from apm_cli.bundle.local_bundle import LocalBundleInfo
    from apm_cli.install import local_bundle_handler
    from apm_cli.models.dependency.mcp import MCPDependency

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    info = LocalBundleInfo(
        source_dir=bundle,
        plugin_json={"name": "native"},
        package_id="native",
        lockfile=None,
        format=BundleFormat.AGENT_PLUGIN.value,
    )
    legacy_parse = MagicMock()
    legacy_wire = MagicMock()
    legacy_from_dict = MagicMock()
    monkeypatch.setattr(
        local_bundle_handler,
        "_parse_legacy_bundle_mcp_servers",
        legacy_parse,
    )
    monkeypatch.setattr(
        local_bundle_handler,
        "_wire_legacy_bundle_mcp_servers",
        legacy_wire,
    )
    monkeypatch.setattr(MCPDependency, "from_dict", legacy_from_dict)

    with pytest.raises(AgentPluginDeploymentBoundaryError):
        local_bundle_handler.install_local_bundle(
            bundle_info=info,
            bundle_arg=str(bundle),
            target=None,
            global_=False,
            force=False,
            dry_run=False,
            verbose=False,
            no_policy=False,
            alias=None,
            logger=MagicMock(),
            rejected_flags={},
            allow_executables=None,
        )

    legacy_parse.assert_not_called()
    legacy_wire.assert_not_called()
    legacy_from_dict.assert_not_called()


def test_legacy_mcp_helpers_self_reject_native_bundle_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.bundle.formats import BundleFormat
    from apm_cli.install import local_bundle_handler
    from apm_cli.models.dependency.mcp import MCPDependency

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "mcp.json").write_text(
        '{"mcpServers":{"native":{"type":"stdio","command":"tool"}}}',
        encoding="ascii",
    )
    legacy_from_dict = MagicMock()
    legacy_install = MagicMock()
    monkeypatch.setattr(MCPDependency, "from_dict", legacy_from_dict)
    monkeypatch.setattr(
        "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
        legacy_install,
    )

    with pytest.raises(AgentPluginLegacyBoundaryError):
        local_bundle_handler._parse_legacy_bundle_mcp_servers(
            bundle,
            bundle_format=BundleFormat.AGENT_PLUGIN.value,
        )
    with pytest.raises(AgentPluginLegacyBoundaryError):
        local_bundle_handler._wire_legacy_bundle_mcp_servers(
            bundle_format=BundleFormat.AGENT_PLUGIN.value,
            targets=[],
            project_root=tmp_path,
            user_scope=False,
            verbose=False,
            logger=MagicMock(),
            deps=[],
        )
    with pytest.raises(ValueError, match="require a Claude plugin bundle format"):
        local_bundle_handler._parse_legacy_bundle_mcp_servers(
            bundle,
            bundle_format="unknown",
        )

    legacy_from_dict.assert_not_called()
    legacy_install.assert_not_called()
