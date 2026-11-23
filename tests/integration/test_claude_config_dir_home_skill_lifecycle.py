"""APMLifecycle: install -g deploys skills when CLAUDE_CONFIG_DIR is HOME itself (#5096).

PROMISE: P-INV-I7 -- with CLAUDE_CONFIG_DIR set to $HOME the Claude root scopes
to ".", and ``apm install -g`` still deploys a dependency's skill there and
records it, while a destination that escapes that root through a symlink is
still refused with nothing written outside it (source: #5096 scope record).
CELL: OP=install|uninstall SRC=git PRIM=skill SCOPE=user TGT=claude(CLAUDE_CONFIG_DIR=HOME)
GATE=destination-safe|destination-symlink-escape
owner: integration/targets.py get_integration_prefixes (allow-list prefixes
validate_deploy_path checks)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_REMOTE = "https://github.com/fixture/skills"
_SUBDIR = "packages/demo"
_SKILL = "---\nname: demo\ndescription: Demo\n---\nDemo skill.\n"
_INSTALL = ("install", "--global", "--no-audit")


def _evidence(result: CommandResult) -> str:
    return f"argv={result.command!r} exit={result.returncode}\n{result.stdout}\n{result.stderr}"


class _World:
    """A git skill package and a user manifest, with CLAUDE_CONFIG_DIR=$HOME."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "world", base_env=dict(os.environ)
        )
        self.root = isolated.root
        self.home = isolated.home
        self.manifest = isolated.config_root / "apm.yml"
        repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        repository = repositories.create("skills")
        package = repository.worktree / _SUBDIR
        (package / "skills" / "demo").mkdir(parents=True)
        (package / "skills" / "demo" / "SKILL.md").write_text(_SKILL, encoding="ascii")
        (package / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ntargets: [claude]\n"
            "dependencies:\n  apm: []\n  mcp: []\nincludes:\n  - skills\n",
            encoding="ascii",
        )
        commit = repositories.commit(repository, message="publish demo").sha
        dependency = {
            "git": "fixture/skills",
            "path": _SUBDIR,
            "ref": commit,
            "targets": ["claude"],
        }
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "consumer",
                    "version": "1.0.0",
                    "dependencies": {"apm": [dependency], "mcp": []},
                }
            ),
            encoding="ascii",
        )
        self.env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
        for name in ("CODEX_HOME", "COPILOT_HOME", "APM_LEGACY_SKILL_PATHS"):
            self.env.pop(name, None)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.home)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
        self.cwd = isolated.work_root

    def install(self) -> CommandResult:
        result = self.runner.run(
            _INSTALL, scenario_id="claude-config-dir-home", cwd=self.cwd, env=self.env
        )
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
        return result

    def uninstall(self) -> CommandResult:
        result = self.runner.run(
            ("uninstall", "--global", f"fixture/skills/{_SUBDIR}"),
            scenario_id="claude-config-dir-home-uninstall",
            cwd=self.cwd,
            env=self.env,
        )
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
        return result

    def state(self) -> dict[str, bytes]:
        """Durable state under HOME: the manifest, lockfile and every deployed file.

        Process locks, the version-check cache and the module cache are not
        durable install state.
        """
        transient = (self.home / ".apm" / "apm_modules", self.home / ".cache")
        return {
            path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in sorted(self.home.rglob("*"))
            if path.is_file()
            and path.name != ".apm-lifecycle.lock"
            and not any(path.is_relative_to(root) for root in transient)
        }

    def claims(self) -> list[str]:
        lock = load_yaml(self.manifest.parent / "apm.lock.yaml")
        (row,) = lock["dependencies"]
        return list(row["deployed_files"])


def test_install_g_deploys_a_skill_when_claude_config_dir_is_home(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)

    result = world.install()

    assert result.returncode == 0, _evidence(result)
    assert "Unsafe skill destination" not in result.stdout + result.stderr, _evidence(result)
    skill = world.home / "skills" / "demo" / "SKILL.md"
    assert skill.read_text(encoding="ascii") == _SKILL, _evidence(result)
    assert "skills/demo/SKILL.md" in world.claims(), _evidence(result)
    # The Claude root is HOME itself: nothing lands under a default ~/.claude.
    assert not (world.home / ".claude" / "skills").exists(), _evidence(result)

    removed = world.uninstall()

    assert removed.returncode == 0, _evidence(removed)
    assert not (world.home / "skills" / "demo").exists(), _evidence(removed)


def test_a_symlinked_skills_dir_under_a_home_claude_root_is_refused(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)
    outside = world.root / "outside"
    outside.mkdir()
    try:
        (world.home / "skills").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    before = world.state()

    result = world.install()

    # P-INV-I7: the escape is refused before any write. The manifest, lockfile
    # and deployed set are byte-identical, the link stays, and nothing lands
    # outside the root. The refusal wording is tracked in #5156.
    assert result.returncode == 1, _evidence(result)
    assert world.state() == before, _evidence(result)
    assert (world.home / "skills").is_symlink(), _evidence(result)
    assert list(outside.rglob("*")) == [], _evidence(result)
    assert not (world.manifest.parent / "apm.lock.yaml").exists(), _evidence(result)
