"""Gemini CLI agent lifecycle through the packaged CLI (#2990).

Project scope deploys to ``.gemini/agents/<name>.md`` and user scope to
``~/.gemini/agents/<name>.md``. Each scenario runs real ``apm`` commands
against a local git package and checks the files on disk and the lockfile:
install, reinstall, restore of a deleted file, update to a new revision, an
agent that becomes invalid, and uninstall. User-authored agents and
Gemini's own ``settings.json`` must be left as they were.

Gemini CLI v0.70.0 references:
  https://github.com/google-gemini/gemini-cli/blob/v0.70.0/docs/core/subagents.md
  https://github.com/google-gemini/gemini-cli/blob/v0.70.0/packages/core/src/agents/agentLoader.ts
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I1", "P-LAW-idempotency.byte_stable", "req-tg-009"),
]

_SCENARIO_TIMEOUT_SECONDS = 300.0
_REMOTE_PREFIX = "https://gitlab.example.invalid/apm-lifecycle"

_WORKER_V1 = (
    "---\n"
    "name: build-worker\n"
    "description: Runs the LaTeX build in an isolated context\n"
    "tools:\n- run_shell_command\n- read_file\n"
    "model: inherit\n"
    "color: blue\n"
    "---\n\n"
    "Run the build script and report only the result.\nv1-marker\n"
)
_WORKER_V2 = _WORKER_V1.replace("v1-marker", "v2-marker")
# Same agent, now declaring a Claude tool name that Gemini does not accept.
_WORKER_INVALID = _WORKER_V1.replace("- read_file\n", "- Read\n").replace("v1-marker", "v3")
# A Claude-style agent in the same package: never deployed to Gemini.
_FOREIGN = "---\nname: reviewer\ndescription: Claude reviewer\ntools: [Read, Grep]\n---\nReview.\n"
_USER_AGENT = "---\nname: mine\ndescription: Written by hand\n---\nMine.\n"
# Gemini's own per-agent switch (subagents.md, "agents.overrides").
_SETTINGS = '{"agents": {"overrides": {"build-worker": {"enabled": false}}}}\n'


@dataclass
class _World:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    repositories: LocalGitRepositoryFactory
    repo: object
    remote: str
    sha: str
    pkg_name: str

    def apm(self, *args: str, scenario: str, cwd: Path) -> CommandResult:
        result = self.runner.run(args, scenario_id=scenario, cwd=cwd, env=self.env)
        assert result.returncode == 0, (
            f"{scenario} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result

    def apm_fails(self, *args: str, scenario: str, cwd: Path) -> CommandResult:
        result = self.runner.run(args, scenario_id=scenario, cwd=cwd, env=self.env)
        assert result.returncode != 0, (
            f"{scenario} unexpectedly succeeded\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result

    def commit_worker(self, content: str, message: str) -> str:
        (self.repo.worktree / ".apm" / "agents" / "build-worker.agent.md").write_text(
            content, encoding="utf-8"
        )
        return self.repositories.commit(self.repo, message=message).sha

    def dependency(self, sha: str) -> dict[str, str]:
        return {"git": self.remote, "type": "gitlab", "ref": sha, "alias": self.pkg_name}


def _world(
    tmp_path: Path,
    apm_binary_path: Path,
    pkg_name: str,
    agents: tuple[tuple[str, str], ...] = (("build-worker", _WORKER_V1), ("reviewer", _FOREIGN)),
) -> _World:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    source = sources.create(pkg_name, targets=("gemini",))
    for name, content in agents:
        sources.add_agent(source, name, content)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repo = repositories.create(pkg_name, source_tree=source.root)
    sha = repositories.commit(repo, message="gemini agent v1").sha
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS
    )
    return _World(isolated, env, runner, repositories, repo, remote, sha, pkg_name)


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), text
    return yaml.safe_load(text.split("---\n", 2)[1])


def _gemini_agent_records(lock_path: Path) -> list[str]:
    lock = LockFile.read(lock_path)
    if lock is None:
        return []
    # Ledger keys are "<target>|<qualifier>|<scope>|<path>".
    return sorted(
        str(key) for key in lock.deployment_ledger.records if ".gemini/agents/" in str(key)
    )


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* with its bytes, keyed by POSIX relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_only_agent_added(before: dict[str, bytes], root: Path, added: str) -> None:
    """The only change under .gemini is *added*: no trust or acknowledgement state."""
    after = _tree(root)
    assert set(after) - set(before) == {added}
    assert {k: v for k, v in after.items() if k != added} == before
    assert "trustedFolders.json" not in after
    assert not any(k.startswith("acknowledgments/") for k in after)


def _seed_gemini_dir(root: Path) -> tuple[Path, Path]:
    """Create the Gemini config dir with a user agent and Gemini settings."""
    agents = root / ".gemini" / "agents"
    agents.mkdir(parents=True)
    user_agent = agents / "mine.md"
    user_agent.write_text(_USER_AGENT, encoding="utf-8")
    settings = root / ".gemini" / "settings.json"
    settings.write_text(_SETTINGS, encoding="utf-8")
    return user_agent, settings


def _set_ref(manifest_path: Path, alias: str, sha: str) -> None:
    manifest = load_yaml(manifest_path)
    for dep in manifest["dependencies"]["apm"]:
        if isinstance(dep, dict) and dep.get("alias") == alias:
            dep["ref"] = sha
    dump_yaml(manifest, manifest_path)


def _assert_worker_v(deployed: Path, marker: str) -> None:
    assert _frontmatter(deployed) == {
        "name": "build-worker",
        "description": "Runs the LaTeX build in an isolated context",
        "tools": ["run_shell_command", "read_file"],
        "model": "inherit",
    }
    assert deployed.read_text(encoding="utf-8").endswith(
        f"Run the build script and report only the result.\n{marker}\n"
    )


@pytest.mark.lifecycle_smoke
def test_gemini_agent_project_lifecycle(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path, "fixture-gemini-agent-project")
    project = LocalPackageFactory(world.isolated.work_root).create(
        "consumer-gemini-agent",
        dependencies=(world.dependency(world.sha),),
        targets=("gemini",),
    )
    root = project.root
    user_agent, settings = _seed_gemini_dir(root)
    deployed = root / ".gemini" / "agents" / "build-worker.md"
    lock_path = root / "apm.lock.yaml"
    install = ("install", "--no-policy", "--parallel-downloads", "0")

    # Install: only the Gemini-valid agent is written, with schema keys only,
    # and nothing else under .gemini changes.
    gemini_before = _tree(root / ".gemini")
    result = world.apm(*install, scenario="install", cwd=root)
    _assert_only_agent_added(gemini_before, root / ".gemini", "agents/build-worker.md")
    _assert_worker_v(deployed, "v1-marker")
    assert not (root / ".gemini" / "agents" / "reviewer.md").exists()
    output = result.stdout + result.stderr
    assert "Gemini agent reviewer.agent.md" in output
    assert "'Grep', 'Read'" in output
    assert _gemini_agent_records(lock_path) == ["gemini||project|.gemini/agents/build-worker.md"]
    first_bytes = deployed.read_bytes()

    # Reinstall is a no-op for the deployed bytes.
    world.apm(*install, scenario="reinstall", cwd=root)
    assert deployed.read_bytes() == first_bytes

    # Restore: a deleted managed agent comes back unchanged.
    deployed.unlink()
    world.apm(*install, scenario="restore", cwd=root)
    assert deployed.read_bytes() == first_bytes

    # Update: a new revision replaces the deployed agent.
    v2_sha = world.commit_worker(_WORKER_V2, "v2")
    _set_ref(project.manifest_path, world.pkg_name, v2_sha)
    world.apm(*install, scenario="update", cwd=root)
    _assert_worker_v(deployed, "v2-marker")
    assert _gemini_agent_records(lock_path) == ["gemini||project|.gemini/agents/build-worker.md"]

    # A failed install (the ref does not exist) leaves the deployed agent and
    # its lockfile rows exactly as they were.
    v2_bytes = deployed.read_bytes()
    lock_bytes = lock_path.read_bytes()
    _set_ref(project.manifest_path, world.pkg_name, "0" * 40)
    failed = world.apm_fails(*install, scenario="failed-update", cwd=root)
    assert "No install transaction changes were committed" in failed.stdout + failed.stderr
    assert deployed.read_bytes() == v2_bytes
    assert lock_path.read_bytes() == lock_bytes
    _set_ref(project.manifest_path, world.pkg_name, v2_sha)

    # The agent turns invalid: the install still succeeds with a warning,
    # nothing invalid is written, and APM removes its stale earlier copy and
    # ledger record instead of leaving v2 deployed as if it were current.
    _set_ref(project.manifest_path, world.pkg_name, world.commit_worker(_WORKER_INVALID, "v3"))
    result = world.apm(*install, scenario="invalid-update", cwd=root)
    assert "Gemini agent build-worker.agent.md" in result.stdout + result.stderr
    assert not deployed.exists()
    assert _gemini_agent_records(lock_path) == []
    assert user_agent.read_text(encoding="utf-8") == _USER_AGENT

    # Pointing back at the valid revision deploys the agent again.
    _set_ref(project.manifest_path, world.pkg_name, v2_sha)
    world.apm(*install, scenario="recover", cwd=root)
    _assert_worker_v(deployed, "v2-marker")

    # Uninstall removes APM's agents and ledger records only.
    world.apm("uninstall", world.remote, scenario="uninstall", cwd=root)
    assert not deployed.exists()
    assert _gemini_agent_records(lock_path) == []
    assert user_agent.read_text(encoding="utf-8") == _USER_AGENT
    assert settings.read_text(encoding="utf-8") == _SETTINGS


def test_gemini_agent_user_scope_lifecycle(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path, "fixture-gemini-agent-user")
    home = world.isolated.home
    user_agent, settings = _seed_gemini_dir(home)
    apm_home = Path(world.env["APM_HOME"])
    apm_home.mkdir(parents=True, exist_ok=True)
    manifest_path = apm_home / "apm.yml"
    dump_yaml(
        {
            "name": "user",
            "version": "1.0.0",
            "targets": ["gemini"],
            "dependencies": {"apm": [world.dependency(world.sha)]},
        },
        manifest_path,
    )
    deployed = home / ".gemini" / "agents" / "build-worker.md"
    lock_path = apm_home / "apm.lock.yaml"
    install = ("install", "-g", "--no-policy", "--parallel-downloads", "0")

    gemini_before = _tree(home / ".gemini")
    world.apm(*install, scenario="user-install", cwd=home)
    _assert_only_agent_added(gemini_before, home / ".gemini", "agents/build-worker.md")
    _assert_worker_v(deployed, "v1-marker")
    assert not (home / ".gemini" / "agents" / "reviewer.md").exists()
    # Gemini and Antigravity user agent dirs are separate (#2990 scope).
    assert not (home / ".gemini" / "config" / "agents").exists()
    assert _gemini_agent_records(lock_path) == ["gemini||user|.gemini/agents/build-worker.md"]
    first_bytes = deployed.read_bytes()

    world.apm(*install, scenario="user-reinstall", cwd=home)
    assert deployed.read_bytes() == first_bytes

    deployed.unlink()
    world.apm(*install, scenario="user-restore", cwd=home)
    assert deployed.read_bytes() == first_bytes

    _set_ref(manifest_path, world.pkg_name, world.commit_worker(_WORKER_V2, "v2"))
    world.apm(*install, scenario="user-update", cwd=home)
    _assert_worker_v(deployed, "v2-marker")

    world.apm("uninstall", "-g", world.remote, scenario="user-uninstall", cwd=home)
    assert not deployed.exists()
    assert _gemini_agent_records(lock_path) == []
    assert user_agent.read_text(encoding="utf-8") == _USER_AGENT
    assert settings.read_text(encoding="utf-8") == _SETTINGS


def test_gemini_all_invalid_agents_leave_gemini_tree_untouched(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A package whose only agent Gemini cannot load creates nothing under .gemini."""
    world = _world(
        tmp_path, apm_binary_path, "fixture-gemini-agent-invalid", agents=(("reviewer", _FOREIGN),)
    )
    project = LocalPackageFactory(world.isolated.work_root).create(
        "consumer-gemini-invalid",
        dependencies=(world.dependency(world.sha),),
        targets=("gemini",),
    )
    root = project.root
    (root / ".gemini").mkdir()
    (root / ".gemini" / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    before = _tree(root / ".gemini")

    result = world.apm(
        "install", "--no-policy", "--parallel-downloads", "0", scenario="install", cwd=root
    )

    assert "Gemini agent reviewer.agent.md" in result.stdout + result.stderr
    assert _tree(root / ".gemini") == before
    assert not (root / ".gemini" / "agents").exists()
    assert _gemini_agent_records(root / "apm.lock.yaml") == []
