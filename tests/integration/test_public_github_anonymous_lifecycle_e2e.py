"""Installed-CLI lifecycle coverage for public GitHub anonymous-first auth."""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
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
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    if not any(
        environment.get(f"GIT_CONFIG_KEY_{index}", "").lower() == "credential.interactive"
        for index in range(count)
    ):
        environment["GIT_CONFIG_COUNT"] = str(count + 1)
        environment[f"GIT_CONFIG_KEY_{count}"] = "credential.interactive"
        environment[f"GIT_CONFIG_VALUE_{count}"] = "never"
    assert environment["HOME"] == str(isolated.home)
    return environment


def _remote_events(shim: GitCredentialShim) -> list[dict[str, object]]:
    return [
        event
        for event in shim.events()
        if event.get("event") == "git" and event.get("command") != "config" and event.get("remotes")
    ]


def _locked_dependency(project_root: Path, repo_url: str) -> dict[str, object]:
    """Return one dependency entry from the installed lifecycle lockfile."""
    lockfile = load_yaml(project_root / "apm.lock.yaml")
    assert lockfile is not None
    dependencies = lockfile.get("dependencies", [])
    if isinstance(dependencies, dict):
        entry = dependencies.get(repo_url)
        assert isinstance(entry, dict)
        return entry
    entry = next(
        (
            candidate
            for candidate in dependencies
            if isinstance(candidate, dict) and candidate.get("repo_url") == repo_url
        ),
        None,
    )
    assert isinstance(entry, dict)
    return entry


def _assert_anonymous_remote_event(event: dict[str, object]) -> None:
    assert event["github_token_env"] == []
    assert event["git_token_present"] is False
    assert event["auth_config_present"] is False
    assert event["credential_helpers"]
    assert set(event["credential_helpers"]) == {""}
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
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
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


