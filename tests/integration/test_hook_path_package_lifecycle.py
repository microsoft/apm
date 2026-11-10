"""Installed-CLI lifecycle for the existing manifestless Git-path hook layouts."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke, pytest.mark.requires_apm_binary]

_REMOTE = "https://gitlab.example.invalid/acme/hook-packages.git"
_SUBPATH = "hooks/markdown-lint"
_TARGETS = ("claude", "codex")
_CONFIGS = (PurePosixPath(".claude/settings.json"), PurePosixPath(".codex/hooks.json"))
_INSTALL = ("install", "--no-policy", "--parallel-downloads", "0")
_UPDATE = ("update", "--yes", "--parallel-downloads", "0")


def _run(
    runner: ApmLifecycleRunner,
    project: Path,
    environment: dict[str, str],
    args: tuple[str, ...],
    scenario: str,
    *,
    success: bool = True,
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario, cwd=project, env=environment)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    return result


def _snapshot(project: Path) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(project, targets=_TARGETS, config_paths=_CONFIGS)


def _source_bytes(installed: Path) -> dict[str, bytes]:
    assert installed.is_dir()
    return {
        path.relative_to(installed).as_posix(): path.read_bytes()
        for path in installed.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
def test_versioned_marketplace_hook_refresh_preserves_invalid_replacement(
    tmp_path: Path, apm_engine_command: tuple[str, ...], layout: str
) -> None:
    """Interpreted versioned selectors must stage replacements before publication."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("versioned-hook-marketplace")
    remote = "https://github.com/acme/versioned-hook-marketplace"
    virtual_path = "plugins/hook-bundle-1.2.3"
    descriptor = repository.worktree / virtual_path / layout / "lint.json"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python "${{CLAUDE_PLUGIN_ROOT}}/{layout}/run.py" v1',
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (descriptor.parent / "run.py").write_text(
        'from pathlib import Path\nPath("hook-ran.txt").write_text("unexpected")\n',
        encoding="utf-8",
    )
    (descriptor.parent / "resource.txt").write_text("hook resource", encoding="utf-8")
    catalog = repository.worktree / ".claude-plugin/marketplace.json"
    catalog.parent.mkdir()
    catalog.write_text(
        json.dumps(
            {
                "name": "hook-catalog",
                "owner": {"name": "Fixture"},
                "plugins": [
                    {
                        "name": "hook-bundle",
                        "source": {
                            "source": "github",
                            "repo": "acme/versioned-hook-marketplace",
                            "path": virtual_path,
                            "ref": "main",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories.commit(repository, message="seed versioned hook marketplace")
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("claude",))
    # Only the catalog transport changes; package validation and acquisition remain real.
    runner = ApmLifecycleRunner(
        (
            apm_engine_command[0],
            "-c",
            "from apm_cli.marketplace import client\n"
            "client._FETCHERS['github'] = client._fetch_git\n"
            "from apm_cli.cli import cli\ncli()\n",
        )
    )
    _run(
        runner,
        consumer.root,
        environment,
        ("marketplace", "add", remote, "--name", "hook-catalog", "--ref", "main"),
        "versioned-hook-register",
    )
    install = ("install", "hook-bundle@hook-catalog", "--target", "claude", "--no-policy")
    _run(runner, consumer.root, environment, install, "versioned-hook-install")
    ref = DependencyReference.parse_from_dict({"git": remote, "path": virtual_path, "ref": "main"})
    installed = ref.get_install_path(consumer.root / "apm_modules")
    assert (installed / layout / "lint.json").is_file()
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8"
    )
    repositories.commit(repository, message="advance versioned hook")
    _run(runner, consumer.root, environment, (*install, "--refresh"), "versioned-hook-refresh")
    assert "v2" in (installed / layout / "lint.json").read_text(encoding="utf-8")
    before_source = _source_bytes(installed)
    before_state = _snapshot(consumer.root)
    assert before_state.deployment_records
    assert any("hook-sidecar" in state.roles and state.content for state in before_state.files)
    descriptor.write_text("{", encoding="utf-8")
    repositories.commit(repository, message="invalidate versioned hook")
    _run(
        runner,
        consumer.root,
        environment,
        (*install, "--refresh"),
        "versioned-hook-reject-invalid",
        success=False,
    )
    assert _snapshot(consumer.root) == before_state
    assert _source_bytes(installed) == before_source
    assert not (consumer.root / "hook-ran.txt").exists()


def _assert_hooks(
    project: Path, marker: str, configs: tuple[PurePosixPath, ...] = _CONFIGS
) -> None:
    for relative in configs:
        payload = json.loads((project / relative).read_text(encoding="utf-8"))
        handlers = payload["hooks"]["PostToolUse"]
        assert len(handlers) == 1
        handler = handlers[0]
        assert handler["matcher"] == "Write"
        command = handler["hooks"][0]["command"]
        assert ".apm-resolution-staging" not in command
        script = command.removeprefix("python ").strip('"')
        script = script.replace("${CLAUDE_PROJECT_DIR}", str(project))
        script = script.replace("${CODEX_PROJECT_DIR}", str(project))
        script_path = Path(script)
        if not script_path.is_absolute():
            script_path = project / script_path
        assert script_path.is_file(), command
        assert marker in script_path.read_text(encoding="utf-8")
        assert (script_path.parent / "resource.txt").read_text(encoding="utf-8") == marker
    assert not (project / "hook-ran.txt").exists()
    assert not (project / ".github/hooks").exists()
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    assert len(lock.dependencies) == 1
    assert lock.deployment_ledger.records
    assert all(".apm-resolution-staging" not in key for key in lock.deployment_ledger.records)


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
def test_hook_path_install_restore_update_remove(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("hook-packages")
    package = repository.worktree / _SUBPATH
    hooks = package / layout
    hooks.mkdir(parents=True)
    descriptor = hooks / "lint.json"
    descriptor.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python "${{CLAUDE_PLUGIN_ROOT}}/{layout}/run.py"',
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    valid_descriptor = descriptor.read_bytes()
    script = hooks / "run.py"
    script.write_text(
        'from pathlib import Path\nPath("hook-ran.txt").write_text("v1")\n',
        encoding="utf-8",
    )
    resource = hooks / "resource.txt"
    resource.write_text("v1", encoding="utf-8")
    controls = repository.worktree / "controls"
    skill = controls / "skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: control-skill\ndescription: Valid non-hook control\n---\n# Control\n",
        encoding="utf-8",
    )
    arbitrary = controls / "arbitrary" / "notes.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("Not a package\n", encoding="utf-8")
    nested = controls / "nested" / "other" / "hooks" / "lint.json"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(valid_descriptor)
    malformed = controls / "malformed" / "hooks" / "lint.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{", encoding="utf-8")
    first = repositories.commit(repository, message="seed manifestless hooks")
    repositories.install_url_rewrite(repository, _REMOTE)
    dependency = {"git": _REMOTE, "type": "gitlab", "path": _SUBPATH, "ref": "main"}
    consumers = LocalPackageFactory(isolated.work_root)
    consumer = consumers.create("consumer", dependencies=(dependency,), targets=_TARGETS)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    ref = DependencyReference.parse_from_dict(dependency)
    installed = ref.get_install_path(consumer.root / "apm_modules")

    _run(runner, consumer.root, environment, _INSTALL, "hook-path-fresh")
    _assert_hooks(consumer.root, "v1")
    assert not (package / "apm.yml").exists()
    assert not (installed / "apm.yml").exists()
    assert not (installed / "SKILL.md").exists()
    initial_lock = (consumer.root / "apm.lock.yaml").read_bytes()
    initial_state = _snapshot(consumer.root)
    _run(runner, consumer.root, environment, _INSTALL, "hook-path-reinstall")
    assert _snapshot(consumer.root).semantic_bytes == initial_state.semantic_bytes

    script.write_text(script.read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8")
    resource.write_text("v2", encoding="utf-8")
    second = repositories.commit(repository, message="update hook and sibling resource")
    updated = _run(
        runner,
        consumer.root,
        environment,
        _UPDATE,
        "hook-path-update",
    )
    assert "Content hash mismatch" not in updated.stdout + updated.stderr
    _assert_hooks(consumer.root, "v2")
    assert first.sha != second.sha
    assert second.sha in (consumer.root / "apm.lock.yaml").read_text(encoding="utf-8")

    restored = consumers.create("restored", dependencies=(dependency,), targets=_TARGETS)
    (restored.root / "apm.lock.yaml").write_bytes(initial_lock)
    restored_install = ref.get_install_path(restored.root / "apm_modules")
    assert not restored_install.exists()
    _run(runner, restored.root, environment, _INSTALL, "hook-path-locked-restore")
    _assert_hooks(restored.root, "v1")
    assert first.sha in (restored.root / "apm.lock.yaml").read_text(encoding="utf-8")

    for name, replacement in (
        ("malformed-json", b"{"),
        ("invalid-grammar", b'{"hooks":{"PostToolUse":[{"command":42}]}}'),
        ("arbitrary-directory", None),
    ):
        before = _snapshot(consumer.root)
        source_before = _source_bytes(installed)
        if replacement is None:
            descriptor.unlink()
        else:
            descriptor.write_bytes(replacement)
        repositories.commit(repository, message=name)
        failed = _run(runner, consumer.root, environment, _UPDATE, name, success=False)
        assert "invalid" in (failed.stdout + failed.stderr).lower()
        after = _snapshot(consumer.root)
        assert after.lockfile_bytes == before.lockfile_bytes
        assert after.manifest_bytes == before.manifest_bytes
        assert after.deployment_records == before.deployment_records
        assert after.files == before.files
        assert installed.exists(), f"{name}: {failed.stdout}\n{failed.stderr}"
        assert _source_bytes(installed) == source_before
        _assert_hooks(consumer.root, "v2")

    descriptor.write_bytes(valid_descriptor)
    repositories.commit(repository, message="repair source")
    gated = consumers.create("gated", dependencies=(dependency,), targets=_TARGETS)
    manifest = load_yaml(gated.manifest_path)
    manifest["allowExecutables"] = {}
    dump_yaml(manifest, gated.manifest_path)
    _run(runner, gated.root, environment, _INSTALL, "hook-path-unapproved")
    assert all(not (gated.root / config).exists() for config in _CONFIGS)
    assert not (gated.root / "hook-ran.txt").exists()
    gated_lock = LockFile.read(gated.root / "apm.lock.yaml")
    assert next(iter(gated_lock.dependencies.values())).exec_status == "gated_pending_approval"
    _run(
        runner,
        gated.root,
        environment,
        ("approve", ref.get_unique_key()),
        "hook-path-approve",
    )
    _run(runner, gated.root, environment, _INSTALL, "hook-path-approved")
    _assert_hooks(gated.root, "v2")

    restricted = consumers.create(
        "restricted",
        dependencies=({**dependency, "targets": ["claude"]},),
        targets=_TARGETS,
    )
    _run(runner, restricted.root, environment, _INSTALL, "hook-path-restricted")
    _assert_hooks(restricted.root, "v2", (_CONFIGS[0],))
    assert not (restricted.root / ".codex/hooks.json").exists()

    _run(runner, consumer.root, environment, ("uninstall", ref.to_canonical()), "hook-path-remove")
    assert not installed.exists()
    assert not (consumer.root / "apm.lock.yaml").exists()
    for target in _TARGETS:
        assert not (consumer.root / f".{target}/hooks/markdown-lint").exists()
    for config in _CONFIGS:
        path = consumer.root / config
        if path.exists():
            assert not json.loads(path.read_text(encoding="utf-8")).get("hooks")
    assert not _snapshot(consumer.root).deployment_records

    for name in ("skill", "arbitrary", "nested", "malformed"):
        control_dependency = {**dependency, "path": f"controls/{name}"}
        control = consumers.create(
            f"control-{name}", dependencies=(control_dependency,), targets=("copilot",)
        )
        _run(
            runner,
            control.root,
            environment,
            _INSTALL,
            f"hook-path-control-{name}",
            success=name == "skill",
        )
        if name == "skill":
            assert (control.root / ".agents/skills/skill/SKILL.md").is_file()
        else:
            assert not (control.root / "apm.lock.yaml").exists()
            control_ref = DependencyReference.parse_from_dict(control_dependency)
            assert not control_ref.get_install_path(control.root / "apm_modules").exists()


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
@pytest.mark.parametrize("unselected_malformed", [False, True])
@pytest.mark.parametrize("selection_route", ["direct", "parent", "shared-child", "refresh"])
def test_selective_hook_update_preserves_unselected_package(
    tmp_path: Path,
    apm_binary_path: Path,
    layout: str,
    unselected_malformed: bool,
    selection_route: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "selection", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    if selection_route == "refresh":
        environment["GITLAB_HOST"] = "gitlab.example.invalid"
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("hook-packages")
    dependencies = []
    descriptors = []
    for name in ("selected", "unselected"):
        descriptor = repository.worktree / name / layout / "lint.json"
        descriptor.parent.mkdir(parents=True)
        descriptor.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f'python "${{CLAUDE_PLUGIN_ROOT}}/{layout}/run.py" {name}-v1',
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (descriptor.parent / "run.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        descriptors.append(descriptor)
        dependencies.append({"git": _REMOTE, "type": "gitlab", "path": name, "ref": "main"})
    selected, unselected = map(DependencyReference.parse_from_dict, dependencies)
    requested = selected
    if selection_route not in ("direct", "refresh"):
        parent = repository.worktree / "parent"
        parent.mkdir()
        dump_yaml(
            {"name": "parent", "version": "1.0.0", "dependencies": {"apm": [dependencies[0]]}},
            parent / "apm.yml",
        )
        parent_dependency = {**dependencies[0], "path": "parent"}
        if selection_route == "shared-child":
            dependencies.insert(1, parent_dependency)
        else:
            dependencies[0] = parent_dependency
        requested = DependencyReference.parse_from_dict(parent_dependency)
    update_args = (
        ("install", requested.to_canonical(), "--refresh", "--no-policy")
        if selection_route == "refresh"
        else ("update", requested.get_display_name(), "--yes")
    ) + ("--parallel-downloads", "0")
    repositories.commit(repository, message="seed two hook packages")
    repositories.install_url_rewrite(repository, _REMOTE)
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=tuple(dependencies), targets=("claude",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=120)
    _run(runner, consumer.root, environment, _INSTALL, "selection-install")
    installed = unselected.get_install_path(consumer.root / "apm_modules")
    source_before = _source_bytes(installed)
    before = LockFile.read(consumer.root / "apm.lock.yaml")
    locked_unselected = before.get_dependency(unselected.get_unique_key())
    unselected_before = locked_unselected.to_dict()
    ownership_before = {
        key: record
        for key, record in before.deployment_ledger.records.items()
        if unselected.get_unique_key() in record.owners
    }
    assert ownership_before
    files_before = {
        path: (consumer.root / path).read_bytes() for path in locked_unselected.deployed_files
    }
    descriptors[0].write_text(
        descriptors[0].read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8"
    )
    descriptors[1].write_text(
        "{" if unselected_malformed else descriptors[1].read_text().replace("v1", "v2"),
        encoding="utf-8",
    )
    advanced = repositories.commit(repository, message="advance both hook sources")
    _run(
        runner,
        consumer.root,
        environment,
        update_args,
        "selection-update",
    )
    after = LockFile.read(consumer.root / "apm.lock.yaml")
    assert after.get_dependency(selected.get_unique_key()).resolved_commit == advanced.sha
    assert after.get_dependency(unselected.get_unique_key()).to_dict() == unselected_before
    assert {
        key: record
        for key, record in after.deployment_ledger.records.items()
        if unselected.get_unique_key() in record.owners
    } == ownership_before
    assert installed.exists()
    assert _source_bytes(installed) == source_before
    assert {path: (consumer.root / path).read_bytes() for path in files_before} == files_before
    selected_path = selected.get_install_path(consumer.root / "apm_modules")
    selected_source = _source_bytes(selected_path)
    state_before = _snapshot(consumer.root)
    descriptors[0].write_text("{", encoding="utf-8")
    repositories.commit(repository, message="invalidate selected hook source")
    _run(
        runner,
        consumer.root,
        environment,
        update_args,
        "selection-invalid-update",
        success=False,
    )
    assert _snapshot(consumer.root).semantic_bytes == state_before.semantic_bytes
    assert _source_bytes(selected_path) == selected_source
    assert _source_bytes(installed) == source_before
