"""Installed-CLI lifecycle proof for native hook event normalization."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
_PACKAGE_NAME = "native-hook-kit"
_COPILOT_GENERATED = f".github/hooks/{_PACKAGE_NAME}-events.json"
_COPILOT_USER = ".github/hooks/user-hooks.json"
_CLAUDE_SETTINGS = ".claude/settings.json"
_CLAUDE_SIDECAR = ".claude/apm-hooks.json"


def _entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _initial_document() -> dict[str, object]:
    return {
        "hooks": {
            "SessionStart": [_entry("echo start-pascal-v1")],
            "sessionStart": [_entry("echo start-camel-v1")],
            "Stop": [_entry("echo stop-v1")],
            "AgentStop": [_entry("echo agent-pascal-v1")],
            "agentStop": [_entry("echo agent-camel-v1")],
        }
    }


def _updated_document() -> dict[str, object]:
    return {
        "hooks": {
            "sessionStart": [_entry("echo start-v2")],
            "agentStop": [_entry("echo stop-v2")],
        }
    }


def _write_json(path: Path, payload: dict[str, object]) -> bytes:
    content = (json.dumps(payload, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _read_hooks(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    return hooks


def _commands(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["hooks"][0]["command"] for entry in entries]


def _run_success(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    cwd: Path,
    environment: dict[str, str],
) -> CommandResult:
    result = runner.run(
        args,
        scenario_id=scenario_id,
        cwd=cwd,
        env=environment,
    )
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _assert_project_native_hooks(
    project_root: Path,
    *,
    expected_agent_commands: list[str],
    expected_session_commands: list[str],
    expected_user_bytes: bytes,
) -> None:
    copilot = _read_hooks(project_root / _COPILOT_GENERATED)
    assert list(copilot) == ["agentStop", "sessionStart"]
    assert _commands(copilot["agentStop"]) == expected_agent_commands
    assert _commands(copilot["sessionStart"]) == expected_session_commands
    assert (project_root / _COPILOT_USER).read_bytes() == expected_user_bytes

    claude = _read_hooks(project_root / _CLAUDE_SETTINGS)
    assert list(claude) == ["stop", "Stop", "SessionStart"]
    assert _commands(claude["stop"]) == ["echo user-stop"]
    assert _commands(claude["Stop"]) == expected_agent_commands
    assert _commands(claude["SessionStart"]) == expected_session_commands
    assert (project_root / _CLAUDE_SIDECAR).is_file()


def _project_snapshot(project_root: Path) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        project_root,
        targets=("copilot", "claude"),
        config_paths=(
            PurePosixPath(_COPILOT_GENERATED),
            PurePosixPath(_COPILOT_USER),
            PurePosixPath(_CLAUDE_SETTINGS),
            PurePosixPath(_CLAUDE_SIDECAR),
        ),
    )


def _author_source(
    source_factory: LocalPackageFactory,
    *,
    name: str,
    document: dict[str, object],
) -> LocalPackage:
    package = source_factory.create(name)
    source_factory.add_hook(package, "events", document)
    return package


def test_project_hook_events_converge_update_and_contract(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "project-hook-events",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    consumers = LocalPackageFactory(isolated.work_root)
    source = _author_source(
        sources,
        name=_PACKAGE_NAME,
        document=_initial_document(),
    )
    consumer = consumers.create(
        "hook-event-consumer",
        dependencies=({"path": str(source.root)},),
        targets=("copilot", "claude"),
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=60,
        scenario_timeout_seconds=120,
    )
    user_copilot = {
        "version": 1,
        "hooks": {"stop": [{"type": "command", "command": "echo user-stop"}]},
    }
    user_claude = {"hooks": {"stop": [_entry("echo user-stop")]}}
    user_copilot_bytes = _write_json(consumer.root / _COPILOT_USER, user_copilot)
    _write_json(consumer.root / _CLAUDE_SETTINGS, user_claude)

    first_result = _run_success(
        runner,
        _INSTALL_ARGS,
        scenario_id="hook-events-project-install-first",
        cwd=consumer.root,
        environment=environment,
    )
    assert "may not be recognized" not in first_result.stdout + first_result.stderr
    initial_agent_commands = [
        "echo agent-pascal-v1",
        "echo stop-v1",
        "echo agent-camel-v1",
    ]
    initial_session_commands = [
        "echo start-pascal-v1",
        "echo start-camel-v1",
    ]
    _assert_project_native_hooks(
        consumer.root,
        expected_agent_commands=initial_agent_commands,
        expected_session_commands=initial_session_commands,
        expected_user_bytes=user_copilot_bytes,
    )
    first = _project_snapshot(consumer.root)

    _run_success(
        runner,
        _INSTALL_ARGS,
        scenario_id="hook-events-project-install-idempotent",
        cwd=consumer.root,
        environment=environment,
    )
    second = _project_snapshot(consumer.root)
    assert second.semantic_bytes == first.semantic_bytes
    assert second.file(_COPILOT_GENERATED).content == first.file(_COPILOT_GENERATED).content
    assert second.file(_CLAUDE_SETTINGS).content == first.file(_CLAUDE_SETTINGS).content

    generated = consumer.root / _COPILOT_GENERATED
    stale = json.loads(generated.read_text(encoding="utf-8"))
    stale["hooks"]["stop"] = stale["hooks"].pop("agentStop")
    _write_json(generated, stale)
    sources.add_hook(source, "events", _updated_document())

    update_result = _run_success(
        runner,
        _INSTALL_ARGS,
        scenario_id="hook-events-project-update-aliases",
        cwd=consumer.root,
        environment=environment,
    )
    assert "may not be recognized" not in update_result.stdout + update_result.stderr
    _assert_project_native_hooks(
        consumer.root,
        expected_agent_commands=["echo stop-v2"],
        expected_session_commands=["echo start-v2"],
        expected_user_bytes=user_copilot_bytes,
    )
    assert "stop" not in _read_hooks(generated)

    consumers.set_targets(consumer, ("copilot",))
    _run_success(
        runner,
        _INSTALL_ARGS,
        scenario_id="hook-events-project-contract-claude",
        cwd=consumer.root,
        environment=environment,
    )
    assert _commands(_read_hooks(consumer.root / _CLAUDE_SETTINGS)["stop"]) == ["echo user-stop"]
    assert not (consumer.root / _CLAUDE_SIDECAR).exists()
    assert (consumer.root / _COPILOT_USER).read_bytes() == user_copilot_bytes
    assert set(_read_hooks(generated)) == {"agentStop", "sessionStart"}

    uninstall_result = _run_success(
        runner,
        ("uninstall", str(source.root)),
        scenario_id="hook-events-project-uninstall",
        cwd=consumer.root,
        environment=environment,
    )
    assert not generated.exists(), uninstall_result.stdout + uninstall_result.stderr
    assert (consumer.root / _COPILOT_USER).read_bytes() == user_copilot_bytes
    assert _commands(_read_hooks(consumer.root / _CLAUDE_SETTINGS)["stop"]) == ["echo user-stop"]
    final = _project_snapshot(consumer.root)
    assert not final.deployment_records


def test_global_hook_events_preserve_user_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-hook-events",
        base_env=dict(os.environ),
    )
    claude_root = isolated.home / ".claude"
    environment = isolated.subprocess_env(overrides={"CLAUDE_CONFIG_DIR": str(claude_root)})
    sources = LocalPackageFactory(isolated.package_root)
    source = _author_source(
        sources,
        name="global-native-hook-kit",
        document={
            "hooks": {
                "SessionStart": [_entry("echo global-start")],
                "AgentStop": [_entry("echo global-stop")],
            }
        },
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(source.name, source_tree=source.root)
    commit = repositories.commit(repository, message="seed global native hook package")
    remote = f"https://github.com/apm-fixture/{source.name}"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    dump_yaml(
        {
            "name": "global-hook-consumer",
            "version": "0.1.0",
            "dependencies": {
                "apm": [
                    {
                        "git": remote,
                        "ref": commit.sha,
                        "alias": source.name,
                    }
                ]
            },
            "targets": ["copilot", "claude"],
        },
        isolated.config_root / "apm.yml",
    )
    workdir = isolated.work_root / "consumer"
    workdir.mkdir()
    copilot_root = isolated.home / ".copilot"
    generated_relative = "hooks/global-native-hook-kit-events.json"
    user_relative = "hooks/user-hooks.json"
    user_copilot = {
        "version": 1,
        "hooks": {"stop": [{"type": "command", "command": "echo global-user-stop"}]},
    }
    user_copilot_bytes = _write_json(copilot_root / user_relative, user_copilot)
    _write_json(
        claude_root / "settings.json",
        {"hooks": {"stop": [_entry("echo global-user-stop")]}},
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=60,
        scenario_timeout_seconds=120,
    )
    install = (
        "install",
        "--global",
        "--target",
        "copilot,claude",
        "--no-policy",
        "--parallel-downloads",
        "0",
    )

    first_result = _run_success(
        runner,
        install,
        scenario_id="hook-events-global-install-first",
        cwd=workdir,
        environment=environment,
    )
    assert "may not be recognized" not in first_result.stdout + first_result.stderr
    copilot = _read_hooks(copilot_root / generated_relative)
    assert list(copilot) == ["agentStop", "sessionStart"]
    assert _commands(copilot["agentStop"]) == ["echo global-stop"]
    assert _commands(copilot["sessionStart"]) == ["echo global-start"]
    claude = _read_hooks(claude_root / "settings.json")
    assert list(claude) == ["stop", "Stop", "SessionStart"]
    assert _commands(claude["stop"]) == ["echo global-user-stop"]
    assert _commands(claude["Stop"]) == ["echo global-stop"]
    assert _commands(claude["SessionStart"]) == ["echo global-start"]

    external_roots = (
        LifecycleStateRoot(
            root_id="copilot-user",
            target="copilot",
            scope="user",
            path=copilot_root,
            config_paths=(
                PurePosixPath(generated_relative),
                PurePosixPath(user_relative),
            ),
        ),
        LifecycleStateRoot(
            root_id="claude-user",
            target="claude",
            scope="user",
            path=claude_root,
            config_paths=(
                PurePosixPath("settings.json"),
                PurePosixPath("apm-hooks.json"),
            ),
        ),
    )
    first = LifecycleStateSnapshot.capture(
        isolated.config_root,
        external_roots=external_roots,
    )
    _run_success(
        runner,
        (
            "install",
            "--global",
            "--target",
            "copilot,claude",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="hook-events-global-install-idempotent",
        cwd=workdir,
        environment=environment,
    )
    second = LifecycleStateSnapshot.capture(
        isolated.config_root,
        external_roots=external_roots,
    )
    assert second.semantic_bytes == first.semantic_bytes
    assert (copilot_root / user_relative).read_bytes() == user_copilot_bytes
    assert _commands(_read_hooks(claude_root / "settings.json")["stop"]) == [
        "echo global-user-stop"
    ]
    assert (claude_root / "apm-hooks.json").is_file()
