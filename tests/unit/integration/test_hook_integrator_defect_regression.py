"""Regression tests for hook integrator issue #1892 defects."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration import hook_integrator as hook_integrator_module
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.hook_ownership import dependency_hook_source_marker
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency.reference import DependencyReference


def _package_info(package_path: Path, name: str = "superpowers") -> PackageInfo:
    return PackageInfo(
        package=APMPackage(name=name, version="1.0.0", source=f"owner/{name}"),
        install_path=package_path,
    )


def _session_start_hook(command: str = "echo hook") -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _owned_hook_commands(path: Path) -> list[str]:
    """Return commands from the nested hook fixture shape."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        handler["command"]
        for entries in document.get("hooks", {}).values()
        for entry in entries
        for handler in entry.get("hooks", [])
    ]


def test_same_hook_in_both_dirs_integrates_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    (project / ".github").mkdir(parents=True)
    for hooks_dir in (package_path / ".apm" / "hooks", package_path / "hooks"):
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(_session_start_hook()),
            encoding="utf-8",
        )

    hook_files = HookIntegrator().find_hook_files(package_path)
    result = HookIntegrator().integrate_package_hooks_claude(_package_info(package_path), project)

    assert [path.relative_to(package_path).as_posix() for path in hook_files] == [
        ".apm/hooks/hooks.json"
    ]
    assert result.files_integrated == 1


def test_overlapping_dirs_different_hooks_both_integrate(tmp_path: Path) -> None:
    package_path = tmp_path / "superpowers"
    (package_path / ".apm" / "hooks").mkdir(parents=True)
    (package_path / "hooks").mkdir(parents=True)
    (package_path / ".apm" / "hooks" / "first.json").write_text(
        json.dumps(_session_start_hook("echo first")),
        encoding="utf-8",
    )
    (package_path / "hooks" / "second.json").write_text(
        json.dumps(_session_start_hook("echo second")),
        encoding="utf-8",
    )

    hook_files = HookIntegrator().find_hook_files(package_path)

    assert [path.relative_to(package_path).as_posix() for path in hook_files] == [
        ".apm/hooks/first.json",
        "hooks/second.json",
    ]


def test_copilot_hook_path_no_doubling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    (project / ".github").mkdir(parents=True)
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks-copilot.json").write_text(
        json.dumps({"hooks": {"sessionStart": [{"command": "./hooks/run-hook.cmd start"}]}}),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(
        _package_info(package_path),
        project,
    )

    output = (project / ".github" / "hooks" / "superpowers-hooks-copilot.json").read_text(
        encoding="utf-8"
    )
    data = json.loads(output)
    command = data["hooks"]["sessionStart"][0]["command"]
    assert command == ".github/hooks/scripts/superpowers/hooks/run-hook.cmd start"
    assert (
        project / ".github" / "hooks" / "scripts" / "superpowers" / "hooks" / "run-hook.cmd"
    ).exists()


def test_copilot_renames_claude_tool_events_to_camel_case(tmp_path: Path, caplog, capsys) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}],
                    "PostToolUse": [{"hooks": [{"type": "command", "command": "echo post"}]}],
                }
            }
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(_package_info(package_path), project)

    output = json.loads(
        (project / ".github" / "hooks" / "superpowers-hooks.json").read_text(encoding="utf-8")
    )
    assert set(output["hooks"]) == {"preToolUse", "postToolUse"}
    captured = capsys.readouterr()
    assert "may not be recognized" not in captured.out
    assert "hook event casing mismatch" not in caplog.text


def test_copilot_merges_duplicate_event_aliases_to_camel_case(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pascal"}]}],
                    "preToolUse": [{"hooks": [{"type": "command", "command": "echo camel"}]}],
                }
            }
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(_package_info(package_path), project)

    output = json.loads(
        (project / ".github" / "hooks" / "superpowers-hooks.json").read_text(encoding="utf-8")
    )
    assert set(output["hooks"]) == {"preToolUse"}
    commands = [entry["hooks"][0]["command"] for entry in output["hooks"]["preToolUse"]]
    assert commands == ["echo pascal", "echo camel"]


