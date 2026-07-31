"""Real-CLI lifecycle contract for JavaScript hook module sidecars."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_PACKAGE_NAME = "js-hook-kit"
_REMOTE_URL = f"https://gitlab.example.invalid/apm-fixture-org/{_PACKAGE_NAME}.git"
_DESCRIPTOR = PurePosixPath(f".github/hooks/{_PACKAGE_NAME}-esm.json")
_SCRIPT = PurePosixPath(f".github/hooks/scripts/{_PACKAGE_NAME}/esm/entry.mjs")
_NESTED_JSON = PurePosixPath(f".github/hooks/scripts/{_PACKAGE_NAME}/esm/config.json")
_FORBIDDEN_SIDECAR = PurePosixPath(f".github/hooks/scripts/{_PACKAGE_NAME}/package.json")
_CLAUDE_SIDECAR = PurePosixPath(f".claude/hooks/{_PACKAGE_NAME}/package.json")
_CLAUDE_SCRIPT = PurePosixPath(f".claude/hooks/{_PACKAGE_NAME}/esm/entry.mjs")
_CLAUDE_NESTED_JSON = PurePosixPath(f".claude/hooks/{_PACKAGE_NAME}/esm/config.json")
_USER_HOOK = PurePosixPath(".github/hooks/user-authored.json")
_HOOK_OUTPUT = PurePosixPath("hook-output.txt")
_GLOBAL_DESCRIPTOR = PurePosixPath(f"hooks/{_PACKAGE_NAME}-esm.json")
_GLOBAL_SCRIPT = PurePosixPath(f"hooks/scripts/{_PACKAGE_NAME}/esm/entry.mjs")
_GLOBAL_NESTED_JSON = PurePosixPath(f"hooks/scripts/{_PACKAGE_NAME}/esm/config.json")
_GLOBAL_FORBIDDEN_SIDECAR = PurePosixPath(f"hooks/scripts/{_PACKAGE_NAME}/package.json")
_GLOBAL_LOCK_FORBIDDEN_SIDECAR = PurePosixPath(".copilot") / _GLOBAL_FORBIDDEN_SIDECAR
_GLOBAL_USER_HOOK = PurePosixPath("hooks/user-authored.json")
_COPILOT_CAPTURE_PATHS = (
    _DESCRIPTOR,
    _SCRIPT,
    _NESTED_JSON,
    _FORBIDDEN_SIDECAR,
    _USER_HOOK,
    _HOOK_OUTPUT,
)


@dataclass(frozen=True)
class _Scenario:
    """One isolated installed-CLI lifecycle workspace."""

    isolated: IsolatedApmEnvironment
    sources: LocalPackageFactory
    consumers: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner


@dataclass(frozen=True)
class _PublishedPackage:
    """A package published through a production-shaped local Git remote."""

    package: LocalPackage
    repository: LocalGitRepository
    dependency: dict[str, object]
    environment: dict[str, str]


def _new_scenario(root: Path, apm_binary_path: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    return _Scenario(
        isolated=isolated,
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
    return {
        "hooks": {
            "preToolUse": [
                {
                    "bash": f"node ./esm/entry.mjs {_HOOK_OUTPUT.as_posix()}",
                }
            ]
        }
    }


def _script(marker: str) -> str:
    return (
        'import { writeFileSync } from "node:fs";\n'
        f'writeFileSync(process.argv[2], "{marker}\\n", {{ encoding: "utf8" }});\n'
    )


def _publish(scenario: _Scenario) -> _PublishedPackage:
    package = scenario.sources.create(_PACKAGE_NAME)
    scenario.sources.add_hook(package, "esm", _hook_document())
    scenario.sources.add_hook_asset(
        package,
        "esm",
        PurePosixPath("entry.mjs"),
        _script("esm-hook-v1"),
    )
    scenario.sources.add_hook_asset(
        package,
        "esm",
        PurePosixPath("config.json"),
        json.dumps({"message": "nested hook configuration"}) + "\n",
    )
    (package.root / "package.json").write_text(
        json.dumps({"type": "module"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    repository = scenario.repositories.create(_PACKAGE_NAME, source_tree=package.root)
    scenario.repositories.commit(repository, message="seed JavaScript hook package")
    scenario.repositories.install_url_rewrite(
        repository,
        _REMOTE_URL,
    )
    environment = scenario.isolated.subprocess_env()
    return _PublishedPackage(
        package=package,
        repository=repository,
        dependency={
            "git": _REMOTE_URL,
            "type": "gitlab",
            "ref": "main",
            "alias": _PACKAGE_NAME,
        },
        environment=environment,
    )


def _consumer(
    scenario: _Scenario,
    published: _PublishedPackage,
    *,
    name: str,
    targets: tuple[str, ...] = (),
) -> LocalPackage:
    consumer = scenario.consumers.create(
        name,
        dependencies=(published.dependency,),
        targets=targets,
    )
    user_hook = consumer.root.joinpath(*_USER_HOOK.parts)
    user_hook.parent.mkdir(parents=True)
    user_hook.write_text(
        json.dumps({"version": 1, "hooks": {}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return consumer


def _run_success(
    scenario: _Scenario,
    consumer: LocalPackage,
    args: tuple[str, ...],
    *,
    environment: dict[str, str],
    scenario_id: str,
) -> CommandResult:
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=consumer.root,
        env=environment,
    )
    assert result.returncode == 0, (
        f"scenario={scenario_id!r}\n"
        f"command={result.command!r}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    return result


def _capture_copilot(consumer: LocalPackage) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("copilot",),
        config_paths=_COPILOT_CAPTURE_PATHS,
    )


def _assert_exact_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _assert_scanned_json_is_hook_config(consumer: LocalPackage) -> None:
    hooks_root = consumer.root / ".github" / "hooks"
    discovered = sorted(hooks_root.rglob("*.json"))
    assert discovered
    for path in discovered:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload.get("hooks"), dict), path


def _deployed_command(consumer: LocalPackage) -> tuple[str, ...]:
    descriptor = consumer.root.joinpath(*_DESCRIPTOR.parts)
    payload = json.loads(descriptor.read_text(encoding="utf-8"))
    command = payload["hooks"]["preToolUse"][0]["bash"]
    assert command == (f"node {_SCRIPT.as_posix()} {_HOOK_OUTPUT.as_posix()}")
    return tuple(shlex.split(command))


def _execute_hook(
    scenario: _Scenario,
    consumer: LocalPackage,
    *,
    environment: dict[str, str],
    marker: str,
    scenario_id: str,
) -> None:
    command = _deployed_command(consumer)
    result = ApmLifecycleRunner(
        (command[0],),
        timeout_seconds=30,
        scenario_timeout_seconds=30,
    ).run(
        command[1:],
        scenario_id=scenario_id,
        cwd=consumer.root,
        env=environment,
    )
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert consumer.root.joinpath(*_HOOK_OUTPUT.parts).read_text(encoding="utf-8") == (
        f"{marker}\n"
    )


def _advance_hook(published: _PublishedPackage, scenario: _Scenario) -> None:
    script = published.repository.worktree / ".apm" / "hooks" / "esm" / "entry.mjs"
    script.write_text(_script("esm-hook-v2"), encoding="utf-8")
    scenario.repositories.commit(published.repository, message="update JavaScript hook")


def _seed_legacy_managed_sidecar(
    consumer: LocalPackage,
) -> LifecycleStateSnapshot:
    sidecar = consumer.root.joinpath(*_FORBIDDEN_SIDECAR.parts)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"type": "module"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lock_path = consumer.root / "apm.lock.yaml"
    lockfile = LockFile.read(lock_path)
    assert lockfile is not None
    dependency = next(
        dependency
        for dependency in lockfile.get_package_dependencies()
        if dependency.name == _PACKAGE_NAME
    )
    owner = dependency.get_unique_key()
    files = sorted({*dependency.deployed_files, _FORBIDDEN_SIDECAR.as_posix()})
    hashes = {
        **dependency.deployed_file_hashes,
        _FORBIDDEN_SIDECAR.as_posix(): compute_file_hash(sidecar),
    }
    DeploymentLedgerCodec.replace_legacy_owner(lockfile, owner, files, hashes)
    lockfile.write(lock_path)

    seeded = _capture_copilot(consumer)
    assert seeded.file(_FORBIDDEN_SIDECAR.as_posix()).kind == "file"
    assert "deployment" in seeded.file(_FORBIDDEN_SIDECAR.as_posix()).roles
    return seeded


def _capture_global(scenario: _Scenario) -> LifecycleStateSnapshot:
    copilot_root = scenario.isolated.home / ".copilot"
    return LifecycleStateSnapshot.capture(
        scenario.isolated.config_root,
        external_roots=(
            LifecycleStateRoot(
                root_id="copilot-user",
                target="copilot",
                scope="user",
                path=copilot_root,
                config_paths=(
                    _GLOBAL_DESCRIPTOR,
                    _GLOBAL_SCRIPT,
                    _GLOBAL_NESTED_JSON,
                    _GLOBAL_FORBIDDEN_SIDECAR,
                    _GLOBAL_USER_HOOK,
                ),
            ),
        ),
    )


def _seed_global_legacy_sidecar(scenario: _Scenario) -> LifecycleStateSnapshot:
    sidecar = scenario.isolated.home / ".copilot"
    sidecar = sidecar.joinpath(*_GLOBAL_FORBIDDEN_SIDECAR.parts)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"type": "module"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lock_path = scenario.isolated.config_root / "apm.lock.yaml"
    lockfile = LockFile.read(lock_path)
    assert lockfile is not None
    dependency = next(
        dependency
        for dependency in lockfile.get_package_dependencies()
        if dependency.name == _PACKAGE_NAME
    )
    owner = dependency.get_unique_key()
    files = sorted(
        {
            *dependency.deployed_files,
            _GLOBAL_LOCK_FORBIDDEN_SIDECAR.as_posix(),
        }
    )
    hashes = {
        **dependency.deployed_file_hashes,
        _GLOBAL_LOCK_FORBIDDEN_SIDECAR.as_posix(): compute_file_hash(sidecar),
    }
    DeploymentLedgerCodec.replace_legacy_owner(lockfile, owner, files, hashes)
    lockfile.write(lock_path)

    seeded = _capture_global(scenario)
    state = seeded.file(
        _GLOBAL_FORBIDDEN_SIDECAR.as_posix(),
        root_id="copilot-user",
    )
    assert state.kind == "file"
    assert _GLOBAL_LOCK_FORBIDDEN_SIDECAR.as_posix() in dependency.deployed_files
    return seeded


def _assert_copilot_contract(
    consumer: LocalPackage,
    *,
    expected_marker: str,
) -> None:
    snapshot = _capture_copilot(consumer)
    assert snapshot.file(_DESCRIPTOR.as_posix()).kind == "file"
    assert snapshot.file(_SCRIPT.as_posix()).kind == "file"
    assert snapshot.file(_NESTED_JSON.as_posix()).kind == "missing"
    assert snapshot.file(_FORBIDDEN_SIDECAR.as_posix()).kind == "missing"
    assert (
        snapshot.file(_USER_HOOK.as_posix()).content
        == (json.dumps({"version": 1, "hooks": {}}, sort_keys=True) + "\n").encode()
    )
    assert _FORBIDDEN_SIDECAR.as_posix() not in {
        record.locator.value for record in snapshot.deployment_records
    }
    assert _script(expected_marker).encode() == snapshot.file(_SCRIPT.as_posix()).content
    _assert_scanned_json_is_hook_config(consumer)


@pytest.mark.parametrize("target_selector", ["copilot", "vscode"])
def test_required_copilot_and_vscode_js_hook_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
    target_selector: str,
) -> None:
    """Install, update, replay, uninstall, and reinstall an executable ESM hook."""
    scenario = _new_scenario(tmp_path / target_selector, apm_binary_path)
    published = _publish(scenario)
    consumer = _consumer(
        scenario,
        published,
        name=f"{target_selector}-consumer",
    )
    manifest_bytes = consumer.manifest_path.read_bytes()
    install_args = (
        "install",
        "--target",
        target_selector,
        "--no-policy",
        "--parallel-downloads",
        "0",
    )

    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-install",
    )
    assert consumer.manifest_path.read_bytes() == manifest_bytes
    _assert_copilot_contract(consumer, expected_marker="esm-hook-v1")
    _execute_hook(
        scenario,
        consumer,
        environment=published.environment,
        marker="esm-hook-v1",
        scenario_id=f"{target_selector}-execute-v1",
    )
    _seed_legacy_managed_sidecar(consumer)

    _advance_hook(published, scenario)
    update_args = (
        "update",
        "--yes",
        "--target",
        target_selector,
        "--parallel-downloads",
        "0",
    )
    _run_success(
        scenario,
        consumer,
        update_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-update",
    )
    _assert_copilot_contract(consumer, expected_marker="esm-hook-v2")
    _execute_hook(
        scenario,
        consumer,
        environment=published.environment,
        marker="esm-hook-v2",
        scenario_id=f"{target_selector}-execute-v2",
    )
    after_update = _capture_copilot(consumer)

    _run_success(
        scenario,
        consumer,
        update_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-update-replay",
    )
    _assert_exact_state(after_update, _capture_copilot(consumer))
    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-install-replay",
    )
    _assert_exact_state(after_update, _capture_copilot(consumer))

    _seed_legacy_managed_sidecar(consumer)
    _run_success(
        scenario,
        consumer,
        ("uninstall", _REMOTE_URL),
        environment=published.environment,
        scenario_id=f"{target_selector}-uninstall",
    )
    removed = _capture_copilot(consumer)
    assert removed.file(_DESCRIPTOR.as_posix()).kind == "missing"
    assert removed.file(_SCRIPT.as_posix()).kind == "missing"
    assert removed.file(_FORBIDDEN_SIDECAR.as_posix()).kind == "missing"
    assert removed.file(_USER_HOOK.as_posix()).kind == "file"

    scenario.consumers.replace_apm_dependencies(consumer, (published.dependency,))
    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-reinstall",
    )
    _assert_copilot_contract(consumer, expected_marker="esm-hook-v2")
    _execute_hook(
        scenario,
        consumer,
        environment=published.environment,
        marker="esm-hook-v2",
        scenario_id=f"{target_selector}-execute-reinstalled",
    )
    after_reinstall = _capture_copilot(consumer)
    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id=f"{target_selector}-reinstall-replay",
    )
    _assert_exact_state(after_reinstall, _capture_copilot(consumer))


def test_required_target_contraction_removes_retained_sidecar(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A dropped target loses its managed sidecar without touching user hooks."""
    scenario = _new_scenario(tmp_path / "target-contraction", apm_binary_path)
    published = _publish(scenario)
    consumer = _consumer(
        scenario,
        published,
        name="target-contraction-consumer",
        targets=("copilot", "claude"),
    )

    _run_success(
        scenario,
        consumer,
        ("install", "--no-policy", "--parallel-downloads", "0"),
        environment=published.environment,
        scenario_id="target-contraction-install-both",
    )
    before = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("copilot", "claude"),
        config_paths=(
            *_COPILOT_CAPTURE_PATHS,
            _CLAUDE_SCRIPT,
            _CLAUDE_NESTED_JSON,
            _CLAUDE_SIDECAR,
        ),
    )
    assert before.file(_FORBIDDEN_SIDECAR.as_posix()).kind == "missing"
    assert before.file(_NESTED_JSON.as_posix()).kind == "missing"
    assert before.file(_CLAUDE_NESTED_JSON.as_posix()).kind == "file"
    assert json.loads(before.file(_CLAUDE_SIDECAR.as_posix()).content.decode()) == {
        "type": "module"
    }

    scenario.consumers.set_targets(consumer, ("copilot",))
    _run_success(
        scenario,
        consumer,
        ("install", "--no-policy", "--parallel-downloads", "0"),
        environment=published.environment,
        scenario_id="target-contraction-install-copilot",
    )
    contracted = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("copilot", "claude"),
        config_paths=(
            *_COPILOT_CAPTURE_PATHS,
            _CLAUDE_SCRIPT,
            _CLAUDE_NESTED_JSON,
            _CLAUDE_SIDECAR,
        ),
    )
    assert contracted.file(_CLAUDE_SCRIPT.as_posix()).kind == "missing"
    assert contracted.file(_CLAUDE_NESTED_JSON.as_posix()).kind == "missing"
    assert contracted.file(_CLAUDE_SIDECAR.as_posix()).kind == "missing"
    assert contracted.file(_FORBIDDEN_SIDECAR.as_posix()).kind == "missing"
    assert (
        contracted.file(_USER_HOOK.as_posix()).content == before.file(_USER_HOOK.as_posix()).content
    )

    _run_success(
        scenario,
        consumer,
        ("install", "--no-policy", "--parallel-downloads", "0"),
        environment=published.environment,
        scenario_id="target-contraction-replay",
    )
    _assert_exact_state(
        contracted,
        LifecycleStateSnapshot.capture(
            consumer.root,
            targets=("copilot", "claude"),
            config_paths=(
                *_COPILOT_CAPTURE_PATHS,
                _CLAUDE_SCRIPT,
                _CLAUDE_NESTED_JSON,
                _CLAUDE_SIDECAR,
            ),
        ),
    )


