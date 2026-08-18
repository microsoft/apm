"""Fail-closed tests for the native Agent Plugin deployment boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginDeploymentBoundaryError,
)
from apm_cli.install.outcome import finalize_install_result
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.install.sources import Materialization
from apm_cli.install.template import run_integration_template
from apm_cli.integration.skill_integrator import SkillIntegrator, get_effective_type
from apm_cli.models.apm_package import APMPackage, PackageContentType, PackageInfo
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
