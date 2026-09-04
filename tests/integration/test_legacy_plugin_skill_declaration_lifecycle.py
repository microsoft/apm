"""Installed-binary lifecycle for authoritative legacy plugin skills."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SKILL_TEXT = "---\nname: {name}\ndescription: Lifecycle fixture\n---\n# {name}\n"


def _result_evidence(result: CommandResult) -> str:
    """Return stable process evidence when a lifecycle command fails."""
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _write_plugin(
    root: Path,
    *,
    declaration: list[str] | str | None,
    skills: tuple[str, ...],
    external_skills: tuple[str, ...] = (),
) -> None:
    """Create a local legacy plugin with declared and undeclared skill sources."""
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    (root / "apm.yml").write_text(
        f"name: {root.name}\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    manifest = {
        "name": root.name,
        "version": "1.0.0",
        "description": "Legacy plugin skill declaration lifecycle fixture",
    }
    if declaration is not None:
        manifest["skills"] = declaration
    (manifest_dir / "plugin.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    for name in skills:
        skill = root / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(_SKILL_TEXT.format(name=name), encoding="utf-8")
    for name in external_skills:
        skill = root / "external" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(_SKILL_TEXT.format(name=name), encoding="utf-8")


def _write_project(root: Path) -> None:
    """Create a project with a stable Claude deployment target."""
    root.mkdir()
    (root / "apm.yml").write_text(
        "name: legacy-plugin-lifecycle\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )


def _assert_skill_tree(project: Path, expected_names: tuple[str, ...]) -> None:
    """Assert that deployment contains exactly the selected skill directories."""
    root = project / ".claude" / "skills"
    actual = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    expected = sorted(item for name in expected_names for item in (name, f"{name}/SKILL.md"))
    assert actual == expected


def _assert_manifest_and_lock_pin(project: Path, plugin: Path) -> None:
    """Assert that the installed local plugin stays pinned in durable state."""
    manifest = load_yaml(project / "apm.yml")
    dependencies = manifest["dependencies"]["apm"]
    assert dependencies == [str(plugin)]

    lock = load_yaml(project / "apm.lock.yaml")
    locked = lock["dependencies"]
    assert len(locked) == 1
    assert locked[0]["repo_url"] == f"_local/{plugin.name}"


def _install(
    runner: ApmLifecycleRunner,
    project: Path,
    plugin: Path,
    *,
    scenario_id: str,
) -> CommandResult:
    """Install one plugin through the candidate binary."""
    result = runner.run(
        ("install", str(plugin), "--no-policy"),
        scenario_id=scenario_id,
        cwd=project,
        env=dict(os.environ),
    )
    assert result.returncode == 0, _result_evidence(result)
    return result


def test_legacy_plugin_skill_declaration_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Exercise declared selection, empty migration, rerun stability, and audit."""
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    cases = (
        (
            "container-subset",
            ["./skills/alpha"],
            ("alpha", "undeclared"),
            (),
            ("alpha",),
            0,
        ),
        (
            "omitted-discovery",
            None,
            ("alpha", "undeclared"),
            (),
            ("alpha", "undeclared"),
            0,
        ),
        (
            "string-declaration",
            "./skills/alpha",
            ("alpha", "undeclared"),
            (),
            ("alpha",),
            0,
        ),
        (
            "selective-external",
            ["./skills/alpha", "./external/outside"],
            ("alpha", "undeclared"),
            ("outside",),
            ("alpha", "outside"),
            0,
        ),
        (
            "explicit-empty",
            [],
            ("alpha", "undeclared"),
            (),
            (),
            1,
        ),
    )

    for name, declaration, skills, external_skills, expected, diagnostics in cases:
        workspace = tmp_path / name
        workspace.mkdir()
        plugin = workspace / "plugin"
        _write_plugin(
            plugin,
            declaration=declaration,
            skills=skills,
            external_skills=external_skills,
        )
        project = workspace / "project"
        _write_project(project)

        first = _install(
            runner,
            project,
            plugin,
            scenario_id=f"legacy-plugin-skill-declaration-lifecycle-{name}-first",
        )
        combined = first.stdout + first.stderr
        assert combined.count("plugin.json declares no deployable skills") == diagnostics
        _assert_skill_tree(project, expected)
        _assert_manifest_and_lock_pin(project, plugin)
        first_lock = (project / "apm.lock.yaml").read_bytes()
        first_manifest = (project / "apm.yml").read_bytes()
        first_tree = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in (project / ".claude" / "skills").rglob("SKILL.md")
        }

        second = _install(
            runner,
            project,
            plugin,
            scenario_id=f"legacy-plugin-skill-declaration-lifecycle-{name}-rerun",
        )
        assert (second.stdout + second.stderr).count(
            "plugin.json declares no deployable skills"
        ) == diagnostics
        _assert_skill_tree(project, expected)
        _assert_manifest_and_lock_pin(project, plugin)
        assert (project / "apm.lock.yaml").read_bytes() == first_lock
        assert (project / "apm.yml").read_bytes() == first_manifest
        assert {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in (project / ".claude" / "skills").rglob("SKILL.md")
        } == first_tree

        audit = runner.run(
            ("audit", "--ci", "--no-policy"),
            scenario_id=f"legacy-plugin-skill-declaration-lifecycle-{name}-audit",
            cwd=project,
            env=dict(os.environ),
        )
        assert audit.returncode == 0, _result_evidence(audit)
