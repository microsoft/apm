"""Installed-CLI proof that a registered marketplace ref selects real artifacts (#1918).

Each catalog repository publishes different plugin bytes on ``main`` and on
the slash-containing branch ``release/pilot``. ``apm marketplace add --ref
release/pilot`` followed by a normal ``apm install plugin@marketplace`` runs
production resolution, ref selection, Git download and deployment. The tests
assert the deployed bytes, selected ref, resolved commit, source identity and
persisted manifest/lock state, so losing the inherited ref or installing
default-branch content fails.

Two existing routes are covered:

* GitHub-family canonical (``owner/repo/path#ref``) served by the loopback
  ``raw.githubusercontent.com`` / ``api.github.com`` fixture.
* GitLab structured ``git`` + ``path`` + ``ref`` for a self-managed GitLab
  FQDN that is not listed in ``GITLAB_HOST`` (the resolver routes it through
  the same structured reference as ``gitlab.com``).

Git transport is rewritten to owned local bare repositories; no real host is
contacted. Explicit plugin source pins and ``plugin@marketplace#ref`` install
overrides keep their existing precedence over the registered ref.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_github_https_fixture import (
    LocalGitHubHttpsFixture,
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_PILOT = "release/pilot"
_MARKETPLACE = "pilot-catalog"
_INSTALL = ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")

_GITHUB_SLUG = "fixture-org/agent-catalog"
_GITLAB_HOST = "gitlab.example.invalid"
_GITLAB_PROJECT = "team/platform/agent-catalog"
_GITLAB_REMOTE = f"https://{_GITLAB_HOST}/{_GITLAB_PROJECT}"


def _skill(plugin: str, revision: str) -> str:
    return (
        f"---\nname: {plugin}-skill\ndescription: {plugin} registered ref fixture\n---\n"
        f"# {plugin} from {revision}\n"
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_plugins(root: Path, revision: str, catalog_plugins: list[dict[str, object]]) -> None:
    """Write Claude plugins whose skill body names the publishing branch."""
    for entry in catalog_plugins:
        name = str(entry["name"])
        plugin = root / "plugins" / name
        manifest = {"name": name, "version": "1.0.0", "description": f"{name} plugin"}
        _write(plugin / ".claude-plugin" / "plugin.json", json.dumps(manifest))
        _write(plugin / "skills" / f"{name}-skill" / "SKILL.md", _skill(name, revision))
    catalog = {"name": _MARKETPLACE, "owner": {"name": "Fixture"}, "plugins": catalog_plugins}
    _write(root / ".claude-plugin" / "marketplace.json", json.dumps(catalog))


def _git(environment: dict[str, str], cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _publish_default_and_pilot(
    isolated: IsolatedApmEnvironment,
    catalog_plugins: list[dict[str, object]],
) -> tuple[LocalGitRepositoryFactory, LocalGitRepository, GitCommit, str]:
    """Publish distinct plugin bytes on ``main`` and ``release/pilot``."""
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("agent-catalog")
    _write_plugins(repository.worktree, "main", catalog_plugins)
    default = repositories.commit(repository, message="default branch plugins")
    _git(environment, repository.worktree, "checkout", "-b", _PILOT)
    _write_plugins(repository.worktree, _PILOT, catalog_plugins)
    _git(environment, repository.worktree, "add", "--all")
    _git(environment, repository.worktree, "commit", "-m", "pilot branch plugins")
    _git(environment, repository.worktree, "push", "origin", f"HEAD:refs/heads/{_PILOT}")
    pilot_sha = _git(environment, repository.worktree, "rev-parse", "HEAD")
    assert pilot_sha != default.sha
    return repositories, repository, default, pilot_sha


def _consumer(isolated: IsolatedApmEnvironment, name: str) -> Path:
    project = isolated.work_root / name
    _write(
        project / "apm.yml",
        f"name: {name}\nversion: 1.0.0\ndescription: registered ref consumer\n"
        "targets:\n  - copilot\ndependencies:\n  apm: []\n",
    )
    return project


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    scenario_id: str,
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)
    assert result.returncode == 0, f"{args!r}\n{result.stdout}\n{result.stderr}"
    return result


def _locked(project: Path, virtual_path: str) -> dict[str, object]:
    entries = [
        entry
        for entry in load_yaml(project / "apm.lock.yaml")["dependencies"]
        if entry.get("virtual_path") == virtual_path
    ]
    assert len(entries) == 1, entries
    return entries[0]


def _deployed_skill(project: Path, plugin: str) -> bytes:
    matches = [
        path
        for path in sorted(project.rglob(f"{plugin}-skill/SKILL.md"))
        if "apm_modules" not in path.relative_to(project).parts
    ]
    assert matches, f"{plugin}-skill was not deployed under {project}"
    contents = {path.read_bytes() for path in matches}
    assert len(contents) == 1, matches
    return contents.pop()


def _manifest_dependencies(project: Path) -> list[object]:
    return load_yaml(project / "apm.yml")["dependencies"]["apm"]


def _catalog_refs(fixture: LocalGitHubHttpsFixture) -> set[str]:
    """Refs the CLI requested when reading the catalog from the GitHub fixture."""
    refs: set[str] = set()
    for observation in fixture.observations:
        if observation.status != 200 or not observation.path.endswith(
            ".claude-plugin/marketplace.json"
        ):
            continue
        if observation.is_contents_api:
            refs.update(parse_qs(observation.query).get("ref", []))
        elif observation.is_raw:
            prefix = f"/{_GITHUB_SLUG}/"
            suffix = "/.claude-plugin/marketplace.json"
            refs.add(observation.path[len(prefix) : -len(suffix)])
    return refs


@pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available")
def test_github_registered_branch_installs_pilot_artifacts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """GitHub canonical: inherited ref deploys pilot bytes; pins and overrides keep precedence."""
    catalog_plugins: list[dict[str, object]] = [
        {"name": "alpha", "source": "./plugins/alpha"},
        {
            "name": "beta",
            "source": {
                "source": "github",
                "repo": _GITHUB_SLUG,
                "path": "plugins/beta",
                "ref": "main",
            },
        },
    ]
    isolated = IsolatedApmEnvironment.create(tmp_path / "github", base_env=dict(os.environ))
    repositories, repository, default, pilot_sha = _publish_default_and_pilot(
        isolated, catalog_plugins
    )
    project = _consumer(isolated, "consumer")
    override_project = _consumer(isolated, "override-consumer")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    with local_github_https_fixture(tmp_path / "certs", {_GITHUB_SLUG: repository}) as fixture:
        env = fixture.child_environment(
            repositories.url_rewrite_subprocess_env(
                repository, f"https://github.com/{_GITHUB_SLUG}"
            )
        )
        _run(
            runner,
            ("marketplace", "add", _GITHUB_SLUG, "--name", _MARKETPLACE, "--ref", _PILOT),
            cwd=project,
            env=env,
            scenario_id="github-marketplace-add-pilot",
        )
        assert _catalog_refs(fixture) == {_PILOT}
        _run(
            runner,
            (*_INSTALL, f"alpha@{_MARKETPLACE}", f"beta@{_MARKETPLACE}"),
            cwd=project,
            env=env,
            scenario_id="github-install-inherited-ref",
        )
        _run(
            runner,
            (*_INSTALL, f"alpha@{_MARKETPLACE}#main"),
            cwd=override_project,
            env=env,
            scenario_id="github-install-user-override",
        )
        refused = [o for o in fixture.observations if o.method == "CONNECT"]
        assert refused == [], refused

    # Inherited registered ref: pilot bytes, pilot commit, pilot ref persisted.
    assert _deployed_skill(project, "alpha") == _skill("alpha", _PILOT).encode("utf-8")
    alpha = _locked(project, "plugins/alpha")
    assert alpha["repo_url"] == _GITHUB_SLUG
    assert alpha.get("host", "github.com") == "github.com"
    assert alpha["resolved_ref"] == _PILOT
    assert alpha["resolved_commit"] == pilot_sha
    assert alpha["discovered_via"] == _MARKETPLACE
    assert alpha["marketplace_plugin_name"] == "alpha"

    # Explicit plugin source pin wins over the registered ref (dict non-overwrite).
    assert _deployed_skill(project, "beta") == _skill("beta", "main").encode("utf-8")
    beta = _locked(project, "plugins/beta")
    assert beta["repo_url"] == _GITHUB_SLUG
    assert beta["resolved_ref"] == "main"
    assert beta["resolved_commit"] == default.sha

    assert sorted(_manifest_dependencies(project)) == [
        f"{_GITHUB_SLUG}/plugins/alpha#{_PILOT}",
        f"{_GITHUB_SLUG}/plugins/beta#main",
    ]

    # User install override wins over the registered ref.
    assert _deployed_skill(override_project, "alpha") == _skill("alpha", "main").encode("utf-8")
    overridden = _locked(override_project, "plugins/alpha")
    assert overridden["resolved_ref"] == "main"
    assert overridden["resolved_commit"] == default.sha
    assert _manifest_dependencies(override_project) == [f"{_GITHUB_SLUG}/plugins/alpha#main"]


def test_gitlab_registered_branch_installs_pilot_artifacts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """GitLab structured git+path: inherited ref deploys pilot bytes and persists the ref."""
    catalog_plugins: list[dict[str, object]] = [{"name": "alpha", "source": "./plugins/alpha"}]
    isolated = IsolatedApmEnvironment.create(tmp_path / "gitlab", base_env=dict(os.environ))
    repositories, repository, _default, pilot_sha = _publish_default_and_pilot(
        isolated, catalog_plugins
    )
    env = repositories.url_rewrite_subprocess_env(repository, _GITLAB_REMOTE)
    project = _consumer(isolated, "consumer")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    _run(
        runner,
        ("marketplace", "add", _GITLAB_REMOTE, "--name", _MARKETPLACE, "--ref", _PILOT),
        cwd=project,
        env=env,
        scenario_id="gitlab-marketplace-add-pilot",
    )
    _run(
        runner,
        (*_INSTALL, f"alpha@{_MARKETPLACE}"),
        cwd=project,
        env=env,
        scenario_id="gitlab-install-inherited-ref",
    )

    assert _deployed_skill(project, "alpha") == _skill("alpha", _PILOT).encode("utf-8")

    (dependency,) = _manifest_dependencies(project)
    assert isinstance(dependency, dict), dependency
    assert set(dependency) == {"git", "path", "ref"}
    git_url = urlparse(str(dependency["git"]))
    assert (git_url.scheme, git_url.hostname) == ("https", _GITLAB_HOST)
    assert git_url.path.removesuffix(".git") == f"/{_GITLAB_PROJECT}"
    assert dependency["path"] == "plugins/alpha"
    assert dependency["ref"] == _PILOT

    alpha = _locked(project, "plugins/alpha")
    assert alpha["host"] == _GITLAB_HOST
    assert alpha["repo_url"] == _GITLAB_PROJECT
    assert alpha["resolved_ref"] == _PILOT
    assert alpha["resolved_commit"] == pilot_sha
    assert alpha["discovered_via"] == _MARKETPLACE
    assert alpha["marketplace_plugin_name"] == "alpha"
