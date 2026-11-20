"""APMLifecycle: org policy discovery selects the project's git remote (#4279).

PROMISE: req-pl-012 -- discovery uses ``origin`` when present, otherwise the
single remote; with several remotes and no ``origin`` it fails closed naming
the candidates; with no remote no discovery is attempted (source:
docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=install SRC=local SCOPE=project GATE=org-policy NET=policy-unreachable
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I8", "P-LAW-transaction.failed_command_preserves_state"),
]

_TARGET = "copilot"
_DEAD_PROXY = "http://127.0.0.1:9"
_INSTRUCTION = "---\napplyTo: '**'\ndescription: d\n---\nBody\n"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _project(tmp_path: Path, remotes: dict[str, str]):
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env[name] = _DEAD_PROXY
    for name in ("NO_PROXY", "no_proxy"):
        env.pop(name, None)
    packages = LocalPackageFactory(isolated.work_root)
    alpha = packages.create("alpha")
    packages.add_instruction(alpha, "alpha", _INSTRUCTION)
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, alpha)
    subprocess.run(("git", "init", "-q"), cwd=project.root, env=env, check=True)
    for name, url in remotes.items():
        subprocess.run(("git", "remote", "add", name, url), cwd=project.root, env=env, check=True)
    return env, project.root


@pytest.mark.req("req-pl-012")
def test_several_remotes_without_origin_fail_closed(tmp_path: Path, apm_binary_path: Path) -> None:
    env, root = _project(
        tmp_path,
        {"fork": "https://github.com/me/app.git", "upstream": "https://github.com/acme/app.git"},
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    before = LifecycleStateSnapshot.capture(root, targets=(_TARGET,))

    result = runner.run(("install",), cwd=root, env=env)

    assert result.returncode == 1, _evidence(result)
    flat = " ".join((result.stdout + "\n" + result.stderr).split())
    assert flat.isascii(), _evidence(result)
    assert "no 'origin' and several remotes (fork, upstream)" in flat, _evidence(result)
    assert not (root / ".github/instructions/alpha.instructions.md").exists()
    assert LifecycleStateSnapshot.capture(root, targets=(_TARGET,)) == before


@pytest.mark.req("req-pl-012")
def test_single_non_origin_remote_is_used_for_discovery(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    env, root = _project(tmp_path, {"upstream": "https://github.com/acme-remote-fixture/app.git"})
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(("install",), cwd=root, env=env)

    # The policy host is unreachable and the default is warn: the install
    # proceeds, and the warning names the org read from the single remote.
    assert result.returncode == 0, _evidence(result)
    flat = " ".join((result.stdout + "\n" + result.stderr).split())
    assert "Could not fetch org policy from org:acme-remote-fixture/" in flat, _evidence(result)
    assert (root / ".github/instructions/alpha.instructions.md").is_file()
