"""Installed Python CLI contracts with only the external Claude CLI substituted.

These tests prove APM acquisition, selection, deployment and reconciliation.
The substitute models native registration, not command loading or invocation.
Authenticated Claude runtime qualification is a separate acceptance gate.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_native_bundle_hash
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.claude_native_protocol import NATIVE_CLIENT_STUB as _NATIVE_CLIENT_STUB
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_claude_commands"
_DEPENDENCY = "fixture/native-commands"


@dataclass
class _Scenario:
    """Own isolated Git, APM and external-host protocol state for one lifecycle."""

    isolated: IsolatedApmEnvironment
    runner: ApmLifecycleRunner
    factory: LocalGitRepositoryFactory
    repository: LocalGitRepository
    environment: dict[str, str]
    source: Path
    revision: str

    def run(self, *args: str, expected: int = 0) -> CommandResult:
        """Run the installed Python CLI and keep complete failure evidence."""
        result = self.runner.run(
            args,
            scenario_id="claude-native-command-context",
            cwd=self.isolated.config_root if args[0] == "audit" else self.isolated.work_root,
            env=self.environment,
        )
        assert result.returncode == expected, (
            f"{result.command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result

    def native_state(self) -> dict:
        """Read only the substituted host's externally visible registration state."""
        path = Path(self.environment["APM_NATIVE_TEST_STATE"])
        assert path.is_file(), "APM never reached the external native registration boundary"
        return json.loads(path.read_text(encoding="utf-8"))

    @property
    def modules(self) -> Path:
        """Return the real global APM acquisition root."""
        return self.isolated.config_root / "apm_modules"

    @property
    def claude(self) -> Path:
        """Return the isolated native user config root."""
        return self.isolated.home / ".claude"

    def write_manifest(
        self, *, revision: str | None = None, plugin_paths: tuple[str, ...] = ("context",)
    ) -> None:
        """Pin the selected Git-path source without a moving remote reference."""
        document = {
            "name": "native-command-consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {
                "apm": [
                    {
                        "git": _DEPENDENCY,
                        "ref": revision or self.revision,
                        "path": f"plugins/{plugin_path}",
                    }
                    for plugin_path in plugin_paths
                ]
            },
        }
        (self.isolated.config_root / "apm.yml").write_text(json.dumps(document), encoding="utf-8")

    def install(self) -> CommandResult:
        """Exercise the ordinary global dependency installation surface."""
        return self.run("install", "-g", "--target", "claude", "--only", "apm")


