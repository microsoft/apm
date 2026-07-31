"""Installed-CLI lifecycle contracts for package target hook routing."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_OWNER = "apm-fixture-org"
_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
_EVENT_COMMANDS = {
    "Stop": "echo package-stop",
    "PreToolUse": "echo package-pre",
    "PostToolUse": "echo package-post",
}
_MANUAL_CURSOR_COMMAND = "echo manual-cursor"


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    sources: LocalPackageFactory
    consumers: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner


@dataclass(frozen=True)
class _PublishedPackage:
    name: str
    repository: LocalGitRepository
    commit: GitCommit
    remote_url: str
    environment: dict[str, str]

    @property
    def dependency(self) -> dict[str, object]:
        """Return the current source-only dependency declaration."""
        return {
            "git": self.remote_url,
            "ref": self.commit.sha,
        }


def _new_scenario(root: Path, apm_binary_path: Path) -> _Scenario:
    """Create one isolated installed-CLI scenario."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    return _Scenario(
        isolated=isolated,
        environment=environment,
        sources=LocalPackageFactory(isolated.package_root),
        consumers=LocalPackageFactory(isolated.work_root),
        repositories=LocalGitRepositoryFactory(
            isolated.repository_root,
            env=environment,
        ),
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=90,
            scenario_timeout_seconds=300,
        ),
    )


def _hook_document() -> dict[str, object]:
    """Return one generic hook document with exact portable event names."""
    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 10,
                        }
                    ],
                }
            ]
            for event, command in _EVENT_COMMANDS.items()
        }
    }


def _publish(
    scenario: _Scenario,
    name: str,
    *,
    targets: tuple[str, ...],
) -> _PublishedPackage:
    """Publish a hooked package through a real local Git origin."""
    package = scenario.sources.create(name, targets=targets)
    scenario.sources.add_hook(package, "hooks", _hook_document())
    repository = scenario.repositories.create(name, source_tree=package.root)
    commit = scenario.repositories.commit(repository, message=f"seed {name}")
    remote_url = f"https://github.com/{_OWNER}/{name}"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    return _PublishedPackage(
        name=name,
        repository=repository,
        commit=commit,
        remote_url=remote_url,
        environment=environment,
    )


def _restrict_published_package_to_claude(
    scenario: _Scenario,
    published: _PublishedPackage,
) -> _PublishedPackage:
    """Advance a universal package manifest to a Claude-only release."""
    manifest_path = published.repository.worktree / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["claude"]
    dump_yaml(manifest, manifest_path)
    commit = scenario.repositories.commit(
        published.repository,
        message=f"restrict {published.name} to claude",
    )
    return _PublishedPackage(
        name=published.name,
        repository=published.repository,
        commit=commit,
        remote_url=published.remote_url,
        environment=published.environment,
    )


def _consumer(
    scenario: _Scenario,
    name: str,
    dependency: dict[str, object],
) -> LocalPackage:
    """Author a two-target consumer and create both native marker roots."""
    package = scenario.consumers.create(
        name,
        dependencies=(dependency,),
        targets=("claude", "cursor"),
    )
    (package.root / ".claude").mkdir()
    (package.root / ".cursor").mkdir()
    return package


def _run_success(
    scenario: _Scenario,
    project: LocalPackage,
    args: tuple[str, ...],
    *,
    environment: dict[str, str],
    scenario_id: str,
) -> CommandResult:
    """Run one public CLI command and retain complete failure evidence."""
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=project.root,
        env=environment,
    )
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _snapshot(project: LocalPackage) -> LifecycleStateSnapshot:
    """Capture exact lock, config, and hook ownership state for both targets."""
    return LifecycleStateSnapshot.capture(
        project.root,
        targets=("claude", "cursor"),
    )


def _json(snapshot: LifecycleStateSnapshot, relative_path: str) -> dict[str, object]:
    """Decode one tracked lifecycle file as an exact JSON object."""
    state = snapshot.file(relative_path)
    assert state.kind == "file"
    document = json.loads(state.content or b"{}")
    assert isinstance(document, dict)
    return document


def _expected_claude_config() -> dict[str, object]:
    """Return the exact Claude-native document expected from the fixture."""
    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 10,
                        }
                    ],
                }
            ]
            for event, command in _EVENT_COMMANDS.items()
        }
    }


def _expected_sidecar(source: str) -> dict[str, object]:
    """Return the exact ownership sidecar for the Claude fixture."""
    hooks = copy.deepcopy(_expected_claude_config()["hooks"])
    assert isinstance(hooks, dict)
    for entries in hooks.values():
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            entry["_apm_source"] = source
    return hooks


def _assert_claude_owned_cursor_empty(
    snapshot: LifecycleStateSnapshot,
    *,
    source: str,
) -> None:
    """Assert exact native event shape, ownership, and no Cursor leakage."""
    assert _json(snapshot, ".claude/settings.json") == _expected_claude_config()
    assert _json(snapshot, ".claude/apm-hooks.json") == _expected_sidecar(source)
    assert snapshot.file(".cursor/hooks.json").kind == "missing"
    assert snapshot.file(".cursor/apm-hooks.json").kind == "missing"


def _assert_audit_passes_without_writes(
    scenario: _Scenario,
    project: LocalPackage,
    *,
    environment: dict[str, str],
    scenario_id: str,
) -> None:
    """Prove audit succeeds and leaves every durable byte unchanged."""
    before = _snapshot(project)
    result = _run_success(
        scenario,
        project,
        _AUDIT_ARGS,
        environment=environment,
        scenario_id=scenario_id,
    )
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["summary"]["failed"] == 0
    assert _snapshot(project) == before


