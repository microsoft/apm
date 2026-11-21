"""APMLifecycle: an unreadable Git config refuses the install and names what failed.

PROMISE: URL rewrite safety stays fail closed when the effective Git config
cannot be read. The refusal shows Git's first diagnostic line with credentials
redacted, the config file Git reports, and a runnable command to inspect it,
and the failed install leaves apm.yml and the lockfile untouched (#4390).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-OWN-git-child-repository-isolation",
        "P-LAW-outcome.status_matches_state",
        "P-LAW-auth.credentials_isolated",
    ),
]

_TOKEN = "ghp_" + "Q7xR2mK9pL4vN8wT3yZ6bC1dF5gH0jS2aE4u"
_PIN = "0123456789abcdef0123456789abcdef01234567"
_MANIFEST = (
    "name: consumer\n"
    "version: 1.0.0\n"
    "targets:\n"
    "  - copilot\n"
    "dependencies:\n"
    "  apm:\n"
    "    - git: https://github.com/example/probe-dependency.git\n"
    f"      ref: {_PIN}\n"
)


def _squash(text: str) -> str:
    """Remove all whitespace so soft-wrapped paths and commands compare whole."""
    return "".join(text.split())


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _install(tmp_path: Path, apm_binary_path: Path, fault: str) -> tuple[CommandResult, Path, str]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(_MANIFEST, encoding="ascii")
    if fault == "unreadable-global-config":
        reported = isolated.work_root / "global-gitconfig"
        reported.mkdir()
        env["GIT_CONFIG_GLOBAL"] = str(reported)
        named = str(reported).replace("\\", "/")
    else:
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        env["GIT_CONFIG_COUNT"] = str(index + 1)
        env[f"GIT_CONFIG_KEY_{index}"] = f"https://x-access-token:{_TOKEN}@github.com/"
        env[f"GIT_CONFIG_VALUE_{index}"] = "https://github.com/"
        named = "invalid key"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    result = runner.run(("install", "--no-policy"), cwd=project, env=env)
    return result, project, named


@pytest.mark.parametrize("fault", ("unreadable-global-config", "credential-in-invalid-key"))
def test_unreadable_git_config_refuses_naming_the_fault_and_a_remedy(
    tmp_path: Path, apm_binary_path: Path, fault: str
) -> None:
    result, project, named = _install(tmp_path, apm_binary_path, fault)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode != 0, _evidence(result)
    assert "Unable to verify Git URL rewrite safety" in " ".join(output.split()), _evidence(result)
    assert _squash(named) in _squash(output).replace("\\", "/"), _evidence(result)
    assert _squash("'git config --show-origin --list'") in _squash(output), _evidence(result)
    assert _TOKEN not in _squash(output), _evidence(result)
    assert "x-access-token" not in output, _evidence(result)
    assert (project / "apm.yml").read_text(encoding="ascii") == _MANIFEST
    assert not (project / "apm.lock.yaml").exists()
