"""Installed-CLI request budget for public GitHub marker validation (#3283).

Nested Claude plugins and ordinary nested APM packages share one validator.
On public github.com at a fixed ref, a marker reachable through the raw-file
path must validate with zero Contents API requests, including repeat, update,
multi-plugin, and marketplace-selected installs. Private and API-only
packages keep bounded authenticated recovery. ``marketplace add`` reuses the
catalog it discovered instead of fetching it a second time.

The loopback HTTPS fixture answers ``raw.githubusercontent.com`` and
``api.github.com`` from owned bare repositories and counts each request; Git
transport is rewritten to the same local origins.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
    GitHubHttpObservation,
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
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_OWNER = "fixture-public"
_PLUGINS_REPO = "plugin-catalog"
_PRIVATE_REPO = "private-plugins"
_REF = "v1.0.0"
_TOKEN = "fixture-github-token"
_PLUGINS = ("alpha", "beta")
_INSTALL = ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _plugin_tree(root: Path, *, revision: str) -> None:
    """Write Claude plugins whose only marker is ``.claude-plugin/plugin.json``."""
    for name in _PLUGINS:
        plugin = root / "plugins" / name
        manifest = {"name": name, "version": "1.0.0", "description": f"{name} plugin"}
        _write(plugin / ".claude-plugin/plugin.json", json.dumps(manifest))
        _write(
            plugin / "skills" / f"{name}-skill" / "SKILL.md",
            f"---\nname: {name}-skill\ndescription: {name} fixture\n---\n# {name} {revision}\n",
        )
    catalog = {
        "name": "fixture-catalog",
        "owner": {"name": "Fixture"},
        "plugins": [
            {"name": name, "source": f"./plugins/{name}", "description": f"{name} plugin"}
            for name in _PLUGINS
        ],
    }
    _write(root / ".claude-plugin/marketplace.json", json.dumps(catalog))


def _publish(
    isolated: IsolatedApmEnvironment, name: str
) -> tuple[LocalGitRepositoryFactory, LocalGitRepository, GitCommit]:
    """Publish the tagged revision, then advance main past it."""
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / name
    _plugin_tree(source, revision=_REF)
    repository = repositories.create(name, source_tree=source)
    selected = repositories.commit(repository, message="selected revision")
    repositories.tag(repository, _REF, selected)
    _plugin_tree(repository.worktree, revision="unselected-main")
    repositories.commit(repository, message="unselected main revision")
    return repositories, repository, selected


def _consumer(isolated: IsolatedApmEnvironment) -> Path:
    project = isolated.work_root / "consumer"
    _write(
        project / "apm.yml",
        "name: consumer\nversion: 1.0.0\ndescription: request budget\n"
        "targets:\n  - copilot\ndependencies:\n  apm: []\n",
    )
    return project


def _contents_requests(
    observations: tuple[GitHubHttpObservation, ...],
) -> list[GitHubHttpObservation]:
    return [observation for observation in observations if observation.is_contents_api]


def _marker_contents_requests(
    observations: tuple[GitHubHttpObservation, ...],
) -> list[GitHubHttpObservation]:
    return [
        observation
        for observation in _contents_requests(observations)
        if "/contents/plugins/" in observation.path
    ]


def _refused(observations: tuple[GitHubHttpObservation, ...]) -> list[GitHubHttpObservation]:
    return [observation for observation in observations if observation.method == "CONNECT"]


def _run(
    runner: ApmLifecycleRunner,
    fixture: LocalGitHubHttpsFixture,
    args: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    scenario_id: str,
    expected: int = 0,
) -> tuple[CommandResult, tuple[GitHubHttpObservation, ...]]:
    fixture.clear()
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)
    observations = fixture.observations
    assert result.returncode == expected, (
        f"{args!r} -> {result.returncode}\n{result.stdout}\n{result.stderr}\n{observations!r}"
    )
    assert not _refused(observations), observations
    return result, observations


def _lock_entries(project: Path) -> dict[str, dict[str, object]]:
    lock = load_yaml(project / "apm.lock.yaml")
    return {
        entry["virtual_path"]: entry for entry in lock["dependencies"] if entry.get("is_virtual")
    }


def _deployed_skill(project: Path, name: str) -> str:
    matches = sorted(project.rglob(f"{name}-skill/SKILL.md"))
    matches = [path for path in matches if "apm_modules" not in path.parts]
    assert matches, f"{name}-skill was not deployed"
    return matches[0].read_text(encoding="utf-8")


def _environment(
    isolated: IsolatedApmEnvironment,
    repositories: LocalGitRepositoryFactory,
    repository: LocalGitRepository,
    slug: str,
    fixture: LocalGitHubHttpsFixture,
) -> dict[str, str]:
    environment = repositories.url_rewrite_subprocess_env(repository, f"https://github.com/{slug}")
    return fixture.child_environment(environment)


def test_public_nested_plugins_validate_without_contents_api(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Multi-plugin install, repeat, and update spend zero marker Contents API calls."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "public", base_env=dict(os.environ))
    repositories, repository, selected = _publish(isolated, _PLUGINS_REPO)
    slug = f"{_OWNER}/{_PLUGINS_REPO}"
    packages = tuple(f"{slug}/plugins/{name}#{_REF}" for name in _PLUGINS)
    project = _consumer(isolated)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    with local_github_https_fixture(tmp_path / "certs", {slug: repository}) as fixture:
        env = _environment(isolated, repositories, repository, slug, fixture)
        _first, first_http = _run(
            runner,
            fixture,
            (*_INSTALL, *packages),
            cwd=project,
            env=env,
            scenario_id="public-marker-first",
        )
        _repeat, repeat_http = _run(
            runner,
            fixture,
            (*_INSTALL, *packages),
            cwd=project,
            env=env,
            scenario_id="public-marker-repeat",
        )
        _update, update_http = _run(
            runner,
            fixture,
            (*_INSTALL, "--update", *packages),
            cwd=project,
            env=env,
            scenario_id="public-marker-update",
        )

    for observations in (first_http, repeat_http, update_http):
        assert _contents_requests(observations) == []
        assert all(not observation.authenticated for observation in observations)
    for name in _PLUGINS:
        marker = f"/{slug}/{_REF}/plugins/{name}/.claude-plugin/plugin.json"
        hits = [o for o in first_http if o.is_raw and o.path == marker and o.status == 200]
        assert len(hits) == 1, first_http

    entries = _lock_entries(project)
    assert set(entries) == {f"plugins/{name}" for name in _PLUGINS}
    for entry in entries.values():
        assert entry["repo_url"] == slug
        assert entry["resolved_ref"] == _REF
        assert entry["resolved_commit"] == selected.sha
    for name in _PLUGINS:
        content = _deployed_skill(project, name)
        assert _REF in content
        assert "unselected-main" not in content


def test_missing_public_marker_keeps_diagnostic_and_bounded_api_fallback(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A path with no marker fails truthfully after one bounded API pass."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "missing", base_env=dict(os.environ))
    repositories, repository, _selected = _publish(isolated, _PLUGINS_REPO)
    slug = f"{_OWNER}/{_PLUGINS_REPO}"
    project = _consumer(isolated)
    manifest_before = (project / "apm.yml").read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    with local_github_https_fixture(tmp_path / "certs", {slug: repository}) as fixture:
        env = _environment(isolated, repositories, repository, slug, fixture)
        result, observations = _run(
            runner,
            fixture,
            (*_INSTALL, f"{slug}/plugins/missing#{_REF}"),
            cwd=project,
            env=env,
            scenario_id="public-marker-missing",
            expected=1,
        )

    diagnostics = result.stdout + result.stderr
    assert "plugins/missing" in diagnostics
    marker_api = _marker_contents_requests(observations)
    # After the raw sweep misses, the existing fallback asks the API once per
    # candidate marker (7) plus once for the directory itself; the sweep never
    # reaches the API and no path is asked twice.
    assert 0 < len(marker_api) <= 8, observations
    assert len({o.path for o in marker_api}) == len(marker_api)
    assert all(not o.authenticated for o in observations)
    assert (project / "apm.yml").read_bytes() == manifest_before
    assert not (project / "apm.lock.yaml").exists()


def test_private_nested_plugin_recovers_through_authenticated_api(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Private content hidden from raw recovers with a bounded authenticated probe."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "private", base_env=dict(os.environ))
    repositories, repository, selected = _publish(isolated, _PRIVATE_REPO)
    slug = f"{_OWNER}/{_PRIVATE_REPO}"
    project = _consumer(isolated)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    with local_github_https_fixture(
        tmp_path / "certs", {slug: repository}, private=(slug,), tokens=(_TOKEN,)
    ) as fixture:
        env = _environment(isolated, repositories, repository, slug, fixture)
        env["GITHUB_APM_PAT"] = _TOKEN
        result, observations = _run(
            runner,
            fixture,
            (*_INSTALL, f"{slug}/plugins/alpha#{_REF}"),
            cwd=project,
            env=env,
            scenario_id="private-marker-recovery",
        )

    marker_api = _marker_contents_requests(observations)
    authenticated_hits = [o for o in marker_api if o.authenticated and o.status == 200]
    assert len(authenticated_hits) == 1, observations
    assert authenticated_hits[0].path.endswith("plugins/alpha/.claude-plugin/plugin.json")
    # Bounded: never more than one anonymous and one authenticated request
    # per candidate marker.
    assert len(marker_api) <= 2 * 7, observations
    assert _TOKEN not in result.stdout + result.stderr
    entry = _lock_entries(project)["plugins/alpha"]
    assert entry["resolved_commit"] == selected.sha
    assert _REF in _deployed_skill(project, "alpha")


def test_marketplace_add_reuses_discovered_catalog_and_installs_without_contents_api(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Registration fetches the catalog once; plugin markers stay off the API."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "marketplace", base_env=dict(os.environ))
    repositories, repository, selected = _publish(isolated, _PLUGINS_REPO)
    slug = f"{_OWNER}/{_PLUGINS_REPO}"
    project = _consumer(isolated)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)
    catalog_path = "/repos/" + slug + "/contents/.claude-plugin/marketplace.json"

    with local_github_https_fixture(tmp_path / "certs", {slug: repository}) as fixture:
        env = _environment(isolated, repositories, repository, slug, fixture)
        _add, add_http = _run(
            runner,
            fixture,
            ("marketplace", "add", slug, "--name", "fixture", "--ref", _REF),
            cwd=project,
            env=env,
            scenario_id="marketplace-add",
        )
        _listed, _ = _run(
            runner,
            fixture,
            ("marketplace", "list"),
            cwd=project,
            env=env,
            scenario_id="marketplace-list",
        )
        _install, install_http = _run(
            runner,
            fixture,
            (*_INSTALL, "alpha@fixture", "beta@fixture"),
            cwd=project,
            env=env,
            scenario_id="marketplace-install",
        )

    catalog_hits = [o for o in add_http if o.path == catalog_path and o.status == 200]
    assert len(catalog_hits) == 1, add_http
    assert "fixture" in _listed.stdout
    assert _marker_contents_requests(install_http) == []
    entries = _lock_entries(project)
    assert set(entries) == {f"plugins/{name}" for name in _PLUGINS}
    assert all(entry["resolved_commit"] == selected.sha for entry in entries.values())
    for name in _PLUGINS:
        assert _REF in _deployed_skill(project, name)


def test_marketplace_add_without_catalog_records_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A failed discovery leaves no registration behind."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "no-catalog", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / "empty"
    _write(source / "README.md", "# no catalog\n")
    repository = repositories.create("empty", source_tree=source)
    selected = repositories.commit(repository, message="no catalog")
    repositories.tag(repository, _REF, selected)
    slug = f"{_OWNER}/empty"
    project = _consumer(isolated)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    with local_github_https_fixture(tmp_path / "certs", {slug: repository}) as fixture:
        env = _environment(isolated, repositories, repository, slug, fixture)
        result, _observations = _run(
            runner,
            fixture,
            ("marketplace", "add", slug, "--name", "empty", "--ref", _REF),
            cwd=project,
            env=env,
            scenario_id="marketplace-add-missing",
            expected=1,
        )
        listed, _ = _run(
            runner,
            fixture,
            ("marketplace", "list"),
            cwd=project,
            env=env,
            scenario_id="marketplace-list-after-failure",
        )

    assert "marketplace.json" in result.stdout + result.stderr
    assert "empty" not in listed.stdout