def test_claude_hook_script_path_no_doubling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("./hooks/run-hook.cmd start")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(_package_info(package_path), project)

    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == '"${CLAUDE_PROJECT_DIR}/.claude/hooks/superpowers/hooks/run-hook.cmd" start'
    assert (project / ".claude" / "hooks" / "superpowers" / "hooks" / "run-hook.cmd").exists()


def _setup_commonjs_hook_package(
    tmp_path: Path, target_manifest: str = "hooks.json"
) -> PackageInfo:
    package_path = tmp_path / "ponytail"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "lib").mkdir()
    (package_path / "package.json").write_text(
        json.dumps({"type": "commonjs"}),
        encoding="utf-8",
    )
    (hooks_dir / "ponytail.js").write_text(
        "const config = require('./ponytail-config');\n"
        "const helper = require('./lib/helper');\n"
        "console.log(config.message, helper.message);\n",
        encoding="utf-8",
    )
    (hooks_dir / "ponytail-config.js").write_text(
        "module.exports = { message: 'ok' };\n",
        encoding="utf-8",
    )
    (hooks_dir / "lib" / "helper.js").write_text(
        "module.exports = { message: 'nested' };\n",
        encoding="utf-8",
    )
    (hooks_dir / "lib" / "config.json").write_text(
        json.dumps({"message": "configuration"}),
        encoding="utf-8",
    )
    (hooks_dir / target_manifest).write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/ponytail.js session-start")),
        encoding="utf-8",
    )
    return _package_info(package_path, "ponytail")


def test_claude_deploys_hook_directory_siblings_and_package_module_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
    pkg_info = _setup_commonjs_hook_package(tmp_path)

    result = HookIntegrator().integrate_package_hooks_claude(pkg_info, project)

    deployed_script = project / ".claude" / "hooks" / "ponytail" / "hooks" / "ponytail.js"
    deployed_package_json = deployed_script.parent / "package.json"
    assert deployed_script.exists()
    assert (deployed_script.parent / "ponytail-config.js").exists()
    assert (deployed_script.parent / "lib" / "helper.js").exists()
    assert (deployed_script.parent / "lib" / "config.json").exists()
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}
    assert deployed_script in result.target_paths
    assert (deployed_script.parent / "ponytail-config.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "helper.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "config.json") in result.target_paths
    assert deployed_package_json in result.target_paths


