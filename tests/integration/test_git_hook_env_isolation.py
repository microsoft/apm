"""Regression tests for Git repository state inherited from hooks."""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from git import Repo

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
from apm_cli.deps.bare_cache import (
    bare_clone_with_fallback,
    clone_with_fallback,
    materialize_from_bare,
)
from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import AttemptSpec, _path_exists_in_tree_at_ref
from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.git_env import (
    GitUrlRewriteError,
    checkout_git_worktree,
    clone_git_worktree,
    get_git_executable,
    git_subprocess_env,
)
from tests.integration.test_marketplace_generic_https_credential_lifecycle import (
    _configure_git_https_fixture,
    _git_exec_path,
    _real_git,
    _write_tls_certificate,
)
from tests.utils.git_credential_sentinel import (
    credential_helper_trap_env,
    exercise_credential_helper,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = pytest.mark.component


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        [get_git_executable(), "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=git_subprocess_env(),
    )
    return result.stdout.strip()


def _commit(repo: Path, content: str, message: str) -> str:
    (repo / "payload.txt").write_text(content, encoding="ascii")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _https_git_fixture(
    tmp_path: Path,
) -> tuple[
    IsolatedApmEnvironment,
    dict[str, str],
    LocalGitRepositoryFactory,
    object,
    LocalGitHttpServerFactory,
    Path,
    Path,
]:
    """Create one repository and the material needed for a real HTTPS server."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "https-fixture", base_env=os.environ)
    environment = isolated.subprocess_env()
    environment["GIT_ALLOW_PROTOCOL"] = "file:http:https"
    environment["GIT_EXEC_PATH"] = _git_exec_path(_real_git())
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("auth-fence")
    (repository.worktree / "README.md").write_text("# Auth fence fixture\n", encoding="ascii")
    repositories.commit(repository, message="seed auth fence")
    certificate, key = _write_tls_certificate(isolated.root)
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=environment,
    )
    return (
        isolated,
        environment,
        repositories,
        repository,
        server_factory,
        certificate,
        key,
    )


def _add_git_config(config: Path, key: str, value: str) -> None:
    subprocess.run(
        (
            str(_real_git()),
            "config",
            "--file",
            str(config),
            "--add",
            key,
            value,
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def test_ado_clone_and_ref_resolution_preserve_caller_git_policy(tmp_path: Path) -> None:
    """Tokenless ADO keeps safe caller rewrites while suppressing native helpers."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "ado-policy", base_env=os.environ)
    environment = isolated.subprocess_env()
    environment["GIT_ALLOW_PROTOCOL"] = "file:https"
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("ado-policy")
    (repository.worktree / "README.md").write_text("# ADO policy fixture\n", encoding="ascii")
    commit = repositories.commit(repository, message="seed ADO policy fixture")
    remote_url = "https://dev.azure.com/org/project/_git/repo"
    caller_env = repositories.url_rewrite_subprocess_env(repository, remote_url)
    helper_root = tmp_path / "helper"
    helper_root.mkdir()
    helper_env, helper_marker = credential_helper_trap_env(helper_root)
    caller_env.update(
        {
            "APM_TEST_HELPER_MARKER": helper_env["APM_TEST_HELPER_MARKER"],
            "GIT_CONFIG_GLOBAL": helper_env["GIT_CONFIG_GLOBAL"],
        }
    )

    dep = DependencyReference.parse(f"{remote_url}#main")
    context = AuthContext(
        token=None,
        source="none",
        token_type="unknown",
        host_info=HostInfo(
            host="dev.azure.com",
            kind="ado",
            has_public_repos=False,
            api_base="https://dev.azure.com",
        ),
        git_env={},
    )
    host = GitHubPackageDownloader(auth_resolver=AuthResolver())
    host.git_env = caller_env
    host._resolve_dep_token = lambda _dep=None: None
    host._resolve_dep_auth_ctx = lambda _dep=None: context
    host._transport_selector.select = lambda **_kwargs: TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="plain HTTPS", use_token=False)],
        strict=True,
    )
    host._build_repo_url = lambda *_args, **_kwargs: remote_url

    clone_target = isolated.work_root / "clone"
    clone_envs: list[dict[str, str]] = []

    def clone_action(url: str, env: dict[str, str], target: Path) -> None:
        clone_envs.append(env)
        exercise_credential_helper(env, host="dev.azure.com", path="org/project/_git/repo")
        Repo.clone_from(url, target, env=env)

    CloneEngine(host).execute(
        dep.repo_url,
        clone_target,
        dep_ref=dep,
        clone_action=clone_action,
    )

    from apm_cli.utils.git_env import git_remote_refs as real_git_remote_refs

    ref_envs: list[dict[str, str]] = []

    def remote_refs_with_policy(url: str, *, env: dict[str, str], options=()):
        ref_envs.append(env)
        exercise_credential_helper(env, host="dev.azure.com", path="org/project/_git/repo")
        return real_git_remote_refs(url, env=env, options=options)

    with patch("apm_cli.utils.git_env.git_remote_refs", side_effect=remote_refs_with_policy):
        refs = host.list_remote_refs(dep)

    assert _git(clone_target, "rev-parse", "HEAD") == commit.sha
    assert {ref.commit_sha for ref in refs} == {commit.sha}
    assert clone_envs and ref_envs
    assert not helper_marker.exists()


