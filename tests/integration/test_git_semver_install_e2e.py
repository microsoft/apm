"""End-to-end ``apm install`` coverage for git-source semver range refs (#1488).

The unit-tier suite (``tests/unit/deps/test_git_semver_resolver.py``,
``tests/unit/install/test_git_semver_wiring.py``) covers each helper in
isolation; this file pairs that work with the full
``apm install`` -> resolve phase -> lockfile write -> lockfile replay
pipeline and asserts on the user-observable artifacts (exit code,
``apm.lock.yaml`` contents, network-call counts).

Fidelity strategy
-----------------
Two seams are stubbed -- everything else runs through the real install
pipeline:

* ``RefResolver.list_remote_refs`` returns canned ``RemoteRef`` lists per
  ``owner/repo`` (the "git ls-remote" output a private fixture repo
  would produce).
* ``GitHubPackageDownloader.download_package`` writes a minimal
  ``apm.yml`` to the install path and returns a ``PackageInfo`` whose
  ``resolved_commit`` matches the SHA the resolver picked. Validation,
  integration, and lockfile writes then run against real disk content.

Both stubs record their call counts so tests can assert "lockfile replay
did not touch the network" without relying on subprocess sentinels.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.auth import AuthResolver
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.models.apm_package import (
    APMPackage,
    DependencyReference,
    PackageInfo,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from tests.utils.git_credential_shim import GitCredentialShimFactory
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


# ---------------------------------------------------------------------------
# Canned remote ref sets
# ---------------------------------------------------------------------------


def _refs_v_prefixed() -> list[RemoteRef]:
    """Standard ``v{version}`` tag fixture: v1.0.0, v1.2.3, v1.5.0, v2.0.0."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/v1.0.0", sha="1" * 40),
        RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
        RemoteRef(name="refs/tags/v1.5.0", sha="3" * 40),
        RemoteRef(name="refs/tags/v2.0.0", sha="4" * 40),
    ]


def _refs_only_name_dashv() -> list[RemoteRef]:
    """Repo where ONLY the ``{name}--v{version}`` pattern matches.

    Mirrors a multi-package repo (PR #1422 convention) where each
    package's tags are scoped by package name.
    """
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/widget--v1.0.0", sha="a" * 40),
        RemoteRef(name="refs/tags/widget--v1.3.0", sha="b" * 40),
        RemoteRef(name="refs/tags/otherpkg--v9.9.9", sha="c" * 40),
    ]


def _refs_only_bare() -> list[RemoteRef]:
    """Repo that tags as bare ``{version}`` -- triggers third-pattern fallback."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/1.0.0", sha="d" * 40),
        RemoteRef(name="refs/tags/1.4.2", sha="e" * 40),
    ]


def _refs_virtual_subdir_single_dash() -> list[RemoteRef]:
    """Monorepo tags two packages with the ``{name}-v{version}`` pattern."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/pkg-a-v0.1.0", sha="a" * 40),
        RemoteRef(name="refs/tags/pkg-b-v9.9.9", sha="b" * 40),
    ]


