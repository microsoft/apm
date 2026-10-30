"""Real CLI lifecycle coverage for independent Claude user profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.lifecycle_smoke]


def _server(name: str, revision: int = 1) -> dict[str, Any]:
    return {
        "name": name,
        "registry": False,
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "fixture_server", str(revision)],
    }


def _read_servers(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})


def _scenario(tmp_path: Path) -> tuple[IsolatedApmEnvironment, Path, ApmLifecycleRunner]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "profiles", base_env=dict(os.environ))
    package = LocalPackageFactory(isolated.work_root).create(
        "claude-profile-consumer",
        mcp_dependencies=(_server("profile-server"),),
        targets=("claude",),
    )
    dump_yaml(load_yaml(package.manifest_path), isolated.config_root / "apm.yml")
    source = Path(__file__).resolve().parents[2] / "src"
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(source)!r}); from apm_cli.cli import cli; cli()",
        )
    )
    return isolated, package.root, runner


def _install(
    runner: ApmLifecycleRunner,
    isolated: IsolatedApmEnvironment,
    cwd: Path,
    config_dir: str,
) -> CommandResult:
    return runner.run(
        ("install", "--global", "--target", "claude", "--no-policy"),
        scenario_id="claude-profile-cleanup",
        cwd=cwd,
        env={**isolated.subprocess_env(), "CLAUDE_CONFIG_DIR": config_dir},
    )


@pytest.mark.parametrize("destination", ["custom", "default"])
def test_claude_user_profile_installation_is_independent_and_idempotent(
    tmp_path: Path, destination: str
) -> None:
    isolated, cwd, runner = _scenario(tmp_path)
    profiles = {
        "a": isolated.home if destination == "default" else isolated.root / "claude" / "a",
        "b": isolated.root / "claude" / "b",
    }

    def install(profile: str) -> CommandResult:
        config_dir = "" if destination == "default" and profile == "a" else str(profiles[profile])
        return _install(runner, isolated, cwd, config_dir)

    first_a = install("a")
    assert first_a.returncode == 0, first_a.stdout + first_a.stderr
    assert "profile-server" in _read_servers(profiles["a"] / ".claude.json")

    first_b = install("b")
    assert first_b.returncode == 0, first_b.stdout + first_b.stderr
    assert "profile-server" in _read_servers(profiles["b"] / ".claude.json")

    before_a = (profiles["a"] / ".claude.json").read_bytes()
    before_b = (profiles["b"] / ".claude.json").read_bytes()
    second_a = install("a")
    assert second_a.returncode == 0, second_a.stdout + second_a.stderr
    assert (profiles["a"] / ".claude.json").read_bytes() == before_a
    assert (profiles["b"] / ".claude.json").read_bytes() == before_b

    manifest = load_yaml(isolated.config_root / "apm.yml")
    manifest["dependencies"]["mcp"][0]["args"] = ["-m", "fixture_server", "2"]
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    update_a = install("a")
    assert update_a.returncode == 0, update_a.stdout + update_a.stderr
    assert _read_servers(profiles["a"] / ".claude.json")["profile-server"]["args"][-1] == "2"
    assert (profiles["b"] / ".claude.json").read_bytes() == before_b

    profile_a_path = profiles["a"] / ".claude.json"
    profile_a_config = json.loads(profile_a_path.read_text(encoding="utf-8"))
    profile_a_config["mcpServers"]["external-server"] = {"command": "external"}
    profile_a_config["theme"] = "preserve"
    profile_a_path.write_text(json.dumps(profile_a_config), encoding="utf-8")

    manifest["dependencies"]["mcp"] = []
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    remove_a = install("a")
    assert remove_a.returncode == 0, remove_a.stdout + remove_a.stderr
    assert _read_servers(profile_a_path) == {"external-server": {"command": "external"}}
    assert json.loads(profile_a_path.read_text(encoding="utf-8"))["theme"] == "preserve"
    assert (profiles["b"] / ".claude.json").read_bytes() == before_b


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "relative",
        pytest.param(
            "symlink-directory",
            marks=pytest.mark.skipif(os.name == "nt", reason="Symlinks require Windows privileges"),
        ),
        pytest.param(
            "symlink-file",
            marks=pytest.mark.skipif(os.name == "nt", reason="Symlinks require Windows privileges"),
        ),
    ],
)
def test_claude_profile_cleanup_refuses_unsafe_paths_without_changing_state(
    tmp_path: Path, unsafe_path: str
) -> None:
    isolated, cwd, runner = _scenario(tmp_path)
    profile = isolated.root / "claude" / "a"
    installed = _install(runner, isolated, cwd, str(profile))
    assert installed.returncode == 0, installed.stdout + installed.stderr
    config = profile / ".claude.json"
    config_before = config.read_bytes()
    lock_path = isolated.config_root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    if unsafe_path == "symlink-directory":
        alias = isolated.root / "selected-profile"
        alias.symlink_to(profile, target_is_directory=True)
        config_dir = str(alias)
    elif unsafe_path == "symlink-file":
        target = profile / "config-target.json"
        config.rename(target)
        config.symlink_to(target)
        config_dir = str(profile)
    else:
        config_dir = "relative-profile"

    manifest = load_yaml(isolated.config_root / "apm.yml")
    manifest["dependencies"]["mcp"] = []
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    removed = _install(runner, isolated, cwd, config_dir)
    assert removed.returncode != 0, removed.stdout + removed.stderr
    message = "must be an absolute path" if unsafe_path == "relative" else "symlinked MCP config"
    assert message in " ".join((removed.stdout + removed.stderr).split()), (
        removed.stdout + removed.stderr
    )
    assert config.read_bytes() == config_before
    assert lock_path.read_bytes() == lock_before


def _skills(root: Path) -> set[str]:
    """Return the names of skills deployed under one Claude profile root."""
    return {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}


@pytest.mark.parametrize("location", ["home", "outside-home"])
def test_claude_profile_install_keeps_files_deployed_to_other_profiles(
    tmp_path: Path, location: str
) -> None:
    """A -> B -> A -> B keeps both profiles; stale cleanup stays in the selected one (#3423)."""
    isolated, cwd, runner = _scenario(tmp_path)
    dump_yaml({"name": "skills-home", "version": "0.1.0"}, isolated.config_root / "apm.yml")
    factory = LocalPackageFactory(isolated.package_root)
    package = factory.create("profile-skills", targets=("claude",))
    factory.add_skill(package, "keep", "---\nname: keep\ndescription: Kept.\n---\nKeep.\n")
    renamed = factory.add_skill(
        package, "renamed", "---\nname: renamed\ndescription: Removed.\n---\nRemoved.\n"
    )
    default = isolated.home / ".claude"
    profile_b = (isolated.home if location == "home" else isolated.root) / "profiles" / "b"
    lock_path = isolated.config_root / "apm.lock.yaml"
    all_skills = {"keep", "renamed"}

    def install(root: Path) -> None:
        """Install the local package into the profile rooted at *root*."""
        result = runner.run(
            ("install", "--global", str(package.root), "--target", "claude", "--no-policy"),
            scenario_id="claude-profile-files",
            cwd=cwd,
            env={
                **isolated.subprocess_env(),
                "CLAUDE_CONFIG_DIR": "" if root == default else str(root),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

    install(default)
    assert _skills(default) == all_skills
    for root in (profile_b, default, profile_b):
        install(root)
        assert _skills(default) == all_skills
        assert _skills(profile_b) == all_skills

    lock_before = lock_path.read_bytes()
    install(profile_b)
    assert lock_path.read_bytes() == lock_before
    assert _skills(default) == all_skills
    assert _skills(profile_b) == all_skills

    # A skill dropped from the package is removed only from the selected
    # profile; the other profile is cleaned when it is installed into.
    renamed.unlink()
    renamed.parent.rmdir()
    install(profile_b)
    assert _skills(profile_b) == {"keep"}
    assert _skills(default) == all_skills
    install(default)
    assert _skills(default) == {"keep"}
    assert _skills(profile_b) == {"keep"}
