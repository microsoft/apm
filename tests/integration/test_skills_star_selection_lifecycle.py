"""APMLifecycle: `skills: ["*"]` deploys every skill of a plugin loose (#4997).

PROMISE: P-OWN-claude-native-plugin-registration -- a dependency with a
skills: selection deploys through the loose skills route, never as a native
plugin; `["*"]`, documented in reference/manifest-schema.md, selects every
skill the package ships (source: issue #4997 accepted scope).
CELL: OP=install,update SRC=git PRIM=plugin(skills) SCOPE=project TGT=claude
SELECT=["*"] | ["*", name]
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-native-plugin-registration", "P-INV-I7"),
]

_PLUGIN = "star-plugin"
_DEPENDENCY = "fixture/star-plugin"


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: Skill {name}\n---\n# {name}\n"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, skills: list[str]) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        self.factory = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        self.repository = self.factory.create(_PLUGIN)
        self.write_plugin("alpha", "beta")
        self.factory.commit(self.repository, message="two skills")
        self.env = self.factory.url_rewrite_subprocess_env(
            self.repository, f"https://github.com/{_DEPENDENCY}.git"
        )
        self.project = isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / "apm.yml").write_text(
            json.dumps(
                {
                    "name": "consumer",
                    "version": "1.0.0",
                    "targets": ["claude"],
                    "dependencies": {
                        "apm": [{"git": _DEPENDENCY, "ref": "main", "skills": skills}]
                    },
                }
            ),
            encoding="utf-8",
        )
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def write_plugin(self, *skills: str) -> None:
        worktree = self.repository.worktree
        manifest = worktree / ".claude-plugin/plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": _PLUGIN, "version": "1.0.0"}), encoding="ascii")
        for name in skills:
            path = worktree / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_skill(name), encoding="ascii")

    def apm(self, *args: str, expect: int = 0) -> CommandResult:
        result = self.runner.run(args, cwd=self.project, env=self.env)
        assert result.stdout.isascii() and result.stderr.isascii(), _evidence(result)
        assert "Traceback" not in result.stdout + result.stderr, _evidence(result)
        assert result.returncode == expect, _evidence(result)
        return result

    def snap(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(self.project, targets=("claude",))

    def loose_skills(self) -> set[str]:
        root = self.project / ".claude/skills"
        return {p.name for p in root.iterdir()} if root.is_dir() else set()

    def assert_not_native(self) -> None:
        assert not (self.project / ".agents/plugins").exists()
        settings = self.project / ".claude/settings.json"
        if settings.exists():
            declared = json.loads(settings.read_text(encoding="utf-8"))
            assert "extraKnownMarketplaces" not in declared, declared
            assert "enabledPlugins" not in declared, declared


def test_star_selection_deploys_every_skill_loose_without_native_registration(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, ["*"])

    world.apm("install")
    assert world.loose_skills() == {"alpha", "beta"}
    for name in ("alpha", "beta"):
        text = (world.project / ".claude/skills" / name / "SKILL.md").read_text(encoding="ascii")
        assert text == _skill(name)
    world.assert_not_native()
    world.apm("audit", "--ci")
    converged = world.snap()
    world.apm("install")
    assert world.snap() == converged


def test_star_selection_deploys_a_skill_added_in_a_later_release(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, ["*"])
    manifest = (world.project / "apm.yml").read_bytes()
    world.apm("install")
    assert world.loose_skills() == {"alpha", "beta"}

    # A later release adds a skill; it arrives with no manifest change.
    world.write_plugin("alpha", "beta", "gamma")
    world.factory.commit(world.repository, message="add gamma")
    world.apm("update", "--yes")
    assert world.loose_skills() == {"alpha", "beta", "gamma"}
    world.assert_not_native()
    assert (world.project / "apm.yml").read_bytes() == manifest
    world.apm("audit", "--ci")


def test_star_mixed_with_names_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, ["*", "alpha"])
    before = world.snap()

    result = world.apm("install", expect=1)

    output = " ".join((result.stdout + result.stderr).split())
    assert "'*' selects every skill and cannot be combined with skill names" in output
    assert world.snap() == before
    assert not (world.project / "apm.lock.yaml").exists()
    assert not (world.project / "apm_modules").exists()
