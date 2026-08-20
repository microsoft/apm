"""Installed-CLI lifecycle coverage for public GitHub anonymous-first auth."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.git_credential_shim import (
    GitCredentialShim,
    GitCredentialShimFactory,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import (
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
]

_PUBLIC_OWNER = "fixture-public"
_PRIVATE_OWNER = "fixture-private"
_PRIVATE_TOKEN = "fixture-private-token"
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--format",
    "json",
    "--output",
    "reports/audit.json",
)


def _real_git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git executable not available")
    return Path(executable).resolve()


def _skill_source(name: str) -> str:
    return f"---\nname: {name}\ndescription: Anonymous-first lifecycle fixture\n---\n# {name}\n"


def _child_environment(
    isolated: IsolatedApmEnvironment,
    shim: GitCredentialShim,
    *,
    proxy_url: str,
) -> dict[str, str]:
    """Route HTTPS probes into the rejecting loopback proxy for hermeticity."""
    environment = dict(shim.environment)
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "no_proxy",
    ):
        environment.pop(name, None)
    environment["HTTPS_PROXY"] = proxy_url
    environment["https_proxy"] = proxy_url
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment["GIT_ALLOW_PROTOCOL"] = "file:http:https"
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "credential.interactive"
    environment["GIT_CONFIG_VALUE_0"] = "never"
    assert environment["HOME"] == str(isolated.home)
    return environment


def _remote_events(shim: GitCredentialShim) -> list[dict[str, object]]:
    return [
        event for event in shim.events() if event.get("event") == "git" and event.get("remotes")
    ]


def _assert_anonymous_remote_event(event: dict[str, object]) -> None:
    assert event["github_token_env"] == []
    assert event["git_token_present"] is False
    assert event["auth_config_present"] is False
    assert event["credential_helpers"] == [""]
    assert event["credential_interactive"] in ([], ["never"])
    remotes = event["remotes"]
    assert isinstance(remotes, list)
    assert remotes
    assert all(remote["authenticated_url"] is False for remote in remotes)


def _create_public_graph(
    isolated: IsolatedApmEnvironment,
    *,
    environment: dict[str, str],
) -> tuple[
    LocalGitRepositoryFactory,
    tuple[LocalGitRepository, ...],
    Path,
]:
    packages = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )

    leaf = packages.create("public-leaf", targets=("copilot",))
    packages.add_skill(leaf, "public-leaf", _skill_source("public-leaf"))
    leaf_repository = repositories.create("public-leaf", source_tree=leaf.root)
    repositories.commit(leaf_repository, message="seed public leaf")

    parent = packages.create(
        "public-parent",
        dependencies=(f"{_PUBLIC_OWNER}/public-leaf#main",),
        targets=("copilot",),
    )
    packages.add_skill(parent, "public-parent", _skill_source("public-parent"))
    parent_repository = repositories.create("public-parent", source_tree=parent.root)
    repositories.commit(parent_repository, message="seed public parent")

    virtuals = packages.create("public-virtuals", targets=("copilot",))
    packages.add_skill(virtuals, "virtual-alpha", _skill_source("virtual-alpha"))
    packages.add_skill(virtuals, "virtual-beta", _skill_source("virtual-beta"))
    virtuals_repository = repositories.create(
        "public-virtuals",
        source_tree=virtuals.root,
    )
    repositories.commit(virtuals_repository, message="seed public virtuals")

    consumer = LocalPackageFactory(isolated.work_root).create(
        "public-consumer",
        dependencies=(
            f"{_PUBLIC_OWNER}/public-parent#main",
            f"{_PUBLIC_OWNER}/public-virtuals/skills/virtual-alpha#main",
            f"{_PUBLIC_OWNER}/public-virtuals/skills/virtual-beta#main",
        ),
        targets=("copilot",),
    )
    return (
        repositories,
        (leaf_repository, parent_repository, virtuals_repository),
        consumer.root,
    )


def test_all_public_graph_lifecycle_never_resolves_or_leaks_credentials(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install, repeat, update, and audit remain credential-free."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "public-scenario",
        base_env={
            **os.environ,
            "GITHUB_APM_PAT": "ambient-pat-must-be-stripped",
            "GITHUB_TOKEN": "ambient-actions-token-must-be-stripped",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Bearer ambient-header",
            "GIT_CONFIG_KEY_1": "credential.interactive",
            "GIT_CONFIG_VALUE_1": "never",
        },
    )
    base_environment = isolated.subprocess_env()
    _repositories, graph, project_root = _create_public_graph(
        isolated,
        environment=base_environment,
    )
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=base_environment,
    )

    with server_factory.start(graph, password=_PRIVATE_TOKEN) as server:
        remote_map = {
            f"{_PUBLIC_OWNER}/{repository.origin.stem}": server.remote_url(repository)
            for repository in graph
        }
        shim = GitCredentialShimFactory(isolated.root / "shims").create(
            base_env=base_environment,
            real_git=_real_git(),
            remote_map=remote_map,
            credential=_PRIVATE_TOKEN,
        )
        environment = _child_environment(
            isolated,
            shim,
            proxy_url=server.proxy_url,
        )
        results = ApmLifecycleRunner(
            (str(apm_binary_path),),
            scenario_timeout_seconds=360,
        ).run_sequence(
            (
                _INSTALL_ARGS,
                _INSTALL_ARGS,
                ("update", "--yes", "--target", "copilot"),
                _AUDIT_ARGS,
            ),
            expected_returncodes=(0, 0, 0, 0),
            scenario_id="public-github-anonymous-lifecycle",
            cwd=project_root,
            env=environment,
        )

        assert len(results) == 4
        assert (project_root / ".agents/skills/public-parent/SKILL.md").is_file()
        assert (project_root / ".agents/skills/public-leaf/SKILL.md").is_file()
        assert (project_root / ".agents/skills/virtual-alpha/SKILL.md").is_file()
        assert (project_root / ".agents/skills/virtual-beta/SKILL.md").is_file()
        assert (project_root / "reports/audit.json").is_file()

        events = shim.events()
        assert [event for event in events if event.get("event") == "credential-fill"] == []
        assert [event for event in events if event.get("event") == "gh"] == []
        remote_events = _remote_events(shim)
        assert remote_events
        for event in remote_events:
            _assert_anonymous_remote_event(event)

        repository_requests = [
            observation
            for observation in server.observations
            if any(repository.origin.name in observation.path for repository in graph)
        ]
        assert repository_requests
        assert all(not observation.authorization_present for observation in repository_requests)


def test_private_github_fallback_resolves_once_and_completes_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One private dependency uses one path-scoped credential fallback."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "private-scenario",
        base_env=dict(os.environ),
    )
    base_environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("private-package", targets=("copilot",))
    packages.add_skill(source, "private-package", _skill_source("private-package"))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=base_environment,
    )
    repository = repositories.create("private-package", source_tree=source.root)
    repositories.commit(repository, message="seed private package")
    project = LocalPackageFactory(isolated.work_root).create(
        "private-consumer",
        dependencies=(f"{_PRIVATE_OWNER}/private-package#main",),
        targets=("copilot",),
    )

    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=base_environment,
    )
    with server_factory.start(
        (repository,),
        private_repositories=(repository,),
        password=_PRIVATE_TOKEN,
    ) as server:
        shim = GitCredentialShimFactory(isolated.root / "shims").create(
            base_env=base_environment,
            real_git=_real_git(),
            remote_map={
                f"{_PRIVATE_OWNER}/private-package": server.remote_url(repository),
            },
            credential=_PRIVATE_TOKEN,
        )
        environment = _child_environment(
            isolated,
            shim,
            proxy_url=server.proxy_url,
        )
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            (*_INSTALL_ARGS, "--verbose"),
            scenario_id="private-github-scoped-fallback",
            cwd=project.root,
            env=environment,
        )

        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}\n"
            f"shim_events={shim.events()!r}\n"
            f"http_observations={server.observations!r}"
        )
        assert (project.root / ".agents/skills/private-package/SKILL.md").is_file()

        credential_events = [
            event for event in shim.events() if event.get("event") == "credential-fill"
        ]
        assert credential_events == [
            {
                "credential_interactive": ["never"],
                "event": "credential-fill",
                "host": "github.com",
                "path": f"{_PRIVATE_OWNER}/private-package",
                "protocol": "https",
            }
        ]

        remote_events = _remote_events(shim)
        assert remote_events
        remote_attempts = [remote for event in remote_events for remote in event["remotes"]]
        assert remote_attempts[0]["authenticated_url"] is False
        assert any(remote["authenticated_url"] is True for remote in remote_attempts)
        for event in remote_events:
            remotes = event["remotes"]
            if all(remote["authenticated_url"] is False for remote in remotes):
                _assert_anonymous_remote_event(event)

        private_requests = [
            observation
            for observation in server.observations
            if repository.origin.name in observation.path
        ]
        assert any(not observation.accepted for observation in private_requests)
        assert any(
            observation.accepted and observation.authorization_present
            for observation in private_requests
        )
