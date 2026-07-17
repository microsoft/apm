"""Tests for the per-dependency target-filter chokepoint."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.integration.hook_integrator import _MERGE_HOOK_TARGETS, HookIntegrator
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
        "Per-dependency targets [codex] do not overlap active install targets; skipping"
    ]
    assert [d.detail for d in diagnostics._diagnostics] == ["active targets: [claude]"]


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


# Every merge-based harness APM ships, so the routing regression below is
# proven across the whole registry rather than a claude/codex special case.
_MERGE_HARNESSES = sorted(_MERGE_HOOK_TARGETS)


def _target_command_hook(command: str) -> dict:
    """A minimal, schema-neutral hook whose command uniquely tags its file.

    ``SessionStart`` and a trivial command survive event normalization across
    every merge harness, so the command string is a reliable per-file marker
    regardless of a target's casing/matcher conventions.
    """
    return {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": command}]}
            ],
        }
    }


def _deployed_commands(project: Path, target_name: str) -> list[str]:
    """Return every hook command written to *target_name*'s merged config."""
    target = KNOWN_TARGETS[target_name]
    config = _MERGE_HOOK_TARGETS[target_name]
    config_path = project / target.root_dir / config.config_filename
    if not config_path.exists():
        return []
    data = json.loads(config_path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for entries in data.get(config.event_container_key, {}).values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            # Most targets nest hooks under a matcher entry ({matcher, hooks});
            # antigravity flattens to bare {type, command} entries. Handle both.
            if "command" in entry:
                commands.append(entry["command"])
            for hook in entry.get("hooks", []):
                if "command" in hook:
                    commands.append(hook["command"])
    return commands


def _write_divergent_pair(package_path: Path, first: str, second: str) -> dict[str, str]:
    """Ship one hook file per target with a unique command tag; return the map."""
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    tags = {first: f"echo {first}-only", second: f"echo {second}-only"}
    for target_name, command in tags.items():
        (hooks_dir / f"pkg-{target_name}-hooks.json").write_text(
            json.dumps(_target_command_hook(command)), encoding="utf-8"
        )
    return tags


@pytest.mark.parametrize(
    ("first", "second"),
    [(a, b) for i, a in enumerate(_MERGE_HARNESSES) for b in _MERGE_HARNESSES[i + 1 :]],
)
def test_divergent_hook_files_route_per_target_under_multi_dep_targets(
    tmp_path: Path, first: str, second: str
) -> None:
    """`targets: [A, B]` must refine routing, not disable it -- for any A, B.

    Regression for the dep-target gate that merged every per-target file into
    every active target. Each divergent file must land only in its own harness;
    the other harness's uniquely-tagged command must not leak in. Proven across
    every pair of merge-based harnesses APM ships, not just claude/codex.
    """
    project = tmp_path / "project"
    for name in (first, second):
        (project / KNOWN_TARGETS[name].root_dir).mkdir(parents=True)
    package_path = tmp_path / "pkg"
    tags = _write_divergent_pair(package_path, first, second)

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target(first), _hook_only_target(second)],
        integrators=_bundle(HookIntegrator()),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
        dep_target_subset=[first, second],
    )

    assert result["hooks"] == 2
    first_cmds = _deployed_commands(project, first)
    second_cmds = _deployed_commands(project, second)

    # Each harness gets exactly its own file's command -- no leak, no dupes.
    assert first_cmds == [tags[first]], f"{first} config: {first_cmds}"
    assert second_cmds == [tags[second]], f"{second} config: {second_cmds}"
    assert tags[second] not in first_cmds, f"{second} hook leaked into {first}"
    assert tags[first] not in second_cmds, f"{first} hook leaked into {second}"


@pytest.mark.parametrize(
    ("active", "excluded"),
    [(a, b) for i, a in enumerate(_MERGE_HARNESSES) for b in _MERGE_HARNESSES[i + 1 :]],
)
def test_divergent_hook_files_route_single_target_under_single_dep_target(
    tmp_path: Path, active: str, excluded: str
) -> None:
    """`targets: [A]` installs only A's file even when B's file also ships."""
    project = tmp_path / "project"
    for name in (active, excluded):
        (project / KNOWN_TARGETS[name].root_dir).mkdir(parents=True)
    package_path = tmp_path / "pkg"
    tags = _write_divergent_pair(package_path, active, excluded)

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target(active), _hook_only_target(excluded)],
        integrators=_bundle(HookIntegrator()),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
        dep_target_subset=[active],
    )

    # Only the active harness is installed, and it receives only its own file.
    assert result["hooks"] == 1
    assert _deployed_commands(project, active) == [tags[active]]
    assert _deployed_commands(project, excluded) == []


def _write_divergent_files(package_path: Path, own: str, foreign: str) -> None:
    """Ship one hook file for *own* and one for *foreign* by filename suffix."""
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    for target_name in (own, foreign):
        (hooks_dir / f"pkg-{target_name}-hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [{"type": "command", "command": f"echo {target_name}"}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize("foreign", [name for name in KNOWN_TARGETS if name != "copilot"])
def test_copilot_per_file_routing_under_dep_targets(tmp_path: Path, foreign: str) -> None:
    """Copilot writes per-file JSON; a dep `targets:` list must not make it
    absorb another target's per-file hook manifest."""
    project = tmp_path / "project"
    package_path = tmp_path / "pkg"
    _write_divergent_files(package_path, own="copilot", foreign=foreign)

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target("copilot")],
        integrators=_bundle(HookIntegrator()),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
        dep_target_subset=["copilot"],
    )

    assert result["hooks"] == 1
    deployed = sorted(p.name for p in (project / ".github" / "hooks").glob("*.json"))
    assert deployed and all("copilot" in name for name in deployed), deployed
    assert not any(foreign in name for name in deployed), f"{foreign} file leaked into Copilot"


def test_kiro_per_file_routing_under_dep_targets(tmp_path: Path) -> None:
    """Kiro writes one JSON per hook action; a dep `targets:` list must not make
    it integrate another target's per-file hook manifest."""
    project = tmp_path / "project"
    (project / ".kiro").mkdir(parents=True)
    package_path = tmp_path / "pkg"
    _write_divergent_files(package_path, own="kiro", foreign="claude")

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target("kiro")],
        integrators=_bundle(HookIntegrator()),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="targeted-hooks",
        dep_target_subset=["kiro"],
    )

    assert result["hooks"] == 1
    deployed = sorted(p.name for p in (project / ".kiro" / "hooks").rglob("*.json"))
    assert deployed and all("kiro" in name for name in deployed), deployed
    assert not any("claude" in name for name in deployed), "Claude file leaked into Kiro"


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