def test_real_https_anonymous_fence_drops_injected_ambient_header(tmp_path: Path) -> None:
    """A malformed safe-looking extraHeader cannot inject Authorization."""
    (
        isolated,
        environment,
        _repositories,
        repository,
        server_factory,
        certificate,
        key,
    ) = _https_git_fixture(tmp_path)
    config = Path(environment["GIT_CONFIG_GLOBAL"])

    with server_factory.start(
        (repository,),
        password="unused",
        certfile=certificate,
        keyfile=key,
    ) as server:
        _configure_git_https_fixture(
            _real_git(),
            remote_base_url=server.proxy_url,
            config_paths=(config,),
        )
        _add_git_config(
            config,
            "http.extraHeader",
            "X-Apm-Safe: value\r\nAuthorization: Basic injected-secret",
        )
        clone_git_worktree(
            server.remote_url(repository),
            isolated.work_root / "anonymous-clone",
            env=environment,
        )
        observations = server.observations

    assert observations
    assert {observation.authorization for observation in observations} == {None}


def test_real_https_managed_fence_keeps_only_intended_authorization(tmp_path: Path) -> None:
    """Malformed ambient headers are dropped before managed auth is reattached."""
    (
        isolated,
        environment,
        _repositories,
        repository,
        server_factory,
        certificate,
        key,
    ) = _https_git_fixture(tmp_path)
    config = Path(environment["GIT_CONFIG_GLOBAL"])
    token = "managed-fixture-token"
    expected = "Basic " + base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode(
        "ascii"
    )

    with server_factory.start(
        (repository,),
        password=token,
        private_repositories=(repository,),
        certfile=certificate,
        keyfile=key,
    ) as server:
        _configure_git_https_fixture(
            _real_git(),
            remote_base_url=server.proxy_url,
            config_paths=(config,),
        )
        _add_git_config(
            config,
            "http.extraHeader",
            "X-Apm-Safe: value\nAuthorization: Basic injected-secret",
        )
        managed_env = AuthResolver._build_git_env(
            token,
            host_kind="github",
            base_env=environment,
        )
        clone_git_worktree(
            server.remote_url(repository),
            isolated.work_root / "managed-clone",
            env=managed_env,
        )
        observations = server.observations

    assert observations
    assert {observation.authorization for observation in observations} == {expected}