def test_required_user_edited_legacy_sidecar_is_preserved(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Cleanup must retain user-edited bytes at a formerly managed sidecar path."""
    scenario = _new_scenario(tmp_path / "user-sidecar", apm_binary_path)
    published = _publish(scenario)
    consumer = _consumer(
        scenario,
        published,
        name="user-sidecar-consumer",
        targets=("copilot",),
    )
    install_args = ("install", "--no-policy", "--parallel-downloads", "0")
    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id="user-sidecar-install",
    )
    seeded = _seed_legacy_managed_sidecar(consumer)
    original_hash = next(
        record.content_hash
        for record in seeded.deployment_records
        if record.locator.value == _FORBIDDEN_SIDECAR.as_posix()
    )
    sidecar = consumer.root.joinpath(*_FORBIDDEN_SIDECAR.parts)
    user_content = json.dumps({"version": 1, "hooks": {}}, sort_keys=True) + "\n"
    sidecar.write_text(user_content, encoding="utf-8")

    _run_success(
        scenario,
        consumer,
        install_args,
        environment=published.environment,
        scenario_id="user-sidecar-reinstall",
    )
    preserved = _capture_copilot(consumer)
    assert preserved.file(_FORBIDDEN_SIDECAR.as_posix()).content == user_content.encode()
    retained_record = next(
        record
        for record in preserved.deployment_records
        if record.locator.value == _FORBIDDEN_SIDECAR.as_posix()
    )
    assert retained_record.content_hash == original_hash
    _assert_scanned_json_is_hook_config(consumer)


def test_required_global_copilot_sidecar_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global Copilot hooks omit, clean, and never recreate the legacy sidecar."""
    scenario = _new_scenario(tmp_path / "global-copilot", apm_binary_path)
    published = _publish(scenario)
    cwd = scenario.consumers.create("global-copilot-cwd").root
    copilot_root = scenario.isolated.home / ".copilot"
    user_hook = copilot_root.joinpath(*_GLOBAL_USER_HOOK.parts)
    user_hook.parent.mkdir(parents=True)
    user_content = json.dumps({"version": 1, "hooks": {}}, sort_keys=True) + "\n"
    user_hook.write_text(user_content, encoding="utf-8")
    dump_yaml(
        {
            "name": "global-copilot-fixture",
            "version": "0.1.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [published.dependency]},
        },
        scenario.isolated.config_root / "apm.yml",
    )
    install_args = (
        "install",
        "--global",
        "--target",
        "copilot",
        "--no-policy",
        "--parallel-downloads",
        "0",
    )

    result = scenario.runner.run(
        install_args,
        scenario_id="global-copilot-install",
        cwd=cwd,
        env=published.environment,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    installed = _capture_global(scenario)
    assert (
        installed.file(
            _GLOBAL_DESCRIPTOR.as_posix(),
            root_id="copilot-user",
        ).kind
        == "file"
    )
    assert (
        installed.file(
            _GLOBAL_SCRIPT.as_posix(),
            root_id="copilot-user",
        ).kind
        == "file"
    )
    assert (
        installed.file(
            _GLOBAL_NESTED_JSON.as_posix(),
            root_id="copilot-user",
        ).kind
        == "missing"
    )
    assert (
        installed.file(
            _GLOBAL_FORBIDDEN_SIDECAR.as_posix(),
            root_id="copilot-user",
        ).kind
        == "missing"
    )
    assert (
        installed.file(
            _GLOBAL_USER_HOOK.as_posix(),
            root_id="copilot-user",
        ).content
        == user_content.encode()
    )

    descriptor = copilot_root.joinpath(*_GLOBAL_DESCRIPTOR.parts)
    payload = json.loads(descriptor.read_text(encoding="utf-8"))
    command = tuple(shlex.split(payload["hooks"]["preToolUse"][0]["bash"]))
    assert Path(command[1]).is_absolute()
    hook_result = ApmLifecycleRunner(
        (command[0],),
        timeout_seconds=30,
        scenario_timeout_seconds=30,
    ).run(
        command[1:],
        scenario_id="global-copilot-execute",
        cwd=cwd,
        env=published.environment,
    )
    assert hook_result.returncode == 0, hook_result.stderr
    assert (cwd / _HOOK_OUTPUT).read_text(encoding="utf-8") == "esm-hook-v1\n"

    _seed_global_legacy_sidecar(scenario)
    reinstall = scenario.runner.run(
        (
            "install",
            "--global",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="global-copilot-reinstall",
        cwd=cwd,
        env=published.environment,
    )
    assert reinstall.returncode == 0, f"stdout={reinstall.stdout!r}\nstderr={reinstall.stderr!r}"
    cleaned = _capture_global(scenario)
    assert (
        cleaned.file(
            _GLOBAL_FORBIDDEN_SIDECAR.as_posix(),
            root_id="copilot-user",
        ).kind
        == "missing"
    )
    assert (
        cleaned.file(
            _GLOBAL_USER_HOOK.as_posix(),
            root_id="copilot-user",
        ).content
        == user_content.encode()
    )
    lockfile = LockFile.read(scenario.isolated.config_root / "apm.lock.yaml")
    assert lockfile is not None
    assert _GLOBAL_LOCK_FORBIDDEN_SIDECAR.as_posix() not in {
        path
        for dependency in lockfile.get_package_dependencies()
        for path in dependency.deployed_files
    }
    assert _GLOBAL_LOCK_FORBIDDEN_SIDECAR.as_posix() not in {
        record.locator.value for record in lockfile.deployment_ledger.records.values()
    }

    replay = scenario.runner.run(
        (
            "install",
            "--global",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="global-copilot-reinstall-replay",
        cwd=cwd,
        env=published.environment,
    )
    assert replay.returncode == 0, f"stdout={replay.stdout!r}\nstderr={replay.stderr!r}"
    _assert_exact_state(cleaned, _capture_global(scenario))