def test_copilot_deploys_hook_directory_siblings_without_package_json_sidecar(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Copilot deployment must NOT write a package.json sidecar.

    Copilot's hook loader scans .github/hooks/ recursively for JSON hook
    descriptors.  A bare {"type": "..."} package.json is not a valid hook
    descriptor and causes Copilot to fail with "hooks: hooks must be an object".
    See: https://github.com/microsoft/apm/issues/2322
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
    pkg_info = _setup_commonjs_hook_package(tmp_path, "hooks-copilot.json")
    caplog.set_level(logging.DEBUG, logger="apm_cli.integration.hook_bundle")

    result = HookIntegrator().integrate_package_hooks(pkg_info, project)

    deployed_script = (
        project / ".github" / "hooks" / "scripts" / "ponytail" / "hooks" / "ponytail.js"
    )
    deployed_package_json = deployed_script.parent / "package.json"
    assert deployed_script.exists()
    assert (deployed_script.parent / "ponytail-config.js").exists()
    assert (deployed_script.parent / "lib" / "helper.js").exists()
    assert not (deployed_script.parent / "lib" / "config.json").exists()
    assert not deployed_package_json.exists(), (
        "package.json must not be deployed to Copilot hooks scripts dir -- "
        "Copilot scans .github/hooks/ recursively and rejects non-hook JSON files"
    )
    assert deployed_script in result.target_paths
    assert (deployed_script.parent / "ponytail-config.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "helper.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "config.json") not in result.target_paths
    assert deployed_package_json not in result.target_paths
    assert "Skipping JSON hook bundle asset" in caplog.text
    assert "config.json" in caplog.text


def test_copilot_does_not_deploy_package_json_sidecar_into_scanned_hooks_dir(
    tmp_path: Path,
) -> None:
    """Regression test for #2322.

    APM must not write a package.json sidecar into .github/hooks/scripts/ when
    deploying hooks for the Copilot target.  Copilot reads all JSON files under
    .github/hooks/ (recursively) and treats them as hook descriptors.  A
    {"type": "module"} or {"type": "commonjs"} file has no "hooks" key and
    causes Copilot to abort hook loading with:
        hooks: hooks must be an object
    """
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "myhooks"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (package_path / "package.json").write_text(
        json.dumps({"type": "commonjs"}),
        encoding="utf-8",
    )
    (hooks_dir / "validate.js").write_text("console.log('ok');\n", encoding="utf-8")
    (hooks_dir / "hooks-copilot.json").write_text(
        json.dumps(
            {"hooks": {"preToolUse": [{"bash": "${CLAUDE_PLUGIN_ROOT}/hooks/validate.js"}]}}
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(_package_info(package_path, "myhooks"), project)

    scripts_dir = project / ".github" / "hooks" / "scripts" / "myhooks" / "hooks"
    assert (scripts_dir / "validate.js").exists()
    assert not (scripts_dir / "package.json").exists(), (
        "package.json must not appear in .github/hooks/** -- "
        "Copilot scans this directory recursively and rejects non-hook JSON"
    )


def test_hook_bundle_defaults_sidecar_to_commonjs_without_package_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "defaulttype"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "entry.js").write_text("console.log('ok');\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/entry.js")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "defaulttype"), project
    )

    deployed_package_json = project / ".claude" / "hooks" / "defaulttype" / "hooks" / "package.json"
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}


def test_hook_bundle_does_not_deploy_root_hook_descriptor_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pkg_info = _setup_commonjs_hook_package(tmp_path)

    HookIntegrator().integrate_package_hooks_claude(pkg_info, project)

    deployed_root = project / ".claude" / "hooks" / "ponytail" / "hooks"
    assert not (deployed_root / "hooks.json").exists()


def test_hook_bundle_skips_package_sidecar_for_shell_only_hooks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "shellhooks"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/run.sh")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "shellhooks"), project
    )

    deployed_root = project / ".claude" / "hooks" / "shellhooks" / "hooks"
    assert (deployed_root / "run.sh").exists()
    assert not (deployed_root / "package.json").exists()


def test_hook_bundle_excludes_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "linkhooks"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside-secret.js"
    outside_file.write_text("module.exports = 'secret';\n", encoding="utf-8")
    (hooks_dir / "entry.js").write_text("require('./linked-secret');\n", encoding="utf-8")
    try:
        (hooks_dir / "linked-secret.js").symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/entry.js")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "linkhooks"), project
    )

    deployed_root = project / ".claude" / "hooks" / "linkhooks" / "hooks"
    assert (deployed_root / "entry.js").exists()
    assert not (deployed_root / "linked-secret.js").exists()


def test_hook_bundle_stale_sibling_removed_from_target_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pkg_info = _setup_commonjs_hook_package(tmp_path)
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks_claude(pkg_info, project)
    stale_sibling = project / ".claude" / "hooks" / "ponytail" / "hooks" / "ponytail-config.js"
    assert stale_sibling in result.target_paths
    managed_files = {path.relative_to(project).as_posix() for path in result.target_paths}

    sync_result = integrator.sync_integration(None, project, managed_files=managed_files)

    assert sync_result["files_removed"] >= 4
    assert not stale_sibling.exists()


