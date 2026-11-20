"""Installed-CLI proofs for the lockfile write authority (#4086).

Every command that persists ``apm.lock.yaml`` is a named writer and commits
the lockfile once, through one owner that refuses an unwritable lockfile
before the command mutates anything else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage, LocalPackageFactory
from tests.utils.lockfile_write_counter import INOTIFY_AVAILABLE, count_lockfile_writes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_needs_inotify = pytest.mark.skipif(
    not INOTIFY_AVAILABLE, reason="counting lockfile writes requires Linux inotify"
)
_INSTALL = ("install", "--trust-transitive-mcp", "--no-policy", "--parallel-downloads", "0")
_LOCKFILE = "apm.lock.yaml"
_SERVER = {
    "name": "write-authority-server",
    "registry": False,
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "write_authority_server"],
}
_SKILL = "---\nname: authority-skill\ndescription: Write authority fixture\n---\n# Skill\n"
_INSTRUCTION = "---\napplyTo: '**'\ndescription: Project rule\n---\nKeep it tidy.\n"
_LSP = {
    "name": "authority-language",
    "command": "inert-language-server",
    "extensionToLanguage": {".fixture": "fixture"},
}


@dataclass(frozen=True)
class _Scenario:
    project: LocalPackage
    packages: LocalPackageFactory
    runner: ApmLifecycleRunner
    environment: dict[str, str]


def _new_scenario(
    root: Path, apm_binary_path: Path, targets: tuple[str, ...], *, lsp: bool = False
) -> _Scenario:
    """A project with local content and a local dependency carrying a skill and MCP."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    project = packages.create(
        "write-authority-consumer", targets=targets, lsp_dependencies=(_LSP,) if lsp else ()
    )
    packages.add_instruction(project, "project-rule", _INSTRUCTION)
    dependencies = LocalPackageFactory(project.root / "packages")
    dependency = dependencies.create("authority-dep", mcp_dependencies=(_SERVER,))
    dependencies.add_skill(dependency, "authority-skill", _SKILL)
    packages.replace_apm_dependencies(
        project, ({"path": "./packages/authority-dep", "alias": dependency.name},)
    )
    return _Scenario(
        project=project,
        packages=packages,
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=480
        ),
        environment=isolated.subprocess_env(),
    )


def _run(scenario: _Scenario, args: tuple[str, ...], scenario_id: str) -> CommandResult:
    return scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=scenario.project.root,
        env=scenario.environment,
    )


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _claude_files(snapshot: LifecycleStateSnapshot) -> list[str]:
    return [
        state.relative_path
        for state in snapshot.files
        if state.relative_path.startswith(".claude/") and state.kind != "missing"
    ]


def _snapshot(scenario: _Scenario) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(scenario.project.root, targets=("copilot", "claude"))


@_needs_inotify
def test_install_commits_the_lockfile_once(tmp_path: Path, apm_binary_path: Path) -> None:
    """Dependencies, MCP servers and local content land in one lockfile commit."""
    scenario = _new_scenario(tmp_path / "install-once", apm_binary_path, ("copilot",))

    with count_lockfile_writes(scenario.project.root) as counter:
        result = _run(scenario, _INSTALL, "install-once")

    assert result.returncode == 0, _evidence(result)
    lock = load_yaml(scenario.project.root / _LOCKFILE)
    assert any("authority-dep" in key for key in _dependency_keys(lock)), lock
    assert lock.get("mcp_servers"), lock
    assert lock.get("local_deployed_files"), lock
    assert counter.writes == 1, (counter.events, _evidence(result))

    with count_lockfile_writes(scenario.project.root) as counter:
        result = _run(scenario, _INSTALL, "install-noop")
    assert result.returncode == 0, _evidence(result)
    assert counter.writes == 0, (counter.events, _evidence(result))


def _dependency_keys(lock: dict[str, object]) -> list[str]:
    dependencies = lock.get("dependencies") or []
    return [str(entry.get("repo_url") or entry.get("local_path")) for entry in dependencies]