def _scenario(tmp_path: Path, command: tuple[str, ...], *, empty: bool = False) -> _Scenario:
    """Create a Git-path plugin and stub only the external native CLI transport."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["CLAUDE_CONFIG_DIR"] = str(isolated.home / ".claude")
    environment["APM_NATIVE_TEST_STATE"] = str(isolated.root / "native-state.json")
    executable_dir = isolated.root / "executables"
    executable_dir.mkdir()
    for name in ("claude", "claude.cmd"):
        executable = executable_dir / name
        executable.write_text("exit 91\n", encoding="ascii")
        executable.chmod(0o755)
    environment["PATH"] = str(executable_dir) + os.pathsep + environment["PATH"]
    guard = isolated.root / "network_guard" / "sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _NATIVE_CLIENT_STUB, encoding="utf-8")
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = factory.create("commands")
    source = repository.worktree / "plugins" / "context"
    shutil.copytree(_FIXTURE, source)
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "command-resource.bin").write_bytes(b"PUBLISHER_COMMAND_RESOURCE\n")
    if empty:
        manifest = source / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["commands"] = []
        manifest.write_text(json.dumps(data), encoding="utf-8")
    revision = factory.commit(repository, message="Own native command fixture").sha
    environment = factory.url_rewrite_subprocess_env(
        repository, f"https://github.com/{_DEPENDENCY}.git"
    )
    scenario = _Scenario(
        isolated,
        ApmLifecycleRunner(command),
        factory,
        repository,
        environment,
        source,
        revision,
    )
    scenario.write_manifest()
    return scenario


def _assert_intact_native_source(
    scenario: _Scenario, revision: str, *, empty: bool, enabled: bool = False
) -> Path:
    """Bind the native registration to acquired source bytes and immutable identity."""
    state = scenario.native_state()
    assert len(state["plugins"]) == 1
    plugin = state["plugins"][0]
    assert plugin["id"] == "apm-command-context@apm-claude"
    assert plugin["scope"] == "user"
    assert plugin["enabled"] is enabled
    source = Path(plugin["readFromFolder"])
    assert source.is_relative_to(scenario.modules)
    assert source != Path(plugin["installPath"])
    for path in scenario.source.rglob("*"):
        if path.is_file():
            assert (source / path.relative_to(scenario.source)).read_bytes() == path.read_bytes()
    manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["commands"] == ([] if empty else ["./selected/status.md"])
    assert not (scenario.claude / "commands" / "status.md").exists()
    assert not (scenario.claude / "commands" / "unselected.md").exists()
    assert not (scenario.claude / "hooks").exists()
    lock = load_yaml(scenario.isolated.config_root / "apm.lock.yaml")
    assert len(lock["dependencies"]) == 1
    dependency = lock["dependencies"][0]
    assert dependency["repo_url"] == _DEPENDENCY
    assert dependency["virtual_path"] == "plugins/context"
    assert dependency["resolved_commit"] == revision
    ledger = json.loads(
        (scenario.modules / ".claude-plugin" / "apm-registration.json").read_text(encoding="utf-8")
    )
    assert ledger["version"] == 2
    assert ledger["plugins"] == {
        "apm-command-context@apm-claude": {
            "dependency_key": f"{_DEPENDENCY}/plugins/context",
            "source": source.relative_to(scenario.modules).as_posix(),
            "source_pin": f"{dependency.get('source')}:{revision}",
            # The admitted tree also includes APM's normalization metadata.
            "bundle_hash": compute_native_bundle_hash(source),
        }
    }
    return source


@pytest.mark.parametrize("empty", [False, True], ids=["select-to-empty", "empty-to-select"])
def test_pinned_command_selection_update_preserves_native_unit(
    tmp_path: Path, apm_engine_command: tuple[str, ...], empty: bool
) -> None:
    """Changing the selected commands updates the intact native unit, never a loose copy."""
    scenario = _scenario(tmp_path, apm_engine_command, empty=empty)
    scenario.install()
    source = _assert_intact_native_source(scenario, scenario.revision, empty=empty)
    before = (source / "selected" / "status.md").read_bytes()
    manifest_path = scenario.source / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["commands"] = ["./selected/status.md"] if empty else []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    revision = scenario.factory.commit(
        scenario.repository, message="Change native command selection"
    ).sha
    scenario.write_manifest(revision=revision)

    scenario.install()
    scenario.run("install", "-g", "--target", "claude", "--only", "apm", "--frozen")

    updated = _assert_intact_native_source(scenario, revision, empty=not empty)
    assert updated == source
    assert (updated / "selected" / "status.md").read_bytes() == before
    assert (updated / "commands" / "unselected.md").is_file()
    assert [
        call[1]
        for call in scenario.native_state()["calls"]
        if call[:2] in (["plugin", "install"], ["plugin", "update"])
    ] == ["install", "update", "update"]


def test_native_command_sibling_removal_preserves_user_command_and_survivor(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    """Two native status commands coexist; removing one retains the other and user files."""
    scenario = _scenario(tmp_path, apm_engine_command)
    sibling = scenario.source.parent / "sibling"
    shutil.copytree(_FIXTURE, sibling)
    manifest_path = sibling / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "apm-command-sibling"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (sibling / "resources" / "identity.txt").write_bytes(b"SIBLING_COMMAND_CONTEXT\n")
    revision = scenario.factory.commit(
        scenario.repository, message="Add independent native command namespace"
    ).sha
    scenario.write_manifest(revision=revision, plugin_paths=("context", "sibling"))
    user_command = scenario.claude / "commands" / "status.md"
    user_command.parent.mkdir(parents=True)
    user_command.write_bytes(b"# User-owned status\n")

    scenario.install()

    state = scenario.native_state()
    plugins = {item["id"]: item for item in state["plugins"]}
    assert set(plugins) == {
        "apm-command-context@apm-claude",
        "apm-command-sibling@apm-claude",
    }
    roots = {name: Path(plugin["readFromFolder"]) for name, plugin in plugins.items()}
    assert len(set(roots.values())) == 2
    for name, publisher in (
        ("apm-command-context@apm-claude", scenario.source),
        ("apm-command-sibling@apm-claude", sibling),
    ):
        assert plugins[name]["scope"] == "user"
        assert plugins[name]["enabled"] is False
        assert roots[name].is_relative_to(scenario.modules)
        for path in publisher.rglob("*"):
            if path.is_file():
                assert (roots[name] / path.relative_to(publisher)).read_bytes() == path.read_bytes()
    plugins["apm-command-sibling@apm-claude"]["enabled"] = True
    Path(scenario.environment["APM_NATIVE_TEST_STATE"]).write_text(
        json.dumps(state), encoding="utf-8"
    )
    ledger_path = scenario.modules / ".claude-plugin" / "apm-registration.json"
    ledger_before = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert set(ledger_before["plugins"]) == set(plugins)
    survivor = roots["apm-command-sibling@apm-claude"]
    survivor_bytes = {
        path.relative_to(survivor): path.read_bytes()
        for path in survivor.rglob("*")
        if path.is_file()
    }

    scenario.run("uninstall", "-g", f"{_DEPENDENCY}/plugins/context")
    scenario.run("install", "-g", "--target", "claude", "--only", "apm", "--frozen")

    remaining = scenario.native_state()
    assert remaining["plugins"] == [plugins["apm-command-sibling@apm-claude"]]
    assert not roots["apm-command-context@apm-claude"].exists()
    assert {
        path.relative_to(survivor): path.read_bytes()
        for path in survivor.rglob("*")
        if path.is_file()
    } == survivor_bytes
    lock = load_yaml(scenario.isolated.config_root / "apm.lock.yaml")
    assert len(lock["dependencies"]) == 1
    assert lock["dependencies"][0]["virtual_path"] == "plugins/sibling"
    assert lock["dependencies"][0]["resolved_commit"] == revision
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["plugins"] == {
        "apm-command-sibling@apm-claude": ledger_before["plugins"]["apm-command-sibling@apm-claude"]
    }
    assert [call for call in remaining["calls"] if call[:2] == ["plugin", "uninstall"]] == [
        ["plugin", "uninstall", "apm-command-context@apm-claude", "--scope", "user", "--keep-data"]
    ]
    assert all(
        call[:2] not in (["plugin", "enable"], ["plugin", "disable"]) for call in remaining["calls"]
    )
    assert user_command.read_bytes() == b"# User-owned status\n"
    assert sorted(path.name for path in user_command.parent.iterdir()) == ["status.md"]
    assert not (scenario.claude / "commands" / "unselected.md").exists()
    assert not (scenario.claude / "hooks").exists()


@pytest.mark.parametrize("empty", [False, True], ids=["selected-command", "explicit-empty"])
@pytest.mark.parametrize(
    "changed",
    [
        "resource",
        pytest.param(
            "executable",
            marks=pytest.mark.skipif(os.name == "nt", reason="Git executable bits require POSIX"),
        ),
    ],
)
def test_command_bundle_audit_refuses_drift_without_repair(
    tmp_path: Path, apm_engine_command: tuple[str, ...], empty: bool, changed: str
) -> None:
    """Audit attests all resources even when the manifest selects no commands."""
    scenario = _scenario(tmp_path, apm_engine_command, empty=empty)
    scenario.install()
    source = _assert_intact_native_source(scenario, scenario.revision, empty=empty)
    audit = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
    healthy = json.loads(scenario.run(*audit).stdout)
    assert all(row["passed"] for row in healthy["checks"])
    ledger = scenario.modules / ".claude-plugin" / "apm-registration.json"
    ledger_before = ledger.read_bytes()
    lock = scenario.isolated.config_root / "apm.lock.yaml"
    lock_before = lock.read_bytes()
    state_before = scenario.native_state()
    payload = source / (
        "__pycache__/command-resource.bin" if changed == "resource" else "scripts/context.mjs"
    )
    original = payload.read_bytes()
    mode = payload.stat().st_mode
    if changed == "resource":
        payload.write_bytes(b"MODIFIED_COMMAND_RESOURCE\n")
    else:
        payload.chmod(mode ^ stat.S_IXUSR)

    result = scenario.run(*audit, expected=1)

    assert "fingerprint changed" in result.stdout
    report = json.loads(result.stdout)
    assert [row["name"] for row in report["checks"] if not row["passed"]] == ["drift"]
    assert payload.read_bytes() == (
        b"MODIFIED_COMMAND_RESOURCE\n" if changed == "resource" else original
    )
    assert payload.stat().st_mode & stat.S_IXUSR == (
        mode & stat.S_IXUSR if changed == "resource" else (mode ^ stat.S_IXUSR) & stat.S_IXUSR
    )
    assert ledger.read_bytes() == ledger_before
    assert lock.read_bytes() == lock_before
    assert scenario.native_state() == state_before

    payload.write_bytes(original)
    payload.chmod(mode)
    restored = json.loads(scenario.run(*audit).stdout)
    assert all(row["passed"] for row in restored["checks"])
    assert ledger.read_bytes() == ledger_before
    assert lock.read_bytes() == lock_before
    assert scenario.native_state() == state_before


@pytest.mark.parametrize("empty", [False, True], ids=["selected-command", "explicit-empty"])
@pytest.mark.parametrize("enabled", [False, True], ids=["user-disabled", "user-enabled"])
def test_global_git_path_command_install_reinstall_and_empty_selection(
    tmp_path: Path, apm_engine_command: tuple[str, ...], empty: bool, enabled: bool
) -> None:
    """Normal and frozen reinstall retain user activation and an intact native unit."""
    scenario = _scenario(tmp_path, apm_engine_command, empty=empty)
    user_command = scenario.claude / "commands" / "personal.md"
    user_command.parent.mkdir(parents=True)
    user_command.write_bytes(b"# User-owned command\n")

    scenario.install()
    source = _assert_intact_native_source(scenario, scenario.revision, empty=empty)
    first_lock = (scenario.isolated.config_root / "apm.lock.yaml").read_bytes()
    first_manifest = (source / ".claude-plugin" / "plugin.json").read_bytes()
    state = scenario.native_state()
    state["plugins"][0]["enabled"] = enabled
    Path(scenario.environment["APM_NATIVE_TEST_STATE"]).write_text(
        json.dumps(state), encoding="utf-8"
    )
    scenario.install()
    assert (
        _assert_intact_native_source(scenario, scenario.revision, empty=empty, enabled=enabled)
        == source
    )
    scenario.run("install", "-g", "--target", "claude", "--only", "apm", "--frozen")

    assert (
        _assert_intact_native_source(scenario, scenario.revision, empty=empty, enabled=enabled)
        == source
    )
    assert (scenario.isolated.config_root / "apm.lock.yaml").read_bytes() == first_lock
    assert (source / ".claude-plugin" / "plugin.json").read_bytes() == first_manifest
    assert user_command.read_bytes() == b"# User-owned command\n"
    assert sorted(path.name for path in user_command.parent.iterdir()) == ["personal.md"]
    registration_calls = [
        call
        for call in scenario.native_state()["calls"]
        if call[:2] in (["plugin", "install"], ["plugin", "update"])
    ]
    assert registration_calls == [
        ["plugin", operation, "apm-command-context@apm-claude", "--scope", "user"]
        for operation in ("install", "update", "update")
    ]
    assert not any(
        call[:2] in (["plugin", "enable"], ["plugin", "disable"])
        for call in scenario.native_state()["calls"]
    )


@pytest.mark.parametrize("enabled", [False, True], ids=["user-disabled", "user-enabled"])
def test_global_git_path_command_update_and_owned_removal(
    tmp_path: Path, apm_engine_command: tuple[str, ...], enabled: bool
) -> None:
    """Ref replacement updates live resources and uninstall retains user command/data."""
    scenario = _scenario(tmp_path, apm_engine_command)
    scenario.install()
    _assert_intact_native_source(scenario, scenario.revision, empty=False)
    state = scenario.native_state()
    state["plugins"][0]["enabled"] = enabled
    Path(scenario.environment["APM_NATIVE_TEST_STATE"]).write_text(
        json.dumps(state), encoding="utf-8"
    )
    user_command = scenario.claude / "commands" / "personal.md"
    user_command.parent.mkdir(parents=True, exist_ok=True)
    user_command.write_bytes(b"# User-owned command\n")
    user_data = scenario.claude / "plugins" / "data" / "apm-command-context"
    user_data.mkdir(parents=True)
    (user_data / "notes.txt").write_bytes(b"USER_DATA\n")
    resource = scenario.source / "resources" / "identity.txt"
    resource.write_bytes(b"APM_NATIVE_COMMAND_UPDATED_RESOURCE\n")
    revision = scenario.factory.commit(
        scenario.repository, message="Advance owned command resource"
    ).sha
    scenario.write_manifest(revision=revision)

    scenario.install()

    source = _assert_intact_native_source(scenario, revision, empty=False, enabled=enabled)
    assert (source / "resources" / "identity.txt").read_bytes() == resource.read_bytes()
    scenario.run("uninstall", "-g", f"{_DEPENDENCY}/plugins/context")
    assert scenario.native_state()["plugins"] == []
    assert not source.exists()
    assert not (scenario.isolated.config_root / "apm.lock.yaml").exists()
    assert user_command.read_bytes() == b"# User-owned command\n"
    assert (user_data / "notes.txt").read_bytes() == b"USER_DATA\n"
    assert not (scenario.claude / "commands" / "status.md").exists()