def _add_manual_cursor_hook(project: LocalPackage) -> None:
    """Add one user-authored Cursor entry outside APM provenance."""
    path = project.root / ".cursor" / "hooks.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {
            "matcher": "manual",
            "hooks": [
                {
                    "type": "command",
                    "command": _MANUAL_CURSOR_COMMAND,
                }
            ],
        }
    )
    path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def _cursor_commands(snapshot: LifecycleStateSnapshot) -> list[str]:
    """Return exact commands from Cursor's nested fixture entries."""
    document = _json(snapshot, ".cursor/hooks.json")
    commands: list[str] = []
    hooks = document.get("hooks", {})
    assert isinstance(hooks, dict)
    for entries in hooks.values():
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            handlers = entry.get("hooks", [])
            assert isinstance(handlers, list)
            for handler in handlers:
                assert isinstance(handler, dict)
                command = handler.get("command")
                if isinstance(command, str):
                    commands.append(command)
    return commands


def test_claude_package_restricts_hooks_across_install_update_and_audit(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A Claude package never writes generic hooks or provenance to Cursor."""
    scenario = _new_scenario(tmp_path / "claude-only", apm_binary_path)
    published = _publish(
        scenario,
        "claude-only-hooks",
        targets=("claude",),
    )
    consumer = _consumer(scenario, "dual-target-consumer", published.dependency)

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=published.environment,
        scenario_id="claude-only-install",
    )
    installed = _snapshot(consumer)
    _assert_claude_owned_cursor_empty(installed, source=f"{_OWNER}/{published.name}")

    _run_success(
        scenario,
        consumer,
        (*_INSTALL_ARGS, "--update"),
        environment=published.environment,
        scenario_id="claude-only-update",
    )
    replayed = _snapshot(consumer)

    assert replayed == installed
    _assert_claude_owned_cursor_empty(replayed, source=f"{_OWNER}/{published.name}")
    _assert_audit_passes_without_writes(
        scenario,
        consumer,
        environment=published.environment,
        scenario_id="claude-only-audit",
    )


def test_package_target_transition_repairs_cursor_and_uninstall_preserves_user_hook(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Universal-to-Claude update removes only APM-owned Cursor state."""
    scenario = _new_scenario(tmp_path / "target-transition", apm_binary_path)
    universal = _publish(
        scenario,
        "transition-hooks",
        targets=(),
    )
    consumer = _consumer(scenario, "transition-consumer", universal.dependency)

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=universal.environment,
        scenario_id="target-transition-universal-install",
    )
    universal_state = _snapshot(consumer)
    assert set(_cursor_commands(universal_state)) == set(_EVENT_COMMANDS.values())
    assert _json(universal_state, ".cursor/apm-hooks.json") == _expected_sidecar(
        f"{_OWNER}/{universal.name}"
    )
    _add_manual_cursor_hook(consumer)

    claude_only = _restrict_published_package_to_claude(scenario, universal)
    scenario.consumers.replace_apm_dependencies(
        consumer,
        (claude_only.dependency,),
    )
    update_result = _run_success(
        scenario,
        consumer,
        (*_INSTALL_ARGS, "--verbose"),
        environment=claude_only.environment,
        scenario_id="target-transition-restrict-update",
    )
    assert (
        "Package target restriction: [claude]; effective targets: [claude]" in update_result.stdout
    )
    lock = load_yaml(consumer.root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert any(
        dependency.get("resolved_commit") == claude_only.commit.sha for dependency in dependencies
    ), dependencies
    installed_manifest = load_yaml(
        consumer.root / "apm_modules" / _OWNER / claude_only.name / "apm.yml"
    )
    assert installed_manifest["targets"] == ["claude"]
    restricted = _snapshot(consumer)

    assert _json(restricted, ".claude/settings.json") == _expected_claude_config()
    assert _json(restricted, ".claude/apm-hooks.json") == _expected_sidecar(
        f"{_OWNER}/{claude_only.name}"
    )
    assert _cursor_commands(restricted) == [_MANUAL_CURSOR_COMMAND], (
        f"stdout={update_result.stdout!r}\nstderr={update_result.stderr!r}"
    )
    assert restricted.file(".cursor/apm-hooks.json").kind == "missing"

    _run_success(
        scenario,
        consumer,
        (*_INSTALL_ARGS, "--update"),
        environment=claude_only.environment,
        scenario_id="target-transition-repeat-update",
    )
    assert _snapshot(consumer) == restricted
    _assert_audit_passes_without_writes(
        scenario,
        consumer,
        environment=claude_only.environment,
        scenario_id="target-transition-audit",
    )

    _run_success(
        scenario,
        consumer,
        ("uninstall", f"{_OWNER}/{claude_only.name}"),
        environment=claude_only.environment,
        scenario_id="target-transition-uninstall",
    )
    uninstalled = _snapshot(consumer)

    assert uninstalled.file(".claude/apm-hooks.json").kind == "missing"
    assert uninstalled.file(".cursor/apm-hooks.json").kind == "missing"
    assert _cursor_commands(uninstalled) == [_MANUAL_CURSOR_COMMAND]
    claude_config = _json(uninstalled, ".claude/settings.json")
    assert claude_config.get("hooks", {}) == {}
    assert not uninstalled.deployment_records
    _assert_audit_passes_without_writes(
        scenario,
        consumer,
        environment=claude_only.environment,
        scenario_id="target-transition-uninstalled-audit",
    )