def _refs_no_match() -> list[RemoteRef]:
    """No tag in any pattern satisfies a ^1.2.0 constraint."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/v0.9.0", sha="9" * 40),
    ]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _RefResolverCallRecorder:
    """Records ``list_remote_refs`` calls and serves canned refs."""

    def __init__(self, refs_by_repo: dict[str, list[RemoteRef]]) -> None:
        self.refs_by_repo = refs_by_repo
        self.calls: list[str] = []
        self.init_kwargs: list[dict] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        original_init = None
        from apm_cli.marketplace import ref_resolver as _rr_mod

        original_init = _rr_mod.RefResolver.__init__

        def _capture_init(self, *args, **kwargs):
            recorder.init_kwargs.append(dict(kwargs))
            return original_init(self, *args, **kwargs)

        def _fake_list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
            recorder.calls.append(owner_repo)
            refs = recorder.refs_by_repo.get(owner_repo)
            if refs is None:
                raise AssertionError(
                    f"Unexpected list_remote_refs call for {owner_repo!r}; "
                    f"fixture has: {sorted(recorder.refs_by_repo)}"
                )
            return list(refs)

        monkeypatch.setattr(_rr_mod.RefResolver, "__init__", _capture_init)
        monkeypatch.setattr(_rr_mod.RefResolver, "list_remote_refs", _fake_list_remote_refs)


class _DownloaderStub:
    """Stubs ``GitHubPackageDownloader.download_package`` to write a
    minimal valid apm package at the install path and return a
    ``PackageInfo`` whose ``resolved_commit`` reflects the SHA the
    resolver picked (read off ``dep_ref.reference`` after the resolve
    phase has rewritten it to the concrete tag).
    """

    def __init__(self, sha_by_tag: dict[str, str]) -> None:
        self.sha_by_tag = sha_by_tag
        self.calls: list[tuple[str, str]] = []  # (owner_repo, reference)
        self.before_download: Callable[[DependencyReference, Path], None] | None = None
        self.invalid_tags: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        def _fake_download(self, repo_ref, target_path, *args, **kwargs):
            from apm_cli.models.apm_package import DependencyReference

            if isinstance(repo_ref, DependencyReference):
                dep_ref = repo_ref
            else:
                dep_ref = DependencyReference.parse(str(repo_ref))

            ref_value = dep_ref.reference or "main"
            recorder.calls.append((dep_ref.repo_url, ref_value))

            sha = recorder.sha_by_tag.get(ref_value, "f" * 40)
            target_path = Path(target_path)
            if recorder.before_download is not None:
                recorder.before_download(dep_ref, target_path)
            target_path.mkdir(parents=True, exist_ok=True)

            if ref_value in recorder.invalid_tags:
                (target_path / "apm.yml").write_text("not: a-package\n", encoding="utf-8")
                return type(
                    "InvalidPackageInfo",
                    (),
                    {
                        "resolved_reference": ResolvedReference(
                            original_ref=ref_value,
                            ref_type=GitReferenceType.TAG,
                            resolved_commit=sha,
                            ref_name=ref_value,
                        )
                    },
                )()

            package_name = dep_ref.repo_url.rsplit("/", 1)[-1]
            (target_path / "apm.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": package_name,
                        "version": "0.0.0",
                        "description": "test fixture package",
                    }
                ),
                encoding="utf-8",
            )

            package = APMPackage.from_apm_yml(target_path / "apm.yml")
            return PackageInfo(
                package=package,
                install_path=target_path,
                installed_at=datetime.now().isoformat(),
                dependency_ref=dep_ref,
                resolved_reference=ResolvedReference(
                    original_ref=ref_value,
                    ref_type=GitReferenceType.TAG,
                    resolved_commit=sha,
                    ref_name=ref_value,
                ),
            )

        from apm_cli.deps import github_downloader as _ghd

        monkeypatch.setattr(_ghd.GitHubPackageDownloader, "download_package", _fake_download)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_apm_yml(project: Path, deps: list, name: str = "consumer-pkg") -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": deps, "mcp": []},
            }
        ),
        encoding="utf-8",
    )
    (project / ".github").mkdir(exist_ok=True)
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")


def _read_lockfile(project: Path) -> dict | None:
    path = project / "apm.lock.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _find_locked(lockfile: dict, repo_url: str) -> dict | None:
    deps = lockfile.get("dependencies") if lockfile else None
    if not deps:
        return None
    if isinstance(deps, dict):
        return deps.get(repo_url)
    for entry in deps:
        if entry.get("repo_url") == repo_url:
            return entry
    return None


def _run_install(
    runner: CliRunner,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str] | None = None,
    *,
    catch_exceptions: bool = False,
):
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, ["install", *(args or [])], catch_exceptions=catch_exceptions)


def _source_cli_environment(child_env: dict[str, str]) -> dict[str, str]:
    """Return a child environment that imports this checkout's CLI sources."""
    env = dict(child_env)
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    python_paths = [
        source_root,
        *(path for path in sys.path if path and Path(path).exists()),
        *(env.get("PYTHONPATH", "").split(os.pathsep)),
    ]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(path for path in python_paths if path))
    return env


def _source_cli_command(*args: str) -> tuple[str, ...]:
    """Run the source checkout as a real CLI process."""
    return (sys.executable, "-c", "from apm_cli.cli import main; main()", *args)


def _file_tree_bytes(root: Path) -> dict[str, bytes]:
    """Return every regular file below ``root`` for transaction assertions."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Promise A: highest matching tag wins
# Promise B: lockfile records all four semver fields
# ---------------------------------------------------------------------------


class TestSemverRangeResolves:
    def test_caret_range_resolves_to_highest_matching_tag_and_lockfile_records_fields(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``acme/widget#^1.2.0`` against {v1.0.0, v1.2.3, v1.5.0, v2.0.0} picks v1.5.0.

        Asserts the lockfile records ``constraint``, ``resolved_tag``,
        ``resolved_commit``, ``version``, and ``resolved_at`` so future
        replays are deterministic.
        """
        project = tmp_path / "promise-ab"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, f"install failed:\n{result.output}"

        lockfile = _read_lockfile(project)
        assert lockfile is not None, "apm.lock.yaml was not written"

        locked = _find_locked(lockfile, "acme/widget")
        assert locked is not None, f"acme/widget missing from lockfile: {lockfile}"

        assert locked.get("constraint") == "^1.2.0"
        assert locked.get("resolved_tag") == "v1.5.0"
        assert locked.get("version") == "1.5.0"
        assert locked.get("resolved_commit") == "3" * 40
        assert locked.get("resolved_at"), "resolved_at timestamp missing"


# ---------------------------------------------------------------------------
# Positional virtual-subdirectory semver lifecycle
# ---------------------------------------------------------------------------