@pytest.mark.parametrize(
    ("reference", "include_virtual"),
    (
        pytest.param("main", True, id="branch"),
        pytest.param("^1.0.0", False, id="semver"),
    ),
)
def test_private_github_fallback_normalizes_locale_and_completes_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
    reference: str,
    include_virtual: bool,
) -> None:
    """A localized private clone retries once with a path-scoped credential."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "private-scenario",
        base_env={
            **os.environ,
            "LC_ALL": "id_ID.UTF-8",
            "LANGUAGE": "id_ID:id",
        },
    )
    base_environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("private-package", targets=("copilot",))
    packages.add_skill(source, "private-package", _skill_source("private-package"))
    packages.add_skill(source, "private-subpackage", _skill_source("private-subpackage"))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=base_environment,
    )
    repository = repositories.create("private-package", source_tree=source.root)
    commit = repositories.commit(repository, message="seed private package")
    if reference == "^1.0.0":
        repositories.tag(repository, "v1.0.0", commit)
    public_source = packages.create("public-package", targets=("copilot",))
    packages.add_skill(public_source, "public-package", _skill_source("public-package"))
    public_repository = repositories.create("public-package", source_tree=public_source.root)
    public_commit = repositories.commit(public_repository, message="seed public package")
    repositories.tag(public_repository, "v2.0.0", public_commit)
    dependencies = [f"{_PRIVATE_OWNER}/private-package#{reference}"]
    if include_virtual:
        dependencies.append(
            f"{_PRIVATE_OWNER}/private-package/skills/private-subpackage#{reference}"
        )
    else:
        dependencies.append(f"{_PUBLIC_OWNER}/public-package#^2.0.0")
    project = LocalPackageFactory(isolated.work_root).create(
        "private-consumer",
        dependencies=tuple(dependencies),
        targets=("copilot",),
    )

    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=base_environment,
    )
    public_server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=_real_git(),
        env=base_environment,
    )
    with ExitStack() as stack:
        server = stack.enter_context(
            server_factory.start(
                (repository,),
                private_repositories=(repository,),
                password=_PRIVATE_TOKEN,
            )
        )
        public_server = stack.enter_context(
            public_server_factory.start(
                (public_repository,),
                password="unused-public-password",
            )
        )
        shim = GitCredentialShimFactory(isolated.root / "shims").create(
            base_env=base_environment,
            real_git=_real_git(),
            remote_map={
                f"{_PRIVATE_OWNER}/private-package": server.remote_url(repository),
                f"{_PUBLIC_OWNER}/public-package": public_server.remote_url(public_repository),
            },
            credential=_PRIVATE_TOKEN,
        )
        environment = _child_environment(
            isolated,
            shim,
            proxy_url=server.proxy_url,
        )
        environment["GIT_HTTP_EXTRAHEADER"] = "Authorization: Bearer ambient-must-not-leak"
        scenario_kind = "semver" if reference == "^1.0.0" else "branch"
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            (*_INSTALL_ARGS, "--verbose"),
            scenario_id=f"private-github-scoped-fallback-{scenario_kind}",
            cwd=project.root,
            env=environment,
        )

        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}\n"
            f"shim_events={shim.events()!r}\n"
            f"http_observations={server.observations!r}"
        )
        assert "Partial clone unavailable" not in result.stdout
        assert "Partial clone unavailable" not in result.stderr
        assert (project.root / ".agents/skills/private-package/SKILL.md").is_file()
        if include_virtual:
            assert (project.root / ".agents/skills/private-subpackage/SKILL.md").is_file()
        else:
            assert (project.root / ".agents/skills/public-package/SKILL.md").is_file()

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
        assert all(event["lc_all"] == "C" for event in remote_events)
        assert all(event["language"] == "C" for event in remote_events)
        remote_attempts = [remote for event in remote_events for remote in event["remotes"]]
        assert remote_attempts[0]["authenticated_url"] is False
        assert all(remote["authenticated_url"] is False for remote in remote_attempts)
        header_authenticated_events = [
            event for event in remote_events if event["auth_config_present"] is True
        ]
        assert header_authenticated_events
        for event in remote_events:
            remotes = event["remotes"]
            if event["auth_config_present"] is True:
                assert event["git_token_present"] is False
                assert all(remote["authenticated_url"] is False for remote in remotes)
            elif all(remote["authenticated_url"] is False for remote in remotes):
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
        if reference == "^1.0.0":
            private_locked = _locked_dependency(
                project.root,
                f"{_PRIVATE_OWNER}/private-package",
            )
            assert private_locked.get("constraint") == "^1.0.0"
            assert private_locked.get("resolved_tag") == "v1.0.0"
            assert private_locked.get("version") == "1.0.0"
            assert private_locked.get("resolved_commit") == commit.sha
            public_locked = _locked_dependency(
                project.root,
                f"{_PUBLIC_OWNER}/public-package",
            )
            assert public_locked.get("resolved_tag") == "v2.0.0"
            assert public_locked.get("resolved_commit") == public_commit.sha
            tag_discovery = [
                observation
                for observation in private_requests
                if observation.path.endswith("/info/refs")
            ]
            assert len(tag_discovery) >= 2
            assert tag_discovery[0].accepted is False
            assert tag_discovery[0].authorization_present is False
            assert tag_discovery[1].accepted is True
            assert tag_discovery[1].authorization_present is True
            assert _PRIVATE_TOKEN not in result.stdout
            assert _PRIVATE_TOKEN not in result.stderr
            public_requests = [
                observation
                for observation in public_server.observations
                if public_repository.origin.name in observation.path
            ]
            assert public_requests
            assert all(not observation.authorization_present for observation in public_requests)

        bare_cache = isolated.cache_root / "git" / "db_v1"
        bare_repositories = [path for path in bare_cache.iterdir() if path.is_dir()]
        assert len(bare_repositories) >= 2
        assert all(_PRIVATE_TOKEN not in path.name for path in bare_repositories)
        for bare_repository in bare_repositories:
            remote = subprocess.run(
                (
                    str(_real_git()),
                    "-C",
                    str(bare_repository),
                    "config",
                    "--get",
                    "remote.origin.url",
                ),
                check=True,
                capture_output=True,
                text=True,
                env=base_environment,
            ).stdout.strip()
            parsed = urlparse(remote)
            assert parsed.username is None
            assert parsed.password is None