@_needs_inotify
def test_compile_commits_the_lockfile_through_the_authority(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Compile is a named writer: one commit on contraction, refusal before mutation."""
    scenario = _new_scenario(tmp_path / "compile", apm_binary_path, ("copilot", "claude"))
    root = scenario.project.root
    installed = _run(scenario, _INSTALL, "compile-install")
    assert installed.returncode == 0, _evidence(installed)
    scenario.packages.set_targets(scenario.project, ("copilot",))

    # A lockfile a newer APM generated: compile must refuse before it removes
    # any deployed file, like every other lockfile writer.
    lock_path = root / _LOCKFILE
    lock = load_yaml(lock_path)
    current_version = lock["apm_version"]
    lock["apm_version"] = "99.0.0"
    dump_yaml(lock, lock_path)
    before = _snapshot(scenario)
    assert _claude_files(before), before.files

    with count_lockfile_writes(root) as counter:
        refused = _run(scenario, ("compile",), "compile-refused")

    assert refused.returncode != 0, _evidence(refused)
    assert "99.0.0" in refused.stdout + refused.stderr, _evidence(refused)
    after = _snapshot(scenario)
    assert after.lockfile_bytes == before.lockfile_bytes
    assert after.files == before.files, _evidence(refused)
    assert counter.writes == 0, counter.events

    lock["apm_version"] = current_version
    dump_yaml(lock, lock_path)
    with count_lockfile_writes(root) as counter:
        compiled = _run(scenario, ("compile",), "compile-contract")

    assert compiled.returncode == 0, _evidence(compiled)
    assert counter.writes == 1, (counter.events, _evidence(compiled))
    contracted = _snapshot(scenario)
    assert not _claude_files(contracted), contracted.files


@_needs_inotify
def test_install_with_mcp_and_lsp_commits_once_and_is_byte_stable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """MCP and LSP phases share the install's single lockfile commit."""
    scenario = _new_scenario(
        tmp_path / "services", apm_binary_path, ("copilot", "claude"), lsp=True
    )
    lock_path = scenario.project.root / _LOCKFILE

    with count_lockfile_writes(scenario.project.root) as counter:
        result = _run(scenario, _INSTALL, "services-install")
    assert result.returncode == 0, _evidence(result)
    lock = load_yaml(lock_path)
    assert lock.get("mcp_servers") and lock.get("lsp_servers"), lock
    assert counter.writes == 1, (counter.events, _evidence(result))

    committed = lock_path.read_bytes()
    with count_lockfile_writes(scenario.project.root) as counter:
        rerun = _run(scenario, _INSTALL, "services-rerun")
    assert rerun.returncode == 0, _evidence(rerun)
    assert counter.writes == 0, (counter.events, _evidence(rerun))
    assert lock_path.read_bytes() == committed


@_needs_inotify
def test_frozen_install_never_flushes_the_lockfile(tmp_path: Path, apm_binary_path: Path) -> None:
    """--frozen withholds every lockfile write, so the committed bytes stay put."""
    scenario = _new_scenario(tmp_path / "frozen", apm_binary_path, ("copilot",))
    installed = _run(scenario, _INSTALL, "frozen-install")
    assert installed.returncode == 0, _evidence(installed)
    lock_path = scenario.project.root / _LOCKFILE
    committed = lock_path.read_bytes()

    with count_lockfile_writes(scenario.project.root) as counter:
        frozen = _run(scenario, (*_INSTALL, "--frozen"), "frozen-replay")

    assert frozen.returncode == 0, _evidence(frozen)
    assert counter.writes == 0, counter.events
    assert lock_path.read_bytes() == committed


@_needs_inotify
def test_failed_install_discards_its_staged_lockfile(tmp_path: Path, apm_binary_path: Path) -> None:
    """A failure after the lockfile phase staged new rows leaves the old lockfile."""
    scenario = _new_scenario(tmp_path / "failed", apm_binary_path, ("copilot",))
    installed = _run(scenario, _INSTALL, "failed-baseline")
    assert installed.returncode == 0, _evidence(installed)
    root = scenario.project.root
    extra = LocalPackageFactory(root / "packages").create(
        "late-mcp", mcp_dependencies=({**_SERVER, "name": "late-server"},)
    )
    scenario.packages.replace_apm_dependencies(
        scenario.project,
        (
            {"path": "./packages/authority-dep", "alias": "authority-dep"},
            {"path": "./packages/late-mcp", "alias": extra.name},
        ),
    )
    # The MCP phase runs after the lockfile phase staged late-mcp; make it fail.
    (root / ".mcp.json").unlink()
    (root / ".mcp.json").mkdir()
    committed = (root / _LOCKFILE).read_bytes()

    with count_lockfile_writes(root) as counter:
        failed = _run(scenario, _INSTALL, "failed-install")

    assert failed.returncode != 0, _evidence(failed)
    assert "late-server" in failed.stdout + failed.stderr, _evidence(failed)
    assert counter.writes == 0, counter.events
    assert (root / _LOCKFILE).read_bytes() == committed