class TestPositionalVirtualSubdirectorySemver:
    @pytest.mark.parametrize(
        ("constraint", "expected_tag", "expected_version"),
        [
            (">=1.0.0", "pkg-v1.2.0", "1.2.0"),
            ("~1.0.0", "pkg-v1.0.0", "1.0.0"),
            ("^1.0.0", "pkg-v1.2.0", "1.2.0"),
        ],
    )
    def test_positional_git_semver_uses_real_bare_remote_and_replays_lockfile(
        self,
        tmp_path: Path,
        constraint: str,
        expected_tag: str,
        expected_version: str,
    ) -> None:
        """Positional virtual ranges resolve a local bare remote before preflight."""
        isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
        environment = isolated.subprocess_env()
        source = isolated.package_root / "mono"
        package = source / "packages" / "pkg"
        package.mkdir(parents=True)
        (package / "apm.yml").write_text(
            "name: pkg\nversion: 1.0.0\ndescription: fixture package\n",
            encoding="utf-8",
        )
        instructions = package / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        (instructions / "fixture.instructions.md").write_text(
            "# Fixture\n",
            encoding="utf-8",
        )

        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        repository = repositories.create("mono", source_tree=source)
        first_commit = repositories.commit(repository, message="pkg v1.0.0")
        repositories.tag(repository, "pkg-v1.0.0", first_commit)
        (repository.worktree / "packages" / "pkg" / "README.md").write_text(
            "# pkg 1.2.0\n",
            encoding="utf-8",
        )
        second_commit = repositories.commit(repository, message="pkg v1.2.0")
        repositories.tag(repository, "pkg-v1.2.0", second_commit)

        child_env = _source_cli_environment(
            repositories.url_rewrite_subprocess_env(
                repository,
                "https://github.com/acme/mono.git",
            )
        )
        trace_path = isolated.work_root / "git-trace.log"
        child_env["GIT_TRACE"] = str(trace_path)

        project = isolated.work_root / "consumer"
        _write_apm_yml(project, [])
        raw_reference = f"acme/mono/packages/pkg#{constraint}"
        command = _source_cli_command(
            "install",
            "--no-policy",
            "--https",
            "--parallel-downloads",
            "0",
            raw_reference,
        )
        first = subprocess.run(
            command,
            cwd=project,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert first.returncode == 0, f"stdout={first.stdout!r}\nstderr={first.stderr!r}"
        assert raw_reference in first.stdout
        trace = trace_path.read_text(encoding="utf-8")
        assert "git ls-remote --tags --heads" in trace
        parsed_trace_urls = [
            urlparse(token.strip("'\"")) for token in trace.split() if "://" in token
        ]
        assert ("https", "github.com", "/acme/mono.git") in [
            (parsed.scheme, parsed.hostname, parsed.path) for parsed in parsed_trace_urls
        ]

        lock_path = project / "apm.lock.yaml"
        first_lock = lock_path.read_bytes()
        locked = _find_locked(_read_lockfile(project), "acme/mono")
        assert locked is not None
        assert locked["constraint"] == constraint
        assert locked["resolved_tag"] == expected_tag
        assert locked["resolved_commit"] == (
            first_commit.sha if expected_tag == "pkg-v1.0.0" else second_commit.sha
        )
        assert locked["version"] == expected_version
        installed = project / ".github" / "instructions" / "fixture.instructions.md"
        assert installed.exists(), "the resolved virtual subdirectory was not installed"

        second = subprocess.run(
            command[:-1],
            cwd=project,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert second.returncode == 0, f"stdout={second.stdout!r}\nstderr={second.stderr!r}"
        assert lock_path.read_bytes() == first_lock

    @pytest.mark.parametrize(
        ("virtual_path", "tag", "create_unmarked_package"),
        [
            ("packages/pkg", "pkg-v0.9.0", False),
            ("packages/missing", "missing-v1.0.0", False),
            ("packages/nomarker", "nomarker-v1.0.0", True),
        ],
        ids=("missing-tag", "missing-virtual-path", "missing-package-marker"),
    )
    def test_positional_git_semver_failures_restore_every_project_file(
        self,
        tmp_path: Path,
        virtual_path: str,
        tag: str,
        create_unmarked_package: bool,
    ) -> None:
        """Failure after positional ingress leaves no manifest, lock, or deployment state."""
        isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
        environment = isolated.subprocess_env()
        source = isolated.package_root / "mono"
        package = source / "packages" / "pkg"
        package.mkdir(parents=True)
        (package / "apm.yml").write_text(
            "name: pkg\nversion: 1.0.0\ndescription: fixture package\n",
            encoding="utf-8",
        )
        instructions = package / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        (instructions / "fixture.instructions.md").write_text("# Fixture\n", encoding="utf-8")
        if create_unmarked_package:
            unmarked = source / virtual_path
            unmarked.mkdir(parents=True)
            (unmarked / "README.md").write_text("# Not a package\n", encoding="utf-8")

        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        repository = repositories.create("mono", source_tree=source)
        commit = repositories.commit(repository, message="failure fixture")
        repositories.tag(repository, tag, commit)

        child_env = _source_cli_environment(
            repositories.url_rewrite_subprocess_env(
                repository,
                "https://github.com/acme/mono.git",
            )
        )
        project = isolated.work_root / "consumer"
        _write_apm_yml(project, [])
        (project / "apm.lock.yaml").write_text(
            "lockfile_version: '1'\ndependencies: []\n",
            encoding="utf-8",
        )
        before = _file_tree_bytes(project)
        raw_reference = f"acme/mono/{virtual_path}#^1.0.0"

        result = subprocess.run(
            _source_cli_command(
                "install",
                "--no-policy",
                "--https",
                "--parallel-downloads",
                "0",
                raw_reference,
            ),
            cwd=project,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0
        assert raw_reference in result.stdout
        assert _file_tree_bytes(project) == before


def test_real_git_semver_environment_build_is_single_flight(
    tmp_path: Path,
) -> None:
    """Concurrent fixture-backed semver resolution builds one shared Git env."""
    from apm_cli.core.auth import AuthResolver
    from apm_cli.deps.transport_selection import TransportSelector
    from apm_cli.install.helpers.ref_reuse import maybe_resolve_git_semver

    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source = isolated.package_root / "mono"
    source.mkdir(parents=True)
    (source / "apm.yml").write_text(
        "name: mono\nversion: 1.2.0\ndescription: fixture package\n",
        encoding="ascii",
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("mono", source_tree=source)
    commit = repositories.commit(repository, message="mono v1.2.0")
    repositories.tag(repository, "v1.2.0", commit)
    requested = "https://gitlab.com/acme/mono.git"
    child_env = repositories.url_rewrite_subprocess_env(repository, requested)
    child_env["GITLAB_APM_PAT"] = "glpat-" + "A" * 24

    class _CountingAuthResolver(AuthResolver):
        def __init__(self) -> None:
            super().__init__()
            self.build_count = 0
            self.count_lock = threading.Lock()

        def git_env_for_remote(self, ctx, remote_url: str) -> dict[str, str]:
            with self.count_lock:
                self.build_count += 1
            time.sleep(0.02)
            return super().git_env_for_remote(ctx, remote_url)

    resolver = _CountingAuthResolver()
    shared_cache: dict = {}
    shared_lock = threading.Lock()
    workers = 8
    barrier = threading.Barrier(workers)

    def resolve_one(_index: int):
        dep = DependencyReference.parse(f"{requested}#^1.0.0")
        dep.source = "git"
        barrier.wait(timeout=10)
        return maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=resolver,
            ref_resolver_cache=shared_cache,
            ref_resolver_cache_lock=shared_lock,
            transport_selector=TransportSelector(),
        )

    with patch.dict(os.environ, child_env, clear=True):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            resolutions = list(executor.map(resolve_one, range(workers)))

    assert {resolution.resolved_tag for resolution in resolutions} == {"v1.2.0"}
    assert {resolution.resolved_sha for resolution in resolutions} == {commit.sha}
    assert resolver.build_count == 1
    assert len(shared_cache) == 1


def test_private_github_semver_resolution_is_single_flight_under_concurrency(
    tmp_path: Path,
) -> None:
    """Concurrent private semver workers share auth, environment, and discovery."""
    from apm_cli.deps.transport_selection import TransportSelector
    from apm_cli.install.helpers.ref_reuse import maybe_resolve_git_semver
    from apm_cli.utils.git_env import get_git_executable, reset_git_cache

    isolated = IsolatedApmEnvironment.create(tmp_path / "private", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source = isolated.package_root / "mono"
    source.mkdir(parents=True)
    (source / "apm.yml").write_text(
        "name: mono\nversion: 1.2.0\ndescription: private fixture\n",
        encoding="ascii",
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("mono", source_tree=source)
    commit = repositories.commit(repository, message="private mono v1.2.0")
    repositories.tag(repository, "v1.2.0", commit)
    real_git = Path(get_git_executable()).resolve()
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=real_git,
        env=environment,
    )
    token = "private-single-flight-token"

    class _CountingAuthResolver(AuthResolver):
        def __init__(self) -> None:
            super().__init__()
            self.credential_resolutions = 0
            self.managed_environment_builds = 0
            self.count_lock = threading.Lock()

        def _resolve_token(self, *args, **kwargs):
            with self.count_lock:
                self.credential_resolutions += 1
            return super()._resolve_token(*args, **kwargs)

        def git_env_for_context(self, ctx, *, base_env):
            with self.count_lock:
                self.managed_environment_builds += 1
            return super().git_env_for_context(ctx, base_env=base_env)

    with server_factory.start(
        (repository,),
        password=token,
        private_repositories=(repository,),
    ) as server:
        requested = "https://github.com/fixture-private/mono.git"
        shim = GitCredentialShimFactory(isolated.root / "shims").create(
            base_env=environment,
            real_git=real_git,
            remote_map={"fixture-private/mono": server.remote_url(repository)},
            credential=token,
        )
        child_env = dict(shim.environment)
        child_env["GIT_ALLOW_PROTOCOL"] = "file:http:https"
        resolver = _CountingAuthResolver()
        shared_cache: dict = {}
        shared_lock = threading.Lock()
        workers = 8
        barrier = threading.Barrier(workers)

        def resolve_one(_index: int):
            dep = DependencyReference.parse(f"{requested}#^1.0.0")
            dep.source = "git"
            barrier.wait(timeout=10)
            return maybe_resolve_git_semver(
                dep_ref=dep,
                existing_lockfile=None,
                update_refs=True,
                auth_resolver=resolver,
                ref_resolver_cache=shared_cache,
                ref_resolver_cache_lock=shared_lock,
                transport_selector=TransportSelector(),
            )

        reset_git_cache()
        try:
            with patch.dict(os.environ, child_env, clear=True):
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    resolutions = list(executor.map(resolve_one, range(workers)))
        finally:
            reset_git_cache()

        observations = server.observations
        credential_events = [
            event for event in shim.events() if event.get("event") == "credential-fill"
        ]
        remote_events = [
            event
            for event in shim.events()
            if event.get("event") == "git"
            and event.get("command") == "ls-remote"
            and event.get("remotes")
        ]

    assert {resolution.resolved_tag for resolution in resolutions} == {"v1.2.0"}
    assert {resolution.resolved_sha for resolution in resolutions} == {commit.sha}
    assert resolver.credential_resolutions == 1
    assert resolver.managed_environment_builds == 1
    assert len(shared_cache) == 1
    assert len(remote_events) == 2
    assert [event["auth_config_present"] for event in remote_events] == [False, True]
    tag_discovery = [
        observation for observation in observations if observation.path.endswith("/info/refs")
    ]
    assert len(tag_discovery) >= 2
    assert tag_discovery[0].accepted is False
    assert tag_discovery[0].authorization is None
    assert any(observation.authorization is not None for observation in tag_discovery)
    assert tag_discovery[-1].accepted is True
    assert credential_events == [
        {
            "credential_interactive": [],
            "event": "credential-fill",
            "host": "github.com",
            "path": "fixture-private/mono",
            "protocol": "https",
        }
    ]


# ---------------------------------------------------------------------------
# Promise C: second install is offline (lockfile replay)
# ---------------------------------------------------------------------------


class TestLockfileReplayIsOffline:
    def test_reinstall_with_unchanged_manifest_does_not_call_ref_resolver(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First install hits ls-remote; second install replays from lockfile.

        The recorder asserts ``list_remote_refs`` was called exactly once
        (during the first install) and never during the second install.
        """
        project = tmp_path / "promise-c"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output
        assert rr.calls == ["acme/widget"], f"first install should ls-remote once, got: {rr.calls}"

        second = _run_install(runner, project, monkeypatch)
        assert second.exit_code == 0, second.output
        assert rr.calls == ["acme/widget"], (
            f"second install must replay from lockfile (no new ls-remote); "
            f"calls after second install: {rr.calls}"
        )


# ---------------------------------------------------------------------------
# Promise D: tag-pattern fallback order
# ---------------------------------------------------------------------------


class TestTagPatternFallback:
    def test_name_dashv_pattern_matches_when_v_pattern_has_no_candidates(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repo with only ``widget--v1.x.y`` tags resolves via second pattern.

        The default ``v{version}`` pattern finds no candidates; the
        ``{name}--v{version}`` pattern scopes to this package only and
        picks ``widget--v1.3.0`` (ignoring ``otherpkg--v9.9.9``).
        """
        project = tmp_path / "promise-d"
        _write_apm_yml(project, ["acme/widget#^1.0.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_only_name_dashv()})
        dl = _DownloaderStub({"widget--v1.3.0": "b" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        assert locked.get("resolved_tag") == "widget--v1.3.0"
        assert locked.get("version") == "1.3.0"
        # Critical: the other package's tag must not leak into this resolution.
        assert locked.get("version") != "9.9.9"

    def test_bare_version_pattern_is_third_pattern_fallback(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When neither default pattern matches, bare ``{version}`` is tried."""
        project = tmp_path / "promise-d-bare"
        _write_apm_yml(project, ["acme/widget#^1.0.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_only_bare()})
        dl = _DownloaderStub({"1.4.2": "e" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        assert locked.get("resolved_tag") == "1.4.2"
        assert locked.get("version") == "1.4.2"

    def test_virtual_subdir_single_dash_pattern_uses_subpath_name(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Virtual subdirectory deps use their path segment for tag matching."""
        project = tmp_path / "promise-d-virtual-subdir"
        _write_apm_yml(
            project,
            [{"git": "acme/mono", "path": "packages/pkg-a", "ref": "^0.1.0"}],
        )

        rr = _RefResolverCallRecorder({"acme/mono": _refs_virtual_subdir_single_dash()})
        dl = _DownloaderStub({"pkg-a-v0.1.0": "a" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        lockfile = _read_lockfile(project)
        locked = _find_locked(lockfile, "acme/mono")
        assert locked is not None, lockfile
        assert locked.get("is_virtual") is True
        assert locked.get("virtual_path") == "packages/pkg-a"
        assert locked.get("resolved_tag") == "pkg-a-v0.1.0"
        assert locked.get("version") == "0.1.0"
        assert locked.get("resolved_commit") == "a" * 40
        assert rr.calls == ["acme/mono"]


# ---------------------------------------------------------------------------
# Promise E: constraint change re-resolves
# Promise F: drift between locked constraint and manifest constraint
# ---------------------------------------------------------------------------


class TestConstraintChangeReResolves:
    def test_lockfile_constraint_change_with_stale_install_path_re_resolves(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drift sub-case (Promise F): when the install path is missing
        (cache pruned, ``apm_modules`` deleted) and the lockfile constraint
        differs from the manifest, the resolver re-runs without ``--update``.

        Exercises the ``_maybe_resolve_git_semver`` branch where
        ``locked.constraint != constraint`` skips the lockfile replay and
        falls through to ``GitSemverResolver.resolve``.
        """
        import shutil

        project = tmp_path / "promise-f"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40, "v2.0.0": "4" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output

        # Simulate a cache-pruned environment: drop the materialised dep
        # but keep the lockfile, then bump the constraint.
        shutil.rmtree(project / "apm_modules", ignore_errors=True)
        _write_apm_yml(project, ["acme/widget#^2.0.0"])
        clear_apm_yml_cache()

        second = _run_install(runner, project, monkeypatch)
        assert second.exit_code == 0, second.output

        # The drift branch in _maybe_resolve_git_semver fired -- ls-remote
        # was called and the lockfile records the new constraint.
        assert rr.calls.count("acme/widget") == 2, (
            f"expected 2 ls-remote calls (initial + drift), got: {rr.calls}"
        )
        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked["constraint"] == "^2.0.0"
        assert locked["resolved_tag"] == "v2.0.0"


# ---------------------------------------------------------------------------
# Promise G: literal ref bypasses the semver resolver
# ---------------------------------------------------------------------------


class TestLiteralRefUnchanged:
    def test_literal_tag_ref_does_not_invoke_semver_resolver(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ref: v1.2.3`` (literal tag) keeps existing behaviour.

        No ``list_remote_refs`` call, no ``constraint``/``resolved_tag``
        fields in the lockfile entry -- these are reserved for the semver
        path.
        """
        project = tmp_path / "promise-g"
        _write_apm_yml(project, ["acme/widget#v1.2.3"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.2.3": "2" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        # The literal-ref path must not touch the semver resolver.
        assert rr.calls == [], f"literal ref must not invoke list_remote_refs; got: {rr.calls}"

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        # Semver-specific fields stay absent for literal refs.
        assert "constraint" not in locked or locked.get("constraint") is None
        assert "resolved_tag" not in locked or locked.get("resolved_tag") is None
        # The literal ref is still pinned through the normal resolved_ref field.
        assert locked.get("resolved_ref") == "v1.2.3"


# ---------------------------------------------------------------------------
# Promise H: RefResolver owns public GitHub anonymous-first auth fallback
# ---------------------------------------------------------------------------


class TestAuthFallbackThreadedToLsRemote:
    def test_github_semver_defers_pat_until_anonymous_failure(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression-trap for semver's public GitHub auth fallback.

        The resolver receives AuthResolver and starts without eagerly resolving
        ``GITHUB_APM_PAT``. A unit regression test covers its authenticated retry
        after an auth-shaped anonymous failure.
        """
        monkeypatch.setenv("GITHUB_APM_PAT", "ghp_e2e_token_abc123")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        project = tmp_path / "promise-h"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        assert rr.init_kwargs
        resolver_kwargs = rr.init_kwargs[0]
        assert resolver_kwargs.get("token") is None
        assert resolver_kwargs.get("unauth_first") is True
        assert resolver_kwargs.get("auth_resolver") is not None


# ---------------------------------------------------------------------------
# Promise I: no matching tag -> clear, actionable error
# ---------------------------------------------------------------------------


class TestNoMatchingTagError:
    def test_no_matching_tag_exits_nonzero_with_actionable_message(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``^1.2.0`` against a repo with only ``v0.9.0`` fails with a
        message that names the constraint, the repo, and the tags considered."""
        project = tmp_path / "promise-i"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_no_match()})
        dl = _DownloaderStub({})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)

        combined = (result.output or "") + (result.stderr or "")
        # npm/pip/cargo convention: ANY reported install failure exits
        # non-zero so CI and scripts can detect failure without parsing
        # stderr. Regression trap for Bug 2 (#1496 e2e wave): the CLI
        # used to exit 0 even when "Installation failed with N error(s)"
        # was printed.
        assert result.exit_code != 0, (
            f"install with no-matching-tag must exit non-zero; got 0\n{combined}"
        )
        assert "failed" in combined.lower(), (
            f"expected an explicit failure marker in output:\n{combined}"
        )

        # The diagnostic must name (a) the constraint, (b) the repo, and
        # (c) at least one tag considered so the user can widen the range.
        assert "^1.2.0" in combined, f"constraint not surfaced:\n{combined}"
        assert "acme/widget" in combined, f"repo not surfaced:\n{combined}"
        assert "v0.9.0" in combined, f"available tags not surfaced:\n{combined}"

        # No lockfile entry for the failed dep.
        lockfile = _read_lockfile(project)
        locked = _find_locked(lockfile, "acme/widget") if lockfile else None
        assert locked is None or not locked.get("resolved_commit"), (
            "failed semver resolution must not write a half-populated lockfile entry"
        )


# ---------------------------------------------------------------------------
# Bug 1 (#1496 e2e wave): apm install --update must re-resolve git-semver
# constraints against the latest remote tags even when the install path
# already exists on disk. npm/cargo/bundler precedent: --update is the
# explicit re-resolve trigger; the install-path cache short-circuit must
# not swallow it.
# ---------------------------------------------------------------------------


class TestUpdateReResolvesGitSemver:
    def test_update_flag_re_resolves_when_install_path_exists_and_new_tag_published(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First install pins v1.2.3; new tag v1.5.0 published upstream;
        ``apm install --update`` must call ls-remote again and the lockfile
        must record v1.5.0.

        Regression trap for the silent no-op surfaced in the e2e wave on
        PR #1496: ``download_callback`` returned early on
        ``install_path.exists()`` before ``_maybe_resolve_git_semver``
        could run, so ``--update`` never re-resolved the constraint.
        """
        project = tmp_path / "bug1-update"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        # Initial remote: tags up through v1.2.3 only.
        initial_refs = [
            RemoteRef(name="refs/heads/main", sha="0" * 40),
            RemoteRef(name="refs/tags/v1.0.0", sha="1" * 40),
            RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
        ]
        rr = _RefResolverCallRecorder({"acme/widget": initial_refs})
        dl = _DownloaderStub({"v1.2.3": "2" * 40, "v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output

        # Clear the module-level apm.yml parse cache so the second invocation
        # re-parses apm.yml from disk. In production each CLI invocation is a
        # fresh process (empty cache); under CliRunner both invocations share
        # one Python session, so without this clear the cached APMPackage
        # instance (whose DependencyReference.reference was mutated to
        # ``v1.2.3`` by the first run's semver resolver) leaks into the
        # second run and disguises the cache-pre-purge gate as ineffective.
        from apm_cli.models.apm_package import clear_apm_yml_cache as _clear_yml

        _clear_yml()

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None and locked.get("resolved_tag") == "v1.2.3", (
            f"first install must lock v1.2.3, got: {locked}"
        )
        assert (project / "apm_modules" / "acme" / "widget").exists(), (
            "first install must materialise the dep so the cache short-circuit "
            "fires on the second invocation"
        )
        assert rr.calls.count("acme/widget") == 1, (
            f"first install should ls-remote once, got: {rr.calls}"
        )
        live_hook = project / "apm_modules" / "acme" / "widget" / "hooks" / "pre_tool.py"
        live_hook.parent.mkdir(parents=True)
        live_hook.write_text("old hook", encoding="ascii")

        def assert_live_hook(_dep_ref: DependencyReference, target_path: Path) -> None:
            if ".apm-resolution-staging" in target_path.parts:
                assert target_path != live_hook.parents[1]
                assert live_hook.read_text(encoding="ascii") == "old hook"

        dl.before_download = assert_live_hook

        # Upstream publishes v1.5.0. The install path still exists from
        # the first run -- this is the surface that hid the bug.
        rr.refs_by_repo["acme/widget"] = [
            RemoteRef(name="refs/heads/main", sha="0" * 40),
            RemoteRef(name="refs/tags/v1.0.0", sha="1" * 40),
            RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
            RemoteRef(name="refs/tags/v1.5.0", sha="3" * 40),
        ]

        second = _run_install(runner, project, monkeypatch, args=["--update"])
        assert second.exit_code == 0, second.output

        # --update must trigger a second ls-remote (the silent-no-op bug
        # would leave this at 1).
        assert rr.calls.count("acme/widget") == 2, (
            f"--update must re-resolve via ls-remote, got calls: {rr.calls}"
        )

        # Lockfile must now record the newly-published highest tag.
        locked_after = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked_after is not None
        assert locked_after.get("resolved_tag") == "v1.5.0", (
            f"--update must update resolved_tag to v1.5.0, got: {locked_after}"
        )
        assert locked_after.get("version") == "1.5.0", (
            f"--update must update version to 1.5.0, got: {locked_after}"
        )
        assert locked_after.get("resolved_commit") == "3" * 40, (
            f"--update must update resolved_commit, got: {locked_after}"
        )

    def test_invalid_update_preserves_live_hook_and_lockfile(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed refresh candidate fails without publishing stale metadata."""
        project = tmp_path / "invalid-update"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])
        rr = _RefResolverCallRecorder(
            {
                "acme/widget": [
                    RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
                ]
            }
        )
        dl = _DownloaderStub({"v1.2.3": "2" * 40, "v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)
        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output
        clear_apm_yml_cache()

        live_hook = project / "apm_modules" / "acme" / "widget" / "hooks" / "pre_tool.py"
        live_hook.parent.mkdir(parents=True)
        live_hook.write_text("old hook", encoding="ascii")
        lock_before = (project / "apm.lock.yaml").read_bytes()
        rr.refs_by_repo["acme/widget"].append(RemoteRef(name="refs/tags/v1.5.0", sha="3" * 40))
        dl.invalid_tags.add("v1.5.0")

        second = _run_install(runner, project, monkeypatch, args=["--update"])

        assert second.exit_code != 0
        output = " ".join(second.output.split())
        assert "Downloaded dependency 'acme/widget' is invalid" in output
        assert "existing installation remains active" in output
        assert live_hook.read_text(encoding="ascii") == "old hook"
        assert (project / "apm.lock.yaml").read_bytes() == lock_before

    @pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
    def test_activation_error_preserves_live_hook_and_lockfile(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_type: type[BaseException],
    ) -> None:
        """Activation errors restore the prior hook and leave lock state unchanged."""
        project = tmp_path / f"activation-{error_type.__name__}"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])
        rr = _RefResolverCallRecorder(
            {"acme/widget": [RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40)]}
        )
        dl = _DownloaderStub({"v1.2.3": "2" * 40, "v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)
        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output
        clear_apm_yml_cache()

        live = project / "apm_modules" / "acme" / "widget"
        live_hook = live / "hooks" / "pre_tool.py"
        live_hook.parent.mkdir(parents=True)
        live_hook.write_text("print('old hook')\n", encoding="ascii")
        settings_path = project / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{sys.executable} {live_hook}",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="ascii",
        )

        def assert_registered_hook_runs(
            _dep_ref: DependencyReference,
            target_path: Path,
        ) -> None:
            if ".apm-resolution-staging" not in target_path.parts:
                return
            command = json.loads(settings_path.read_text(encoding="ascii"))["hooks"]["PreToolUse"][
                0
            ]["hooks"][0]["command"]
            result = subprocess.run(
                command.split(),
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "old hook"

        dl.before_download = assert_registered_hook_runs
        lock_before = (project / "apm.lock.yaml").read_bytes()
        rr.refs_by_repo["acme/widget"].append(RemoteRef(name="refs/tags/v1.5.0", sha="3" * 40))
        original_replace = Path.replace

        def fail_activation(source: Path, target: Path) -> Path:
            if (
                ".apm-resolution-staging" in source.parts
                and "replacements" in source.parts
                and target == live
            ):
                raise error_type("injected activation failure")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_activation)

        second = _run_install(
            runner,
            project,
            monkeypatch,
            args=["--update"],
            catch_exceptions=True,
        )

        assert second.exit_code != 0
        assert live_hook.read_text(encoding="ascii") == "print('old hook')\n"
        assert_registered_hook_runs(
            DependencyReference(repo_url="acme/widget"),
            project / "apm_modules" / ".apm-resolution-staging" / "probe",
        )
        assert (project / "apm.lock.yaml").read_bytes() == lock_before
        assert not (project / "apm_modules" / ".apm-resolution-staging").exists()


# ---------------------------------------------------------------------------
# Bug 2 (#1496 e2e wave): apm install must exit non-zero whenever
# "Installation failed with N error(s)" is reported. Matches npm / pip /
# cargo: ANY install failure -> non-zero exit so CI scripts can detect it.
# The TestNoMatchingTagError class above also pins the exit-code assertion
# for the per-dep failure path; this class isolates the contract at the
# summary level.
# ---------------------------------------------------------------------------


class TestInstallExitCodeOnReportedErrors:
    def test_install_with_unsatisfiable_semver_exits_nonzero(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A direct dep with an unsatisfiable semver constraint produces
        a reported error -> exit code MUST be non-zero.

        Regression trap for Bug 2 (#1496 e2e wave).
        """
        project = tmp_path / "bug2-exit"
        _write_apm_yml(project, ["acme/widget#^9.9.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)

        combined = (result.output or "") + (result.stderr or "")
        assert result.exit_code != 0, (
            f"install with reported errors must exit non-zero; got 0\n{combined}"
        )
