"""Regression tests for hook integrator issue #1892 and #1978 defects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


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
    assert command == ".claude/hooks/superpowers/hooks/run-hook.cmd start"
    assert (project / ".claude" / "hooks" / "superpowers" / "hooks" / "run-hook.cmd").exists()


# ---------------------------------------------------------------------------
# #1978 -- real_project_root must be used for ownership checks during replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_name,method_name",
    [
        ("claude", "integrate_package_hooks_claude"),
        ("codex", "integrate_package_hooks_codex"),
        ("cursor", "integrate_package_hooks_cursor"),
        ("gemini", "integrate_package_hooks_gemini"),
        ("windsurf", "integrate_package_hooks_windsurf"),
    ],
)
def test_real_project_root_fixes_source_marker_for_merge_targets(
    tmp_path: Path,
    target_name: str,
    method_name: str,
) -> None:
    """real_project_root causes _get_hook_source_marker to return _local/<name>.

    Reproduces the phantom-drift bug (#1978): when project_root is a scratch
    tmpdir but install_path is the real project, passing real_project_root
    equal to install_path restores the expected ``_local/<name>`` marker
    instead of the bare ``<name>`` that drifts against the on-disk file.
    """
    real_project = tmp_path / "myapp"
    real_project.mkdir()
    (real_project / "apm.yml").write_text(
        f"name: myapp\nversion: 0.0.0\ntargets:\n  - {target_name}\n",
        encoding="utf-8",
    )
    hooks_dir = real_project / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_payload = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo test"}],
                }
            ]
        }
    }
    (hooks_dir / "pre.json").write_text(json.dumps(hook_payload), encoding="utf-8")

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    # Seed target dir so require_dir check passes.
    target_dirs = {
        "claude": ".claude",
        "codex": ".codex",
        "cursor": ".cursor",
        "gemini": ".gemini",
        "windsurf": ".windsurf",
    }
    (scratch / target_dirs[target_name]).mkdir()

    pkg_info = PackageInfo(
        package=APMPackage(name="myapp", version="0.0.0", source="_local"),
        install_path=real_project,
    )

    integrator = HookIntegrator()
    # Without real_project_root the source marker would be bare "myapp";
    # _is_root_local_package sees install_path != scratch -> False.
    marker_wrong = HookIntegrator._get_hook_source_marker(pkg_info, scratch, "myapp")
    assert marker_wrong == "myapp", f"Pre-condition failed: got {marker_wrong!r}"

    # With real_project_root the marker must be "_local/myapp".
    marker_correct = HookIntegrator._get_hook_source_marker(pkg_info, real_project, "myapp")
    assert marker_correct == "_local/myapp", f"Marker with real root: {marker_correct!r}"

    # And _get_package_name with real_project_root must return "myapp" (not the
    # install_path.name fallback) because apm.yml is present and readable.
    pkg_name_real = integrator._get_package_name(pkg_info, real_project)
    assert pkg_name_real == "myapp"

    pkg_name_scratch = integrator._get_package_name(pkg_info, scratch)
    # Falls back to install_path.name when project_root != install_path.
    assert pkg_name_scratch == pkg_info.install_path.name


def test_get_hook_source_marker_uses_real_project_root_not_scratch(
    tmp_path: Path,
) -> None:
    """Direct unit check: _get_hook_source_marker uses project_root identity.

    Concretely verifies the pre/post state that caused phantom drift in #1978.
    """
    real_root = tmp_path / "repo"
    real_root.mkdir()
    (real_root / "apm.yml").write_text("name: myrepo\nversion: 0.0.0\n", encoding="utf-8")

    scratch = tmp_path / "apm_drift_123"
    scratch.mkdir()

    pkg = PackageInfo(
        package=APMPackage(name="myrepo", version="0.0.0", source="_local"),
        install_path=real_root,
    )

    # Bug scenario: project_root = scratch -> wrong marker.
    marker_bug = HookIntegrator._get_hook_source_marker(pkg, scratch, "myrepo")
    assert marker_bug == "myrepo"  # bare name, causes phantom drift

    # Fix: use real_root -> correct marker.
    marker_fix = HookIntegrator._get_hook_source_marker(pkg, real_root, "myrepo")
    assert marker_fix == "_local/myrepo"