def test_package_target_cleanup_uses_canonical_owner_not_shared_leaf(
    tmp_path: Path,
) -> None:
    """Restricting org-a/hooks must preserve org-b/hooks with the same leaf."""
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    integrator = HookIntegrator()
    packages: list[PackageInfo] = []
    for owner, command in (("org-a", "echo alpha"), ("org-b", "echo beta")):
        install_path = tmp_path / owner / "hooks"
        hooks_dir = install_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(_session_start_hook(command)),
            encoding="utf-8",
        )
        dependency = DependencyReference.parse(f"{owner}/hooks")
        package = PackageInfo(
            package=APMPackage(
                name="hooks",
                version="1.0.0",
                source=f"{owner}/hooks",
            ),
            install_path=install_path,
            dependency_ref=dependency,
        )
        packages.append(package)
        result = integrator.integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"],
            package,
            project,
        )
        assert result.files_integrated == 1

    sidecar = json.loads((project / ".cursor/apm-hooks.json").read_text(encoding="utf-8"))
    assert {entry["_apm_source"] for entries in sidecar.values() for entry in entries} == {
        "org-a/hooks",
        "org-b/hooks",
    }

    integrator.reconcile_package_target_restriction(
        packages[0],
        project,
        [KNOWN_TARGETS["cursor"]],
    )

    remaining_sidecar = json.loads((project / ".cursor/apm-hooks.json").read_text(encoding="utf-8"))
    assert {
        entry["_apm_source"] for entries in remaining_sidecar.values() for entry in entries
    } == {"org-b/hooks"}
    assert _owned_hook_commands(project / ".cursor/hooks.json") == ["echo beta"]


def test_transitive_local_hook_markers_include_anchored_parent_identity(
    tmp_path: Path,
) -> None:
    """Same-spelling local deps from different parents clean up independently."""
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    integrator = HookIntegrator()
    packages: list[PackageInfo] = []
    expected_markers: set[str] = set()
    for index, command in enumerate(("echo alpha", "echo beta"), start=1):
        dependency = DependencyReference.parse("../shared")
        dependency.declaring_parent = f"owner/parent-{index}"
        dependency.anchored_local_path = str(tmp_path / f"parent-{index}" / "shared")
        install_path = dependency.get_install_path(project / "apm_modules")
        hooks_dir = install_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(_session_start_hook(command)),
            encoding="utf-8",
        )
        package = PackageInfo(
            package=APMPackage(
                name="shared",
                version="1.0.0",
                source="../shared",
            ),
            install_path=install_path,
            dependency_ref=dependency,
        )
        packages.append(package)
        expected_markers.add(dependency_hook_source_marker(dependency))
        integrator.integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"],
            package,
            project,
        )

    sidecar_path = project / ".cursor/apm-hooks.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert {
        entry["_apm_source"] for entries in sidecar.values() for entry in entries
    } == expected_markers

    integrator.reconcile_package_target_restriction(
        packages[0],
        project,
        [KNOWN_TARGETS["cursor"]],
    )

    remaining = json.loads(sidecar_path.read_text(encoding="utf-8"))
    remaining_sources = {
        entry["_apm_source"] for entries in remaining.values() for entry in entries
    }
    assert len(remaining_sources) == 1
    assert remaining_sources < expected_markers
    assert _owned_hook_commands(project / ".cursor/hooks.json") == ["echo beta"]


def test_root_healing_preserves_canonical_dependency_marker_without_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-install fallback discovery recognizes canonical dependency owners."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: root-project\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    hook = _session_start_hook("echo shared")
    dependency_path = project / "apm_modules" / "owner" / "dependency"
    dependency_hooks = dependency_path / ".apm" / "hooks"
    dependency_hooks.mkdir(parents=True)
    (dependency_hooks / "hooks.json").write_text(
        json.dumps(hook),
        encoding="utf-8",
    )
    dependency = DependencyReference.parse("owner/dependency")
    dependency_info = PackageInfo(
        package=APMPackage(
            name="dependency",
            version="1.0.0",
            source="owner/dependency",
        ),
        install_path=dependency_path,
        dependency_ref=dependency,
    )
    root_hooks = project / ".apm" / "hooks"
    root_hooks.mkdir(parents=True)
    (root_hooks / "hooks.json").write_text(json.dumps(hook), encoding="utf-8")
    root_info = PackageInfo(
        package=APMPackage(
            name="root-project",
            version="1.0.0",
            package_path=project,
        ),
        install_path=project,
    )
    root_info.root_local_project_root = project
    integrator = HookIntegrator()

    original_dependency_sources = hook_integrator_module.dependency_hook_sources
    source_scans = 0

    def _count_dependency_sources(project_root: Path) -> set[str]:
        nonlocal source_scans
        source_scans += 1
        return original_dependency_sources(project_root)

    monkeypatch.setattr(
        hook_integrator_module,
        "dependency_hook_sources",
        _count_dependency_sources,
    )

    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["claude"],
        dependency_info,
        project,
    )
    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["claude"],
        root_info,
        project,
    )

    sidecar = json.loads((project / ".claude/apm-hooks.json").read_text(encoding="utf-8"))
    assert {entry["_apm_source"] for entries in sidecar.values() for entry in entries} == {
        "owner/dependency",
        "_local/root-project",
    }
    assert _owned_hook_commands(project / ".claude/settings.json") == [
        "echo shared",
        "echo shared",
    ]
    assert source_scans == 1


