"""Real CLI install proof for Git-hook repository environment isolation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_marketplace_generic_https_credential_lifecycle import (
    _DENY_PROXY,
    _HELPER_PASSWORD,
    _add_credential_helper,
    _configure_git_https_fixture,
    _git_exec_path,
    _real_git,
    _verify_git_https_fixture,
    _write_credential_helper,
    _write_tls_certificate,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_SKILL_PATH = "skills/hook-proof"
_SKILL_BYTES = b"---\nname: hook-proof\ndescription: Git hook isolation proof\n---\n# Hook proof\n"


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "remote_url",
    (
        "https://github.com/acme/hook-proof",
        "https://git.example.com/acme/hook-proof",
    ),
    ids=("github", "generic-host"),
)
def test_apm_install_from_git_hook_preserves_invoking_worktree(
    tmp_path: Path,
    apm_binary_path: Path,
    remote_url: str,
) -> None:
    """A real install cannot redirect Git into the invoking worktree."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()

    package_factory = LocalPackageFactory(isolated.package_root)
    source = package_factory.create("hook-proof-source")
    package_factory.add_skill(source, "hook-proof", _SKILL_BYTES.decode("ascii"))
    marker = isolated.root / "dependency-hook-ran"
    hook = source.root / ".githooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n",
        encoding="ascii",
    )
    hook.chmod(0o755)
    subprocess.run(
        (
            "git",
            "config",
            "--file",
            environment["GIT_CONFIG_GLOBAL"],
            "core.hooksPath",
            ".githooks",
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("hook-proof", source_tree=source.root)
    repositories.commit(repository, message="seed hook isolation proof")
    child_env = repositories.url_rewrite_subprocess_env(repository, remote_url)

    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(
            {
                "git": remote_url,
                "path": _SKILL_PATH,
                "ref": "main",
            },
        ),
        targets=("copilot",),
    )
    _git(consumer.root, environment, "init", "--initial-branch=main")
    _git(consumer.root, environment, "config", "user.email", "test@example.com")
    _git(consumer.root, environment, "config", "user.name", "APM Test")
    _git(consumer.root, environment, "add", "apm.yml")
    _git(consumer.root, environment, "commit", "-m", "seed consumer")
    invoking_sha = _git(consumer.root, environment, "rev-parse", "HEAD")

    hook_worktree = isolated.work_root / "hook-worktree"
    _git(
        consumer.root,
        environment,
        "worktree",
        "add",
        "-b",
        "hook-wt",
        str(hook_worktree),
    )
    child_env["GIT_DIR"] = _git(hook_worktree, environment, "rev-parse", "--absolute-git-dir")
    child_env["GIT_WORK_TREE"] = str(hook_worktree)
    invoking_config = consumer.root / ".git" / "config"
    original_config = invoking_config.read_bytes()
    child_env["GIT_CONFIG"] = str(invoking_config)

    result = subprocess.run(
        (
            str(apm_binary_path),
            "install",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        cwd=hook_worktree,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert _git(hook_worktree, environment, "symbolic-ref", "--short", "HEAD") == "hook-wt"
    assert _git(hook_worktree, environment, "rev-parse", "HEAD") == invoking_sha
    assert invoking_config.read_bytes() == original_config
    deployed = hook_worktree / ".agents" / "skills" / "hook-proof" / "SKILL.md"
    assert deployed.read_bytes() == _SKILL_BYTES
    assert not marker.exists()


def test_generic_https_dependency_helper_receives_no_platform_credentials(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A real generic dependency uses its helper without platform credential bleed."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=dict(os.environ),
    )
    real_git = _real_git()
    helper_log = isolated.root / "credential-helper.json"
    helper = _write_credential_helper(real_git, isolated.home, helper_log)
    environment = isolated.subprocess_env()
    _add_credential_helper(
        real_git,
        Path(environment["GIT_CONFIG_GLOBAL"]),
        helper,
    )
    environment.update(
        {
            "ADO_APM_PAT": "ado-sentinel",
            "GH_TOKEN": "gh-sentinel",
            "GITHUB_APM_PAT": "github-apm-sentinel",
            "GITHUB_TOKEN": "github-sentinel",
            "GIT_HTTP_EXTRAHEADER": "Authorization: sentinel",
            "GIT_TOKEN": "git-sentinel",
            "APM_TEST_HELPER_LOG": str(helper_log),
            "GIT_ALLOW_PROTOCOL": "file:http:https",
            "ALL_PROXY": _DENY_PROXY,
            "HTTP_PROXY": _DENY_PROXY,
            "HTTPS_PROXY": _DENY_PROXY,
            "NO_PROXY": "",
            "all_proxy": _DENY_PROXY,
            "http_proxy": _DENY_PROXY,
            "https_proxy": _DENY_PROXY,
            "no_proxy": "",
        }
    )
    environment["GIT_EXEC_PATH"] = _git_exec_path(real_git)
    package_factory = LocalPackageFactory(isolated.package_root)
    source = package_factory.create("generic-source")
    package_factory.add_skill(
        source,
        "hook-proof",
        _SKILL_BYTES.decode("ascii"),
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("generic-dependency", source_tree=source.root)
    repositories.commit(repository, message="seed generic dependency")
    certificate, key = _write_tls_certificate(isolated.root)
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=real_git,
        env=environment,
    )

    with server_factory.start(
        (repository,),
        username="x-access-token",
        password=_HELPER_PASSWORD,
        private_repositories=(repository,),
        certfile=certificate,
        keyfile=key,
    ) as server:
        remote_url = f"{server.proxy_url}/acme/generic-dependency.git"
        local_url = server.remote_url(repository)
        _configure_git_https_fixture(
            real_git,
            remote_base_url=server.proxy_url,
            config_paths=(
                Path(environment["GIT_CONFIG_GLOBAL"]),
                isolated.home / ".gitconfig",
            ),
        )
        _verify_git_https_fixture(
            real_git,
            remote_url=local_url,
            environment=environment,
        )
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = f"url.{local_url}.insteadOf"
        environment["GIT_CONFIG_VALUE_0"] = remote_url
        consumer = LocalPackageFactory(isolated.work_root).create(
            "consumer",
            dependencies=(
                {
                    "git": remote_url,
                    "path": _SKILL_PATH,
                    "ref": "main",
                },
            ),
            targets=("copilot",),
        )
        result = subprocess.run(
            (
                str(apm_binary_path),
                "install",
                "--target",
                "copilot",
                "--no-policy",
                "--parallel-downloads",
                "0",
            ),
            cwd=consumer.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert helper_log.exists()
    observations = json.loads(helper_log.read_text(encoding="utf-8"))
    assert observations
    assert all(not names for names in observations)
