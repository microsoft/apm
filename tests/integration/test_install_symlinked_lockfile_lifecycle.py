"""Installed-CLI proof: a symlinked lockfile is refused before install deploys (#4168).

APM never writes a lockfile through a symbolic link. An install that would
rewrite one refuses before it deploys anything, at project and user scope,
so the manifest, the link, its target and the deployed tree stay
byte-identical.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]

_SKILL = "---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n"


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _tree(*roots: Path) -> dict[str, object]:
    state: dict[str, object] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if "cache" in path.relative_to(root).parts:
                continue
            if path.is_symlink():
                state[str(path)] = ("link", os.readlink(path))
            elif path.is_file():
                state[str(path)] = path.read_bytes()
            else:
                state[str(path)] = None
    return state


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_refuses_a_symlinked_lockfile_before_deploying(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / scope, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    first = sources.create("first-skill")
    sources.add_skill(first, "first-skill", _SKILL.format(name="first-skill"))
    second = sources.create("second-skill")
    sources.add_skill(second, "second-skill", _SKILL.format(name="second-skill"))
    if scope == "project":
        workspace = cwd = isolated.work_root / "consumer"
        workspace.mkdir()
        (workspace / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ntargets:\n- copilot\n", encoding="utf-8"
        )
        scope_args: tuple[str, ...] = ()
        roots = (workspace,)
    else:
        workspace = isolated.config_root
        cwd = isolated.work_root
        scope_args = ("--global", "--target", "copilot")
        roots = (isolated.home,)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def run(*args: str) -> CommandResult:
        return runner.run(args, cwd=cwd, env=env, scenario_id=f"symlink-{scope}-{args[0]}")

    installed = run("install", *scope_args, str(first.root))
    assert installed.returncode == 0, _evidence(installed)
    lock = workspace / "apm.lock.yaml"
    shared = isolated.root / "shared.lock.yaml"
    lock.replace(shared)
    lock.symlink_to(shared)
    before = _tree(*roots, shared.parent)

    refused = run("install", *scope_args, str(second.root))

    assert refused.returncode == 1, _evidence(refused)
    assert "symbolic link" in " ".join(refused.stdout.split()), _evidence(refused)
    assert lock.is_symlink() and os.readlink(lock) == str(shared)
    assert _tree(*roots, shared.parent) == before, _evidence(refused)
