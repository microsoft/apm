"""Real, unmonkeypatched CLI install proof for ``url_rewrite_subprocess_env``.

Confirms the RED this factory method fixes: ``LocalGitRepositoryFactory.
install_url_rewrite`` writes its rewrite to an isolated ``GIT_CONFIG_GLOBAL``
file, but production ``GitAuthEnvBuilder.setup_environment`` intentionally
replaces ``GIT_CONFIG_GLOBAL`` with ``/dev/null`` for the auth-bearing
primary clone attempt -- silently defeating that fixture for any real
(non-monkeypatched) install. ``url_rewrite_subprocess_env`` instead injects
the rewrite as process-scoped ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_`` /
``GIT_CONFIG_VALUE_`` entries, which ``setup_environment`` never touches.

No ``GitHubPackageDownloader`` or Git method is patched anywhere in this
module: the install below drives the real production download/validation/
clone pipeline, under the :class:`IsolatedApmEnvironment` network guard
(which denies every ``AF_INET``/``AF_INET6`` socket), with only the git
transport rewritten to a local bare repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration]

_OWNER = "apm-fixture-org"
_REPO_NAME = "virtual-lifecycle-proof"
_SKILL_NAME = "auth"
_SKILL_PATH = "skills/security/auth"
_SKILL_BYTES = (
    f"---\nname: {_SKILL_NAME}\n"
    "description: Auth-hardened rewrite proof skill\n---\n# Authentication\n"
).encode()
_REMOTE_URL = f"https://github.com/{_OWNER}/{_REPO_NAME}"
_DEPENDENCY = f"{_OWNER}/{_REPO_NAME}/{_SKILL_PATH}#main"
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)


def test_url_rewrite_subprocess_env_reaches_owned_commit_through_real_cli_install(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A real ``apm install`` resolves the owned commit through the returned env."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()

    source = isolated.package_root / _REPO_NAME
    skill_source = source / _SKILL_PATH / "SKILL.md"
    skill_source.parent.mkdir(parents=True)
    skill_source.write_bytes(_SKILL_BYTES)
    assert not (source / "apm.yml").exists()

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create(_REPO_NAME, source_tree=source)
    commit = repositories.commit(repository, message="seed virtual lifecycle proof")

    child_env = repositories.url_rewrite_subprocess_env(repository, _REMOTE_URL)
    assert child_env["GIT_CONFIG_COUNT"] == "2"

    project = LocalPackageFactory(isolated.work_root).create(
        "virtual-lifecycle-proof-consumer",
        dependencies=(_DEPENDENCY,),
        targets=("copilot",),
    )

    result = subprocess.run(
        (str(apm_binary_path), *_INSTALL_ARGS),
        cwd=project.root,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    # No cross-protocol retry: the auth-bearing primary attempt must have
    # succeeded directly through the process-scoped rewrite, never falling
    # through to a later TransportPlan attempt.
    assert "Protocol fallback:" not in result.stdout
    assert "Protocol fallback:" not in result.stderr

    dependencies = load_yaml(project.root / "apm.lock.yaml")["dependencies"]
    assert len(dependencies) == 1
    locked = dependencies[0]
    assert locked["package_type"] == "claude_skill"
    assert locked["virtual_path"] == _SKILL_PATH
    assert locked["is_virtual"] is True
    assert locked["repo_url"] == f"{_OWNER}/{_REPO_NAME}"
    assert locked["host"] == "github.com"
    assert locked["resolved_ref"] == "main"
    assert locked["resolved_commit"] == commit.sha

    deployed_skill = project.root / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
    assert deployed_skill.read_bytes() == _SKILL_BYTES
