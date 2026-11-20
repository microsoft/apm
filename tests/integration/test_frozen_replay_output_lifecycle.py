"""APMLifecycle: ``install --frozen`` reports a replay in the same shape as install.

PROMISE: I9 -- a frozen replay prints each package header and diagnostic line
once, in the same shape a normal install prints (source: #4195).
CELLS: OP=install FROZEN SRC=marketplace(git-subdir, ref main) PRIM=command
SCOPE=project TGT=claude LOCK=present DEPLOYED=absent (cold cache);
OP=install FROZEN SRC=local GATE=registries-disabled (registry in $APM_HOME config)
SCOPE=project LOCK=present (#4175).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-outcome.status_matches_state"),
]

_TARGET = "claude"
_HOST = "git.example.invalid"
_REMOTE = f"https://{_HOST}/acme/plugins-official"
_PLUGIN = "commit-commands"
_CATALOG = "replay-catalog"
_COMMAND = ".claude/commands/commit.md"
_COMMAND_TEXT = "---\ndescription: Commit fixture\n---\n# commit\n"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


@dataclass(frozen=True)
class _World:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    consumer: LocalPackage
    packages: LocalPackageFactory
    commit: str

    def apm(self, *args: str, expected: int = 0) -> CommandResult:
        result = self.runner.run(args, cwd=self.consumer.root, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)
        return result

    def snapshot(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(self.consumer.root, targets=(_TARGET,))

    def cold(self) -> None:
        """Model a fresh machine: no modules, no deployed files, empty cache."""
        for name in ("apm_modules", ".claude"):
            shutil.rmtree(self.consumer.root / name)
        shutil.rmtree(self.isolated.cache_root)
        self.isolated.cache_root.mkdir()


def _world(root: Path, binary: Path, *, targets: tuple[str, ...] = (_TARGET,)) -> _World:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["COLUMNS"] = "200"
    packages = LocalPackageFactory(isolated.work_root)
    consumer = packages.create("consumer", targets=targets)
    runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
    return _World(isolated, env, runner, consumer, packages, commit="")


def _marketplace_world(root: Path, binary: Path) -> _World:
    world = _world(root, binary)
    repositories = LocalGitRepositoryFactory(world.isolated.repository_root, env=world.env)
    repository = repositories.create("plugins-official")
    plugin = repository.worktree / "plugins" / _PLUGIN
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": _PLUGIN, "version": "1.0.0", "description": "Replay fixture"}),
        encoding="utf-8",
    )
    (plugin / "commands").mkdir()
    (plugin / "commands" / "commit.md").write_text(_COMMAND_TEXT, encoding="utf-8")
    commit = repositories.commit(repository, message="publish plugin")
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    catalog = world.isolated.work_root / "catalog"
    catalog.mkdir()
    source = {
        "source": "git-subdir",
        "url": f"{_REMOTE}.git",
        "path": f"plugins/{_PLUGIN}",
        "ref": "main",
    }
    (catalog / "marketplace.json").write_text(
        json.dumps({"name": _CATALOG, "plugins": [{"name": _PLUGIN, "source": source}]}),
        encoding="utf-8",
    )
    world = replace(world, env=env, commit=commit.sha)
    world.apm("marketplace", "add", str(catalog), "--name", _CATALOG)
    return world


def _header_lines(result: CommandResult) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("  [+] ")]


def test_cold_frozen_replay_header_matches_install_shape(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4195: a cold frozen replay names the declared ref once, as install does."""
    world = _marketplace_world(tmp_path / "world", apm_binary_path)
    header = f"  [+] {_HOST}/acme/plugins-official/plugins/{_PLUGIN} #main @{world.commit[:8]}"

    installed = world.apm("install", f"{_PLUGIN}@{_CATALOG}")
    assert _header_lines(installed) == [header], _evidence(installed)
    lock_bytes = (world.consumer.root / "apm.lock.yaml").read_bytes()
    command_bytes = (world.consumer.root / _COMMAND).read_bytes()

    world.cold()
    before = world.snapshot()
    frozen = world.apm("install", "--frozen")
    after = world.snapshot()

    assert _header_lines(frozen) == [header], _evidence(frozen)
    assert "[i] Deployed files verified against apm.lock.yaml." in frozen.stdout
    assert (world.consumer.root / _COMMAND).read_bytes() == command_bytes
    assert after.lockfile_bytes == before.lockfile_bytes == lock_bytes
    assert after.manifest_bytes == before.manifest_bytes
    for record in after.deployment_records:
        assert (world.consumer.root / record.locator.value).exists(), record
    world.apm("audit", "--ci")


def test_frozen_names_user_registry_config_when_registries_disabled(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4175: the refusal names $APM_HOME config, not apm.yml, and its fix runs."""
    world = _world(tmp_path / "world", apm_binary_path)
    alpha = world.packages.create("alpha")
    world.packages.add_skill(alpha, "alpha-skill", "---\nname: alpha-skill\ndescription: d\n---\n")
    world.packages.add_relative_dependency(world.consumer, alpha)
    world.apm("experimental", "enable", "registries")
    world.apm("config", "set", "registry.corp.url", "https://registry.example.invalid/apm")
    world.apm("install")
    world.apm("experimental", "disable", "registries")
    config = world.isolated.config_root / "config.json"
    before = world.snapshot()

    refused = world.apm("install", "--frozen", expected=1)

    output = " ".join(refused.stdout.split())
    assert (
        f"[x] Registry configuration in {config} requires the experimental registries "
        "feature. Run 'apm experimental enable registries' to enable it." in output
    ), _evidence(refused)
    assert "apm.yml" not in output.split("[x]", 1)[1], _evidence(refused)
    assert world.snapshot() == before

    world.apm("experimental", "enable", "registries")
    replay = world.apm("install", "--frozen")
    assert "[i] Deployed files verified against apm.lock.yaml." in replay.stdout
    assert world.snapshot() == before
