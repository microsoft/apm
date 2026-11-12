"""Installed-CLI contract: user-scope hooks anchor to $HOME per documented harness shell (#2821)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(os.name == "nt", reason="POSIX shell contract; Windows stays absolute"),
]

_PACKAGE = "portable-hooks"
_SCRIPT = PurePosixPath("portable/run.sh")
_OUTPUT = "hook-output.txt"
_RELATIVE_COMMAND = f"sh ./{_SCRIPT.as_posix()} {_OUTPUT}"


@dataclass(frozen=True)
class _Harness:
    target: str
    config_root: str
    script_root: str
    document: dict
    verified: bool


def _nested(event: str) -> dict:
    handler = {"type": "command", "command": _RELATIVE_COMMAND}
    return {"hooks": {event: [{"matcher": "*", "hooks": [handler]}]}}


_HARNESSES = (
    _Harness("claude", ".claude", f".claude/hooks/{_PACKAGE}", _nested("PreToolUse"), True),
    _Harness(
        "copilot",
        ".copilot",
        f".copilot/hooks/scripts/{_PACKAGE}",
        {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": _RELATIVE_COMMAND}]}},
        True,
    ),
    _Harness("gemini", ".gemini", f".gemini/hooks/{_PACKAGE}", _nested("PreToolUse"), True),
    _Harness("kiro", ".kiro", f".kiro/hooks/{_PACKAGE}", _nested("PreToolUse"), True),
    _Harness(
        "cursor",
        ".cursor",
        f".cursor/hooks/{_PACKAGE}",
        {"version": 1, "hooks": {"preToolUse": [{"command": _RELATIVE_COMMAND}]}},
        False,
    ),
)


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    runner: ApmLifecycleRunner
    environment: dict[str, str]
    cwd: Path


def _scenario(root: Path, apm_binary_path: Path, harness: _Harness) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    package = sources.create(_PACKAGE)
    sources.add_hook(package, "portable", harness.document)
    sources.add_hook_asset(
        package,
        "portable",
        PurePosixPath("run.sh"),
        "#!/bin/sh\nprintf 'portable-hook\\n' > \"$1\"\n",
        executable=True,
    )
    repository = repositories.create(_PACKAGE, source_tree=package.root)
    repositories.commit(repository, message="seed portable hook package")
    remote = f"https://gitlab.example.invalid/apm-fixture-org/{_PACKAGE}.git"
    repositories.install_url_rewrite(repository, remote)
    dump_yaml(
        {
            "name": "portable-hook-consumer",
            "version": "0.1.0",
            "targets": [harness.target],
            "dependencies": {
                "apm": [{"git": remote, "type": "gitlab", "ref": "main", "alias": _PACKAGE}]
            },
        },
        isolated.config_root / "apm.yml",
    )
    (isolated.home / harness.config_root).mkdir(parents=True, exist_ok=True)
    cwd = isolated.work_root / "unrelated-cwd"
    cwd.mkdir(parents=True)
    return _Scenario(
        isolated=isolated,
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),), timeout_seconds=90, scenario_timeout_seconds=300
        ),
        environment=isolated.subprocess_env(),
        cwd=cwd,
    )


def _install(scenario: _Scenario, harness: _Harness, env: dict[str, str], label: str) -> None:
    result = scenario.runner.run(
        ("install", "--global", "--target", harness.target, "--no-policy"),
        scenario_id=f"{harness.target}-{label}",
        cwd=scenario.cwd,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"


def _config_files(home: Path, harness: _Harness) -> dict[str, bytes]:
    root = home / harness.config_root
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.json"))
    }


def _commands(home: Path, harness: _Harness) -> list[str]:
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and _SCRIPT.as_posix() in value:
            found.append(value)

    for name, content in _config_files(home, harness).items():
        if not name.endswith("apm-hooks.json"):
            walk(json.loads(content))
    return found


@pytest.mark.parametrize("harness", _HARNESSES, ids=[harness.target for harness in _HARNESSES])
def test_user_scope_hook_home_reference_lifecycle(
    tmp_path: Path, apm_binary_path: Path, harness: _Harness
) -> None:
    scenario = _scenario(tmp_path / harness.target, apm_binary_path, harness)
    home = scenario.isolated.home
    absolute = f"sh {(home / harness.script_root / _SCRIPT).resolve()} {_OUTPUT}"
    portable = f"sh $HOME/{harness.script_root}/{_SCRIPT.as_posix()} {_OUTPUT}"

    _install(scenario, harness, scenario.environment, "install")
    assert _commands(home, harness) == [portable if harness.verified else absolute]
    if not harness.verified:
        return

    hook = ApmLifecycleRunner(("sh", "-c"), timeout_seconds=30, scenario_timeout_seconds=30).run(
        (portable,),
        scenario_id=f"{harness.target}-execute",
        cwd=scenario.cwd,
        env=scenario.environment,
    )
    assert hook.returncode == 0, hook.stderr
    assert (scenario.cwd / _OUTPUT).read_text(encoding="utf-8") == "portable-hook\n"

    # A pre-#2821 install wrote absolute paths; reinstall must replace them in place.
    portable_files = _config_files(home, harness)
    for name, content in portable_files.items():
        legacy = content.decode("utf-8").replace("$HOME", str(home.resolve()))
        (home / harness.config_root / name).write_text(legacy, encoding="utf-8")
    assert _commands(home, harness) == [absolute]
    _install(scenario, harness, scenario.environment, "migrate")
    assert _commands(home, harness) == [portable]
    assert _config_files(home, harness) == portable_files

    # Dotfiles round trip: the same configs on a second home reinstall unchanged.
    home_b = tmp_path / f"{harness.target}-home-b"
    shutil.copytree(home, home_b, symlinks=True)
    env_b = dict(scenario.environment)
    env_b.update(HOME=str(home_b), USERPROFILE=str(home_b), APM_HOME=str(home_b / ".apm"))
    _install(scenario, harness, env_b, "second-home")
    assert _config_files(home_b, harness) == portable_files
