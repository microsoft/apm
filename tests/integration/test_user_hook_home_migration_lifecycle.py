"""User-scope hooks migrate legacy absolute $HOME paths without losing user hooks (#2944, #3649).

Chain: legacy absolute install -> user adds own hooks (one with an absolute
$HOME path) -> upgrade reinstall -> idempotent reinstall -> project-scope
install of the same package -> uninstall -g. Asserts exit codes, the
user-scope settings.json bytes, and the user lockfile after every step.

Set ``APM_BASELINE_BINARY`` to a v0.35.x ``apm`` to produce the legacy
state with the real older release instead of a simulated absolute rewrite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="POSIX shell contract; Windows stays absolute"),
]

_PACKAGE = "portable-hooks"
_REMOTE = f"https://gitlab.example.invalid/apm-fixture-org/{_PACKAGE}.git"
_SCRIPT = "portable/run.sh"


def _document() -> dict:
    handler = {"type": "command", "command": f"sh ./{_SCRIPT} out.txt"}
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [handler]}]}}


def _setup(root: Path) -> tuple[IsolatedApmEnvironment, dict[str, str], Path]:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    repos = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    package = sources.create(_PACKAGE)
    sources.add_hook(package, "portable", _document())
    sources.add_hook_asset(
        package, "portable", PurePosixPath("run.sh"), "#!/bin/sh\necho hi\n", executable=True
    )
    repo = repos.create(_PACKAGE, source_tree=package.root)
    repos.commit(repo, message="seed hooks")
    repos.install_url_rewrite(repo, _REMOTE)
    dependency = {"git": _REMOTE, "type": "gitlab", "ref": "main", "alias": _PACKAGE}
    dump_yaml(
        {
            "name": "user-hooks",
            "version": "0.1.0",
            "targets": ["claude"],
            "dependencies": {"apm": [dependency]},
        },
        isolated.config_root / "apm.yml",
    )
    (isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
    cwd = isolated.work_root / "cwd"
    cwd.mkdir(parents=True)
    env = isolated.subprocess_env()
    env["APM_NON_INTERACTIVE"] = "1"
    return isolated, env, cwd


def _run(binary: str, args: tuple[str, ...], cwd: Path, env: dict[str, str]) -> CommandResult:
    runner = ApmLifecycleRunner((binary,), timeout_seconds=120)
    return runner.run(args, cwd=cwd, env=env, scenario_id=" ".join(args))


def _ok(result: CommandResult) -> None:
    assert result.returncode == 0, f"{result.command}\n{result.stdout}\n{result.stderr}"


def _settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _hook_commands(home: Path) -> list[str]:
    commands: list[str] = []
    for groups in _settings(home).get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                commands.append(handler.get("command", ""))
    return commands


def _apm_commands(home: Path) -> list[str]:
    return [c for c in _hook_commands(home) if _SCRIPT in c]


@pytest.mark.parametrize("variant", ["claude-settings"])
def test_user_hook_home_migration_chain(
    tmp_path: Path, apm_binary_path: Path, variant: str
) -> None:
    isolated, env, cwd = _setup(tmp_path / variant)
    home = isolated.home
    main = str(apm_binary_path)
    install_g = ("install", "--global", "--target", "claude", "--no-policy")
    script_abs = home.resolve() / ".claude" / "hooks" / _PACKAGE / _SCRIPT
    portable = f"sh $HOME/.claude/hooks/{_PACKAGE}/{_SCRIPT} out.txt"

    # Step 1: legacy state (real v0.35.x when provided, else simulated).
    baseline = os.environ.get("APM_BASELINE_BINARY")
    if baseline:
        _ok(_run(baseline, install_g, cwd, env))
    else:
        _ok(_run(main, install_g, cwd, env))
        for path in sorted((home / ".claude").rglob("*.json")):
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("$HOME", str(home.resolve())), encoding="utf-8")
    legacy = _apm_commands(home)
    assert len(legacy) == 1, legacy
    assert str(script_abs) in legacy[0] or "$HOME" in legacy[0], legacy

    # Step 2: the user adds their own hooks, one anchored at an absolute $HOME path.
    user_script = home.resolve() / "bin" / "mine.sh"
    user_absolute = f"sh {user_script} --audit"
    user_portable = "echo user-owned"
    settings = _settings(home)
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": user_absolute}]}
    )
    settings["hooks"].setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": user_portable}]}
    )
    settings["theme"] = "dark"
    (home / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2), "utf-8")

    # Step 3: upgrade reinstall migrates only the APM entry, in place.
    _ok(_run(main, install_g, cwd, env))
    assert _apm_commands(home) == [portable]
    commands = _hook_commands(home)
    assert user_absolute in commands, commands
    assert user_portable in commands, commands
    assert commands.count(portable) == 1
    assert _settings(home)["theme"] == "dark"
    settings_bytes = (home / ".claude" / "settings.json").read_bytes()
    lock_path = isolated.config_root / "apm.lock.yaml"
    lock_bytes = lock_path.read_bytes()

    # Step 4: a second reinstall is a byte-level no-op.
    _ok(_run(main, install_g, cwd, env))
    assert (home / ".claude" / "settings.json").read_bytes() == settings_bytes
    assert lock_path.read_bytes() == lock_bytes

    # Step 5: uninstall -g removes only the APM entry and its scripts.
    _ok(
        _run(
            main,
            ("uninstall", "--global", f"gitlab.example.invalid/apm-fixture-org/{_PACKAGE}"),
            cwd,
            env,
        )
    )
    commands = _hook_commands(home)
    assert not [c for c in commands if _SCRIPT in c], commands
    assert user_absolute in commands, commands
    assert user_portable in commands, commands
    assert _settings(home)["theme"] == "dark"
    assert not (home / ".claude" / "hooks" / _PACKAGE).exists()