def test_unambiguous_legacy_leaf_marker_migrates_to_canonical_owner(
    tmp_path: Path,
) -> None:
    """A pre-upgrade leaf marker is replaced when the lock proves one owner."""
    project = tmp_path / "project"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    install_path = project / "apm_modules" / "org-a" / "hooks"
    hooks_dir = install_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    hook = _session_start_hook("echo alpha")
    (hooks_dir / "hooks.json").write_text(json.dumps(hook), encoding="utf-8")
    legacy_entry = _session_start_hook("echo legacy")["hooks"]["SessionStart"][0]
    (cursor / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"SessionStart": [legacy_entry]}}),
        encoding="utf-8",
    )
    sidecar_entry = dict(legacy_entry)
    sidecar_entry["_apm_source"] = "hooks"
    (cursor / "apm-hooks.json").write_text(
        json.dumps({"SessionStart": [sidecar_entry]}),
        encoding="utf-8",
    )
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="org-a/hooks"))
    lock.write(project / "apm.lock.yaml")
    dependency = DependencyReference.parse("org-a/hooks")
    package = PackageInfo(
        package=APMPackage(
            name="hooks",
            version="1.0.0",
            source="org-a/hooks",
        ),
        install_path=install_path,
        dependency_ref=dependency,
    )

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["cursor"],
        package,
        project,
    )

    assert result.files_integrated == 1
    sidecar = json.loads((cursor / "apm-hooks.json").read_text(encoding="utf-8"))
    assert {entry["_apm_source"] for entries in sidecar.values() for entry in entries} == {
        "org-a/hooks"
    }
    assert _owned_hook_commands(cursor / "hooks.json") == ["echo alpha"]


def test_reconcile_validates_survivor_targets_before_destructive_wipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed survivor leaves existing merged-hook bytes untouched."""
    project = tmp_path / "project"
    claude = project / ".claude"
    claude.mkdir(parents=True)
    config = claude / "settings.json"
    sidecar = claude / "apm-hooks.json"
    config_bytes = b'{"hooks": {"Stop": [{"hooks": []}]}}\n'
    sidecar_bytes = b'{"Stop": [{"hooks": [], "_apm_source": "owner/survivor"}]}\n'
    config.write_bytes(config_bytes)
    sidecar.write_bytes(sidecar_bytes)
    dependency = DependencyReference.parse("owner/survivor")
    survivor_path = project / "apm_modules" / "owner" / "survivor"
    survivor_path.mkdir(parents=True)
    (survivor_path / "apm.yml").write_text(
        "name: survivor\nversion: 1.0.0\ntarget: null\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "apm_cli.models.apm_package.surviving_dependency_refs_for_reintegration",
        lambda *args, **kwargs: [dependency],
    )
    root_package = APMPackage(
        name="consumer",
        version="1.0.0",
        canonical_targets=("claude",),
    )

    with pytest.raises(ValueError, match="Cannot validate surviving package"):
        HookIntegrator().reconcile_after_removal(root_package, project)

    assert config.read_bytes() == config_bytes
    assert sidecar.read_bytes() == sidecar_bytes