def test_real_https_managed_auth_rejects_cross_port_rewrite_before_request(
    tmp_path: Path,
) -> None:
    """A target-scoped empty header cannot hide a managed cross-port rewrite."""
    (
        isolated,
        environment,
        _repositories,
        repository,
        server_factory,
        certificate,
        key,
    ) = _https_git_fixture(tmp_path)
    second_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=environment,
    )
    config = Path(environment["GIT_CONFIG_GLOBAL"])
    token = "managed-cross-port-token"

    with (
        server_factory.start(
            (repository,),
            password=token,
            private_repositories=(repository,),
            certfile=certificate,
            keyfile=key,
        ) as source,
        second_factory.start(
            (repository,),
            password=token,
            private_repositories=(repository,),
            certfile=certificate,
            keyfile=key,
        ) as target,
    ):
        _configure_git_https_fixture(
            _real_git(),
            remote_base_url=source.proxy_url,
            config_paths=(config,),
        )
        _configure_git_https_fixture(
            _real_git(),
            remote_base_url=target.proxy_url,
            config_paths=(config,),
        )
        _add_git_config(
            config,
            f"url.{target.proxy_url}/.insteadOf",
            f"{source.proxy_url}/",
        )
        _add_git_config(
            config,
            f"http.{target.proxy_url}/.extraHeader",
            "",
        )
        managed_env = AuthResolver._build_git_env(
            token,
            host_kind="github",
            base_env=environment,
        )

        with pytest.raises(GitUrlRewriteError, match="different HTTPS origin"):
            clone_git_worktree(
                source.remote_url(repository),
                isolated.work_root / "cross-port-clone",
                env=managed_env,
            )

        assert source.observations == ()
        assert target.observations == ()


