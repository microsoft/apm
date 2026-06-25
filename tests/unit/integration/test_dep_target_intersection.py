"""Tests for the per-dependency target-filter chokepoint."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector


def _package_info(package_path: Path, name: str = "targeted-hooks") -> PackageInfo:
    return PackageInfo(
        package=APMPackage(name=name, version="1.0.0"),
        install_path=package_path,
    )


def _hook_only_target(name: str):
    target = KNOWN_TARGETS[name]
    return replace(target, primitives={"hooks": target.primitives["hooks"]})


def _session_start_hook(command: str = "echo hook") -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


class _NoopSkillIntegrator:
    def __init__(self) -> None:
        self.seen_targets: list[str] = []

    def integrate_package_skill(self, package_info, project_root: Path, **kwargs):
        self.seen_targets = [target.name for target in kwargs["targets"]]
        return SimpleNamespace(
            target_paths=[],
            skill_created=False,
            sub_skills_promoted=0,
            bin_deployed=0,
            bin_skipped_reason="",
        )


class _RecordingHookIntegrator:
    def __init__(self) -> None:
        self.seen_targets: list[str] = []
        self.dep_targets_active_values: list[bool] = []

    def integrate_hooks_for_target(self, target, package_info, project_root: Path, **kwargs):
        self.seen_targets.append(target.name)
        self.dep_targets_active_values.append(kwargs["dep_targets_active"])
        return IntegrationResult(0, 0, 0, [])


def _bundle(hook_integrator) -> IntegratorBundle:
    return IntegratorBundle(
        prompt=None,
        agent=None,
        skill=_NoopSkillIntegrator(),
        instruction=None,
        command=None,
        hook=hook_integrator,
        canvas=None,
    )


def _run_with_targets(
    tmp_path: Path,
    install_target_names: list[str],
    dep_target_subset: list[str] | None,
    hook_integrator: _RecordingHookIntegrator,
) -> DiagnosticCollector:
    package_path = tmp_path / "pkg"
    package_path.mkdir()
    diagnostics = DiagnosticCollector()

    integrate_package_primitives(
        _package_info(package_path),
        tmp_path / "project",
        targets=[_hook_only_target(name) for name in install_target_names],
        integrators=_bundle(hook_integrator),
        force=False,
        managed_files=set(),
        diagnostics=diagnostics,
        package_name="targeted-hooks",
        dep_target_subset=dep_target_subset,
    )
    return diagnostics


@pytest.mark.parametrize(
    ("install_targets", "dep_targets", "expected_targets"),
    [
        (["claude", "codex"], None, ["claude", "codex"]),
        (["claude", "codex"], ["codex"], ["codex"]),
        (["claude"], ["codex"], []),
        (["claude"], ["claude", "codex"], ["claude"]),
        (["claude", "codex"], ["claude", "cursor"], ["claude"]),
    ],
    ids=[
        "no_dep_targets_means_all",
        "dep_subset_of_install",
        "disjoint_targets_skip",
        "dep_superset_narrowed",
        "partial_overlap_narrowed",
    ],
)
def test_dep_target_intersection_matrix(
    tmp_path: Path,
    install_targets: list[str],
    dep_targets: list[str] | None,
    expected_targets: list[str],
) -> None:
    hook_integrator = _RecordingHookIntegrator()

    _run_with_targets(tmp_path, install_targets, dep_targets, hook_integrator)

    assert hook_integrator.seen_targets == expected_targets


def test_disjoint_targets_emits_diagnostic(tmp_path: Path) -> None:
    hook_integrator = _RecordingHookIntegrator()

    diagnostics = _run_with_targets(tmp_path, ["claude"], ["codex"], hook_integrator)

    assert [d.message for d in diagnostics._diagnostics] == [
        "per-dependency targets do not overlap active install targets; skipping"
    ]


def test_hooks_integrate_to_all_targets_when_no_dep_targets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    package_path = tmp_path / "pkg"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(json.dumps(_session_start_hook()), encoding="utf-8")

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target("claude"), _hook_only_target("codex")],
        integrators=_bundle(HookIntegrator()),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
    )

    assert result["hooks"] == 2
    assert (project / ".claude" / "settings.json").exists()
    assert (project / ".codex" / "hooks.json").exists()


def test_skills_integrate_to_all_targets_when_no_dep_targets(tmp_path: Path) -> None:
    skill_integrator = _NoopSkillIntegrator()
    bundle = IntegratorBundle(
        prompt=None,
        agent=None,
        skill=skill_integrator,
        instruction=None,
        command=None,
        hook=_RecordingHookIntegrator(),
        canvas=None,
    )

    integrate_package_primitives(
        _package_info(tmp_path / "pkg"),
        tmp_path / "project",
        targets=[_hook_only_target("claude"), _hook_only_target("codex")],
        integrators=bundle,
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
    )

    assert skill_integrator.seen_targets == ["claude", "codex"]
