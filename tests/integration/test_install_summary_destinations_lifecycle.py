"""APMLifecycle: the install summary reports each deployment once, by folder.

PROMISE: install output names where files landed, once per cause
(I9; source: #4207, #4211, #4192).
CELLS:
- OP=install|install --frozen SRC=local PRIM=skill-bundle SELECT TGT=claude (#4207)
- OP=install SRC=local PRIM=instruction TGT=claude,copilot,cursor (#4211)
- OP=install SRC=local-bundle-archive PRIM=skill,agent TGT=copilot (#4192)
OWNER: src/apm_cli/install/services.py (integrate_package_primitives),
src/apm_cli/install/deployed_paths.py (format_target_collapse, destination_folders).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status or tree lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [
            line.strip() for line in stream.splitlines() if line.lstrip().startswith(("[", "|--"))
        ]
        assert len(lines) == len(set(lines)), _evidence(result)


def _integration_lines(result: CommandResult) -> list[str]:
    return [line for line in result.stdout.splitlines() if " integrated" in line]


def _assert_lock_matches_deployed(project: Path, snapshot: LifecycleStateSnapshot) -> None:
    """I1: every deployment record points at a path that exists."""
    assert snapshot.deployment_records, "expected recorded deployments"
    for record in snapshot.deployment_records:
        assert (project / record.locator.value).exists(), record


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, targets: tuple[str, ...]) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        # A wide terminal keeps Rich from hard-wrapping long summary lines.
        self.env = {**isolated.subprocess_env(), "COLUMNS": "200"}
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        self.work_root = isolated.work_root
        self.packages = LocalPackageFactory(isolated.work_root)
        self.targets = targets
        self.project: LocalPackage = self.packages.create("consumer", targets=targets)

    def apm(self, *args: str, exit_code: int = 0, cwd: Path | None = None) -> CommandResult:
        result = self.runner.run(args, cwd=cwd or self.project.root, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == exit_code, _evidence(result)  # I8
        return result

    def snap(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(self.project.root, targets=self.targets)


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n"


def test_skill_bundle_install_prints_one_skill_line(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4207: a skills/ bundle reports its selected skill in one tree line."""
    world = _World(tmp_path, apm_binary_path, ("claude",))
    bundle = world.packages.create("bundle")
    for name in ("alpha", "beta"):
        world.packages.add_skill(bundle, name, _skill(name))
    (world.project.root / ".claude").mkdir()
    expected = ["  |-- 1 skill(s) integrated -> .claude/skills/"]

    installed = world.apm("install", "../bundle", "--skill", "alpha")

    assert _integration_lines(installed) == expected, _evidence(installed)
    skills = world.project.root / ".claude" / "skills"
    assert (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8") == _skill("alpha")
    assert not (skills / "beta").exists()
    after = world.snap()
    _assert_lock_matches_deployed(world.project.root, after)

    replay = world.apm("install", "--frozen")

    assert _integration_lines(replay) == expected, _evidence(replay)
    assert world.snap() == after, "I2: frozen replay of a converged install changed state"


def test_widened_install_names_every_destination_folder(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4211: a third target is named by folder, never collapsed to 'N targets'."""
    world = _World(tmp_path, apm_binary_path, ("claude", "copilot"))
    rules = world.packages.create("rules")
    world.packages.add_instruction(
        rules, "style", "---\napplyTo: '**/*.py'\ndescription: style\n---\nUse four spaces.\n"
    )
    world.packages.add_relative_dependency(world.project, rules)
    world.apm("install")

    world.packages.set_targets(world.project, ("claude", "copilot", "cursor"))
    world.targets = ("claude", "copilot", "cursor")
    widened = world.apm("install")

    destinations = (".claude/rules/", ".github/instructions/", ".cursor/rules/")
    assert _integration_lines(widened) == [
        f"  |-- 1 rule(s) integrated (2 adopted) -> {', '.join(destinations)}"
    ], _evidence(widened)
    for folder in destinations:
        assert any((world.project.root / folder).iterdir()), folder
    after = world.snap()
    _assert_lock_matches_deployed(world.project.root, after)
    assert world.apm("install").stdout.count(" targets") == 0
    assert world.snap() == after, "I2: converged install changed state"


def test_local_bundle_install_names_destination_folders(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4192: a packed archive install names the folders it deployed into."""
    world = _World(tmp_path, apm_binary_path, ())
    producer = world.work_root / "producer"
    (producer / ".apm" / "skills" / "bs").mkdir(parents=True)
    (producer / ".apm" / "agents").mkdir()
    (producer / "apm.yml").write_text(
        "name: producer\nversion: 1.0.0\ndescription: p\nlicense: MIT\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    (producer / ".apm" / "skills" / "bs" / "SKILL.md").write_text(_skill("bs"), encoding="utf-8")
    (producer / ".apm" / "agents" / "ba.agent.md").write_text(
        "---\ndescription: a\n---\n# ba\n", encoding="utf-8"
    )
    world.apm("install", cwd=producer)
    world.apm("pack", "--archive", "-o", "../build", cwd=producer)
    archive = world.work_root / "build" / "producer-1.0.0.zip"
    world.targets = ("copilot",)

    installed = world.apm("install", str(archive), "--target", "copilot")

    lines = installed.stdout.splitlines()
    summary = lines.index("[*] Installed 2 file(s) from local bundle")
    expected = "  |-- 2 file(s) deployed -> .github/agents/, .agents/skills/"
    assert lines[summary + 1 : summary + 2] == [expected], _evidence(installed)
    project = world.project.root
    assert (project / ".agents" / "skills" / "bs" / "SKILL.md").is_file()
    assert (project / ".github" / "agents" / "ba.agent.md").is_file()
    _assert_lock_matches_deployed(project, world.snap())