def test_cache_refresh_ignores_linked_worktree_git_environment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    _commit(source, "old\n", "old")

    cache = GitCache(tmp_path / "cache")
    bare_dir = cache._db_root / cache_shard_key(str(source))
    subprocess.run(
        [get_git_executable(), "clone", "--bare", str(source), str(bare_dir)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )
    dependency_sha = _commit(source, "new\n", "new")

    invoking = tmp_path / "invoking"
    invoking.mkdir()
    _git(invoking, "init")
    _git(invoking, "config", "user.email", "test@example.com")
    _git(invoking, "config", "user.name", "Test")
    invoking_sha = _commit(invoking, "invoking\n", "invoking")
    hook_worktree = tmp_path / "hook-worktree"
    _git(invoking, "worktree", "add", "-b", "hook-wt", str(hook_worktree))

    poisoned_env = dict(os.environ)
    poisoned_env["GIT_DIR"] = _git(hook_worktree, "rev-parse", "--absolute-git-dir")
    poisoned_env["GIT_WORK_TREE"] = str(hook_worktree)

    checkout = cache.get_checkout(
        str(source),
        dependency_sha,
        locked_sha=dependency_sha,
        env=poisoned_env,
    )

    assert _git(hook_worktree, "symbolic-ref", "--short", "HEAD") == "hook-wt"
    assert _git(hook_worktree, "rev-parse", "HEAD") == invoking_sha
    assert _git(checkout, "rev-parse", "HEAD") == dependency_sha


def test_fallback_checkout_targets_dependency_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    dependency_sha = _commit(source, "dependency\n", "dependency")

    target = tmp_path / "target"
    subprocess.run(
        [get_git_executable(), "clone", "--no-checkout", str(source), str(target)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )

    invoking = tmp_path / "invoking"
    invoking.mkdir()
    _git(invoking, "init")
    _git(invoking, "config", "user.email", "test@example.com")
    _git(invoking, "config", "user.name", "Test")
    invoking_sha = _commit(invoking, "invoking\n", "invoking")
    hook_worktree = tmp_path / "hook-worktree"
    _git(invoking, "worktree", "add", "-b", "hook-wt", str(hook_worktree))
    _git(hook_worktree, "fetch", str(source), dependency_sha)

    poisoned_env = dict(os.environ)
    poisoned_env["GIT_DIR"] = _git(hook_worktree, "rev-parse", "--absolute-git-dir")
    poisoned_env["GIT_WORK_TREE"] = str(hook_worktree)

    checkout_git_worktree(target, dependency_sha, env=poisoned_env)

    assert _git(target, "rev-parse", "HEAD") == dependency_sha
    assert _git(hook_worktree, "symbolic-ref", "--short", "HEAD") == "hook-wt"
    assert _git(hook_worktree, "rev-parse", "HEAD") == invoking_sha


def test_materialize_ignores_repository_local_git_config_override(tmp_path: Path) -> None:
    """GIT_CONFIG cannot redirect dependency configuration writes."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    dependency_sha = _commit(source, "dependency\n", "dependency")
    bare = tmp_path / "source.git"
    subprocess.run(
        [get_git_executable(), "clone", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )

    invoking = tmp_path / "invoking"
    invoking.mkdir()
    _git(invoking, "init")
    _git(invoking, "config", "user.email", "test@example.com")
    _git(invoking, "config", "user.name", "Test")
    invoking_config = invoking / ".git" / "config"
    original_config = invoking_config.read_bytes()
    poisoned_env = {
        **os.environ,
        "GIT_CONFIG": str(invoking_config),
    }

    materialize_from_bare(
        bare,
        tmp_path / "consumer",
        ref=None,
        env=poisoned_env,
        known_sha=dependency_sha,
    )

    assert invoking_config.read_bytes() == original_config


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX Git hook executable fixture")
def test_dependency_clone_disables_repository_checkout_hook(tmp_path: Path) -> None:
    """A dependency-controlled post-checkout hook cannot execute during clone."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    marker = tmp_path / "hook-ran"
    hook = source / ".githooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n",
        encoding="ascii",
    )
    hook.chmod(0o755)
    _commit(source, "dependency\n", "dependency with hook")
    _git(source, "add", ".githooks/post-checkout")
    _git(source, "commit", "-m", "add checkout hook")
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[core]\n\thooksPath = .githooks\n", encoding="ascii")

    clone_git_worktree(
        str(source),
        tmp_path / "consumer",
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )

    assert not marker.exists()


def test_dependency_clone_ignores_template_url_rewrite(tmp_path: Path) -> None:
    """Clone execution uses the same template-free config model as its probe."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    _commit(source, "dependency\n", "dependency")
    template = tmp_path / "template"
    template.mkdir()
    remote_url = source.as_uri()
    (template / "config").write_text(
        f'[url "http://127.0.0.1:9/"]\n\tinsteadOf = {remote_url}\n',
        encoding="ascii",
    )

    target = tmp_path / "consumer"
    clone_git_worktree(
        remote_url,
        target,
        env={
            "PATH": os.environ["PATH"],
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TEMPLATE_DIR": str(template),
        },
    )

    assert _git(target, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")


def test_full_clone_fallback_replaces_poisoned_process_environment(tmp_path: Path) -> None:
    """Working-tree clone must replace, not overlay, the Git child environment."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    dependency_sha = _commit(source, "dependency\n", "dependency")

    invoking = tmp_path / "invoking"
    invoking.mkdir()
    _git(invoking, "init")
    _git(invoking, "config", "user.email", "test@example.com")
    _git(invoking, "config", "user.name", "Test")
    invoking_sha = _commit(invoking, "invoking\n", "invoking")
    hook_worktree = tmp_path / "hook-worktree"
    _git(invoking, "worktree", "add", "-b", "hook-wt", str(hook_worktree))

    poisoned_env = dict(os.environ)
    poisoned_env["GIT_DIR"] = _git(hook_worktree, "rev-parse", "--absolute-git-dir")
    poisoned_env["GIT_WORK_TREE"] = str(hook_worktree)
    target = tmp_path / "dependency"

    def execute_transport_plan(
        repo_url: str,
        target_path: Path,
        *,
        clone_action: Callable[[str, dict[str, str], Path], None],
        **_kwargs: Any,
    ) -> None:
        clone_action(repo_url, poisoned_env, target_path)

    with patch.dict(os.environ, poisoned_env, clear=True):
        repo = clone_with_fallback(execute_transport_plan, str(source), target)
    repo.close()

    assert _git(target, "rev-parse", "HEAD") == dependency_sha
    assert _git(hook_worktree, "symbolic-ref", "--short", "HEAD") == "hook-wt"
    assert _git(hook_worktree, "rev-parse", "HEAD") == invoking_sha


def test_shallow_fetch_failure_reports_captured_git_stderr(tmp_path: Path) -> None:
    """A real failed Git fetch retains its actionable stderr."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    _commit(source, "dependency\n", "dependency")
    logs: list[str] = []
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)

    exists = _path_exists_in_tree_at_ref(
        downloader,
        DependencyReference(repo_url="owner/repo"),
        "skills/missing",
        "missing-ref",
        logs.append,
        AttemptSpec("local fixture", str(source), git_subprocess_env()),
    )

    assert exists is False
    assert any("couldn't find remote ref" in message for message in logs)


def test_shallow_fetch_failure_keeps_cause_and_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validation diagnostic keeps stderr without exposing URL credentials."""
    import apm_cli.deps.github_downloader_validation as validation

    real_run = subprocess.run
    token = "secret-sentinel"

    def fail_fetch(args, **kwargs):
        if "fetch" in args:
            raise subprocess.CalledProcessError(
                128,
                args,
                stderr=f"fatal: denied https://{token}@git.example.test/repo".encode(),
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(validation.subprocess, "run", fail_fetch)
    logs: list[str] = []
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)

    exists = _path_exists_in_tree_at_ref(
        downloader,
        DependencyReference(repo_url="owner/repo"),
        "skills/missing",
        "missing-ref",
        logs.append,
        AttemptSpec(
            "fixture",
            "https://git.example.test/repo",
            {
                "PATH": os.environ["PATH"],
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        ),
    )

    output = "\n".join(logs)
    assert exists is False
    assert "fatal: denied" in output
    assert token not in output


def test_shallow_tree_probe_ignores_remote_activated_template_rewrite(
    tmp_path: Path,
) -> None:
    """The validation fetch cannot activate template configuration after origin."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    _git(source, "branch", "-M", "main")
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Demo\n", encoding="ascii")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "seed")
    remote_url = source.as_uri()

    included = tmp_path / "included-gitconfig"
    included.write_text(
        f'[url "http://127.0.0.1:9/"]\n\tinsteadOf = {remote_url}\n',
        encoding="ascii",
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "config").write_text(
        f'[includeIf "hasconfig:remote.*.url:{remote_url}"]\n\tpath = {included.as_posix()}\n',
        encoding="ascii",
    )
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    logs: list[str] = []

    exists = _path_exists_in_tree_at_ref(
        downloader,
        DependencyReference(repo_url="owner/repo"),
        "skills/demo",
        "main",
        logs.append,
        AttemptSpec(
            "fixture",
            remote_url,
            {
                "PATH": os.environ["PATH"],
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_TEMPLATE_DIR": str(template),
            },
        ),
    )

    assert exists is True, logs


def test_shared_bare_clone_rejects_unsafe_effective_rewrite(tmp_path: Path) -> None:
    """The default shared-bare path cannot bypass URL rewrite validation."""
    remote_url = "https://git.example.test/org/repo"
    unsafe_env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.http://127.0.0.1:9/.insteadOf",
        "GIT_CONFIG_VALUE_0": remote_url,
    }

    def execute_transport_plan(
        repo_url: str,
        target_path: Path,
        *,
        clone_action: Callable[[str, dict[str, str], Path], None],
        **_kwargs: Any,
    ) -> None:
        clone_action(repo_url, unsafe_env, target_path)

    with pytest.raises(GitUrlRewriteError, match="insecure HTTP"):
        bare_clone_with_fallback(
            execute_transport_plan,
            remote_url,
            tmp_path / "bare.git",
            dep_ref=DependencyReference(repo_url="org/repo", host="git.example.test"),
            ref="main",
            is_commit_sha=False,
        )


def test_shared_bare_sha_rejection_leaves_no_tokenized_config(tmp_path: Path) -> None:
    """Rewrite rejection happens before a token-bearing remote is persisted."""
    token = "bare-config-token"
    remote_url = f"https://{token}@git.example.test/org/repo"
    unsafe_env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.ssh://git@mirror.example/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://",
    }
    target = tmp_path / "bare.git"

    def execute_transport_plan(
        repo_url: str,
        target_path: Path,
        *,
        clone_action: Callable[[str, dict[str, str], Path], None],
        **_kwargs: Any,
    ) -> None:
        clone_action(repo_url, unsafe_env, target_path)

    with pytest.raises(GitUrlRewriteError):
        bare_clone_with_fallback(
            execute_transport_plan,
            remote_url,
            target,
            dep_ref=DependencyReference(repo_url="org/repo", host="git.example.test"),
            ref="a" * 40,
            is_commit_sha=True,
        )

    assert not target.exists()


def test_git_cache_ls_remote_rejects_unsafe_effective_rewrite(tmp_path: Path) -> None:
    """Persistent-cache ref resolution uses the same rewrite-safety owner."""
    remote_url = "https://git.example.test/org/repo"
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.https://token@mirror.example/.insteadOf",
        "GIT_CONFIG_VALUE_0": remote_url,
    }

    with pytest.raises(GitUrlRewriteError, match="must not contain credentials"):
        GitCache(tmp_path / "cache")._ls_remote_resolve(remote_url, "main", env=env)


def test_git_cache_fetch_rejects_dependency_bare_local_rewrite(tmp_path: Path) -> None:
    """A cached repository's own config cannot activate an unsafe fetch rewrite."""
    remote_url = "https://git.example.test/org/repo"
    bare = tmp_path / "dependency.git"
    subprocess.run(
        [get_git_executable(), "init", "--bare", str(bare)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )
    subprocess.run(
        [
            get_git_executable(),
            "--git-dir",
            str(bare),
            "config",
            "url.http://127.0.0.1:9/.insteadOf",
            remote_url,
        ],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )

    with pytest.raises(GitUrlRewriteError, match="insecure HTTP"):
        GitCache(tmp_path / "cache")._fetch_into_bare_locked(
            bare,
            remote_url,
            "a" * 40,
            env={"PATH": os.environ["PATH"]},
        )


def test_git_cache_fetch_fallback_contacts_only_explicit_remote(tmp_path: Path) -> None:
    """A broadened SHA fetch cannot fan out to configured sibling remotes."""
    import apm_cli.cache.git_cache as git_cache_module

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    sha = _commit(source, "dependency\n", "dependency")
    source_bare = tmp_path / "source.git"
    subprocess.run(
        [get_git_executable(), "clone", "--bare", str(source), str(source_bare)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )

    cache_bare = tmp_path / "cache.git"
    subprocess.run(
        [get_git_executable(), "init", "--bare", str(cache_bare)],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )
    subprocess.run(
        [
            get_git_executable(),
            "--git-dir",
            str(cache_bare),
            "remote",
            "add",
            "unrelated",
            "http://127.0.0.1:9/unrelated.git",
        ],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )

    real_run = subprocess.run
    fetch_calls: list[list[str]] = []

    def fail_sha_fetch_once(args, **kwargs):
        if "fetch" in args:
            fetch_calls.append(list(args))
            if len(fetch_calls) == 1:
                raise subprocess.CalledProcessError(1, args)
        return real_run(args, **kwargs)

    with patch.object(git_cache_module.subprocess, "run", side_effect=fail_sha_fetch_once):
        GitCache(tmp_path / "cache")._fetch_into_bare_locked(
            cache_bare,
            source_bare.as_uri(),
            sha,
            env={
                "PATH": os.environ["PATH"],
                "GIT_ALLOW_PROTOCOL": "file",
            },
        )

    assert len(fetch_calls) == 2
    assert "--all" not in fetch_calls[1]
    assert source_bare.as_uri() in fetch_calls[1]
    assert (
        real_run(
            [get_git_executable(), "--git-dir", str(cache_bare), "cat-file", "-e", sha],
            check=False,
            capture_output=True,
            env=git_subprocess_env(),
        ).returncode
        == 0
    )
