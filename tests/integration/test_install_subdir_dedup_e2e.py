"""End-to-end tests for subdirectory clone-cache identity and reuse.

The hermetic contract for #2191 uses real local Git origins behind nested
GitLab-shaped URLs. It proves distinct repositories remain distinct across
shared and persistent cache tiers, lockfile provenance, and deployed bytes,
while two packages from the same repository still reuse one bare clone.

The live test exercises the parallel download race directly against the real
GitHub-hosted ``github/awesome-copilot`` repo with two sibling subdirectory
dependencies sharing one repository/ref cache identity.

Pre-#1126 fix, this test reliably failed with
``RuntimeError("Subdirectory '...' not found in repository")`` because
the v1 cache materialized one subdir at the cache layer and the second
consumer found the cached dir without its expected subdir.

Parametrized across ``ref_kind`` to cover all three materialization
paths:
- ``symbolic-https``: ref="main" (the original 6.2 baseline)
- ``sha-https``: ref pinned to a known commit (exercises
  ``_bare_clone_with_fallback``'s 3-tier SHA path)
- ``default-branch``: no ref (exercises the no-ref path)

Marked ``integration`` so it only runs in the integration suite (it
requires network and a GitHub token like the rest of
``tests/integration/test_apm_dependencies.py``).
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cache.url_normalize import cache_shard_key, normalize_repo_url
from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockFile
from apm_cli.deps.shared_clone_cache import SharedCloneCache
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.path_security import PathTraversalError

# Two sibling subdirs under the same upstream repo+ref. Both are
# present on github/awesome-copilot at the time of writing; if either
# is removed upstream, swap with another pair from
# `gh api repos/github/awesome-copilot/contents/skills --jq '.[].name'`.
SUBDIR_A = "skills/acquire-codebase-knowledge"
SUBDIR_B = "skills/agent-governance"

# A historical commit on github/awesome-copilot main that contains
# both subdirs. Resolved at fixture-setup time via the GitHub API; if
# resolution fails, the sha-https variant is skipped (rare network /
# upstream-API issue). The KNOWN_SHA constant below is a fallback for
# offline scenarios where the resolver cannot reach the API.
KNOWN_SHA: str | None = None


_REMOTE_A = "https://gitlab.com/acme/platform/team/repo-a"
_REMOTE_B = "https://gitlab.com/acme/platform/team/repo-b"


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run Git and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_package_origin(
    root: Path,
    origin_name: str,
    packages: dict[str, tuple[str, str]],
) -> tuple[Path, str]:
    """Create a bare local origin with real package bytes at each subdirectory."""
    worktree = root / f"{origin_name}-worktree"
    origin = root / f"{origin_name}.git"
    subprocess.run(
        ["git", "init", "-b", "main", str(worktree)],
        check=True,
        capture_output=True,
    )
    _git("config", "user.email", "test@example.com", cwd=worktree)
    _git("config", "user.name", "APM Test", cwd=worktree)
    for package_path, (package_name, payload) in packages.items():
        package_dir = worktree / package_path
        package_dir.mkdir(parents=True)
        (package_dir / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": package_name,
                    "version": "1.0.0",
                    "description": f"Hermetic fixture for {package_name}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (package_dir / "repository.txt").write_text(payload, encoding="utf-8")
        instructions_dir = package_dir / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / f"{package_name}.instructions.md").write_text(
            payload,
            encoding="utf-8",
        )
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "fixture", cwd=worktree)
    commit = _git("rev-parse", "HEAD", cwd=worktree)
    _git("clone", "--bare", str(worktree), str(origin), cwd=root)
    return origin, commit


def _dependency(remote: str, package_path: str) -> DependencyReference:
    """Build one nested GitLab subdirectory dependency at the shared ref."""
    return DependencyReference.parse_from_dict(
        {
            "git": f"{remote}.git",
            "path": package_path,
            "ref": "main",
        }
    )


def _instead_of_env(origins: dict[str, Path]) -> dict[str, str]:
    """Route GitLab-shaped URLs to local bare origins through real Git config."""
    rewrites = [
        (remote_variant, origin)
        for remote, origin in origins.items()
        for remote_variant in (f"{remote}.git", remote)
    ]
    env: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(rewrites))}
    for index, (remote, origin) in enumerate(rewrites):
        env[f"GIT_CONFIG_KEY_{index}"] = f"url.{origin.as_uri()}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{index}"] = remote
    return env


def _write_install_project(project: Path, dependencies: list[DependencyReference]) -> None:
    """Write a project manifest that drives the real install pipeline."""
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "nested-gitlab-cache-contract",
                "version": "1.0.0",
                "targets": ["copilot"],
                "dependencies": {
                    "apm": [
                        {
                            "git": f"{dep.to_github_url()}.git",
                            "path": dep.virtual_path,
                            "ref": dep.reference,
                        }
                        for dep in dependencies
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _run_install(
    project: Path,
    git_env: dict[str, str],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Run the real CLI while replacing only remote transport with local origins."""

    def _fixture_git_env(
        downloader: GitHubPackageDownloader,
        **_kwargs: object,
    ) -> dict[str, str]:
        return dict(downloader.git_env)

    monkeypatch.chdir(project)
    with (
        patch.dict(os.environ, git_env, clear=False),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch.object(GitHubPackageDownloader, "_resolve_dep_token", return_value=None),
        patch.object(GitHubPackageDownloader, "_resolve_dep_auth_ctx", return_value=None),
        patch.object(
            GitHubPackageDownloader,
            "_build_noninteractive_git_env",
            autospec=True,
            side_effect=_fixture_git_env,
        ),
    ):
        return CliRunner().invoke(
            cli,
            [
                "install",
                "--no-policy",
                "--parallel-downloads",
                "2",
                "--target",
                "copilot",
            ],
            catch_exceptions=False,
        )


@pytest.mark.integration
def test_nested_gitlab_repositories_with_same_group_install_independently(tmp_path: Path) -> None:
    """The old key reused repo A's real bare and bytes for repo B."""
    origin_a, _ = _create_package_origin(
        tmp_path,
        "origin-a",
        {
            "skills/tool": ("repo-a", "bytes-from-repo-a"),
            "skills/reuse": ("repo-a-reuse", "reuse-bytes-from-repo-a"),
        },
    )
    origin_b, _ = _create_package_origin(
        tmp_path,
        "origin-b",
        {"skills/tool": ("repo-b", "bytes-from-repo-b")},
    )
    dep_a = _dependency(_REMOTE_A, "skills/tool")
    dep_a_reuse = _dependency(_REMOTE_A, "skills/reuse")
    dep_b = _dependency(_REMOTE_B, "skills/tool")
    fixture_by_repository = {
        dep_a.repo_url: origin_a,
        dep_b.repo_url: origin_b,
    }
    cloned_repositories: list[str] = []
    bare_by_repository: dict[str, Path] = {}

    def clone_fixture(repository: str, bare_target: Path, **_kwargs) -> None:
        cloned_repositories.append(repository)
        bare_by_repository[repository] = bare_target
        subprocess.run(
            ["git", "clone", "--bare", str(fixture_by_repository[repository]), str(bare_target)],
            check=True,
            capture_output=True,
        )

    downloader = GitHubPackageDownloader()
    downloader.persistent_git_cache = None
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    target_a = tmp_path / "installed" / "repo-a"
    target_a_reuse = tmp_path / "installed" / "repo-a-reuse"
    target_b = tmp_path / "installed" / "repo-b"

    with (
        SharedCloneCache(base_dir=cache_dir) as shared_cache,
        patch.object(downloader, "_bare_clone_with_fallback", side_effect=clone_fixture),
    ):
        downloader.shared_clone_cache = shared_cache
        downloader.download_subdirectory_package(dep_a, target_a)
        downloader.download_subdirectory_package(dep_b, target_b)
        downloader.download_subdirectory_package(dep_a_reuse, target_a_reuse)

        assert (target_a / "repository.txt").read_text(encoding="utf-8") == "bytes-from-repo-a"
        assert (target_b / "repository.txt").read_text(encoding="utf-8") == "bytes-from-repo-b"
        assert (target_a_reuse / "repository.txt").read_text(
            encoding="utf-8"
        ) == "reuse-bytes-from-repo-a"

        key_a = (normalize_repo_url(dep_a.to_github_url()), "main")
        key_b = (normalize_repo_url(dep_b.to_github_url()), "main")
        assert set(shared_cache._entries) == {key_a, key_b}
        assert shared_cache._entries[key_a].path != shared_cache._entries[key_b].path

        origin_url_a = _git(
            "--git-dir",
            str(bare_by_repository[dep_a.repo_url]),
            "config",
            "--get",
            "remote.origin.url",
        )
        origin_url_b = _git(
            "--git-dir",
            str(bare_by_repository[dep_b.repo_url]),
            "config",
            "--get",
            "remote.origin.url",
        )
        assert Path(origin_url_a).resolve() == origin_a.resolve()
        assert Path(origin_url_b).resolve() == origin_b.resolve()

    assert cloned_repositories == [dep_a.repo_url, dep_b.repo_url]


@pytest.mark.integration
def test_nested_gitlab_identity_survives_cache_lock_and_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full install preserves identity through cache, lock provenance, and bytes."""
    origin_a, commit_a = _create_package_origin(
        tmp_path,
        "origin-a",
        {
            "packages/repo-a": ("repo-a", "deployed-bytes-from-repo-a"),
            "packages/reuse": ("repo-a-reuse", "deployed-reuse-bytes-from-repo-a"),
        },
    )
    origin_b, commit_b = _create_package_origin(
        tmp_path,
        "origin-b",
        {"packages/repo-b": ("repo-b", "deployed-bytes-from-repo-b")},
    )
    dependencies = [
        _dependency(_REMOTE_A, "packages/repo-a"),
        _dependency(_REMOTE_A, "packages/reuse"),
        _dependency(_REMOTE_B, "packages/repo-b"),
    ]
    expected_commits = {
        dependencies[0].get_unique_key(): commit_a,
        dependencies[1].get_unique_key(): commit_a,
        dependencies[2].get_unique_key(): commit_b,
    }
    git_env = _instead_of_env({_REMOTE_A: origin_a, _REMOTE_B: origin_b})

    shared_project = tmp_path / "shared-project"
    _write_install_project(shared_project, dependencies)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    shared_result = _run_install(shared_project, git_env, monkeypatch=monkeypatch)
    assert shared_result.exit_code == 0, shared_result.output

    shared_lock = LockFile.read(shared_project / "apm.lock.yaml")
    assert shared_lock is not None
    for dep in dependencies:
        locked = shared_lock.get_dependency(dep.get_unique_key())
        assert locked is not None
        assert locked.host == "gitlab.com"
        assert locked.repo_url == dep.repo_url
        assert locked.virtual_path == dep.virtual_path
        assert locked.resolved_commit == expected_commits[dep.get_unique_key()]

    assert (shared_project / ".github" / "instructions" / "repo-a.instructions.md").read_text(
        encoding="utf-8"
    ) == "deployed-bytes-from-repo-a"
    assert (shared_project / ".github" / "instructions" / "repo-a-reuse.instructions.md").read_text(
        encoding="utf-8"
    ) == "deployed-reuse-bytes-from-repo-a"
    assert (shared_project / ".github" / "instructions" / "repo-b.instructions.md").read_text(
        encoding="utf-8"
    ) == "deployed-bytes-from-repo-b"

    persistent_project = tmp_path / "persistent-project"
    persistent_cache = tmp_path / "persistent-cache"
    _write_install_project(persistent_project, dependencies)
    monkeypatch.delenv("APM_NO_CACHE", raising=False)
    monkeypatch.setenv("APM_CACHE_DIR", str(persistent_cache))
    persistent_result = _run_install(persistent_project, git_env, monkeypatch=monkeypatch)
    assert persistent_result.exit_code == 0, persistent_result.output

    cache_urls = {dep.repo_url: dep.to_github_url() for dep in dependencies}
    shard_a = cache_shard_key(cache_urls[dependencies[0].repo_url])
    shard_b = cache_shard_key(cache_urls[dependencies[2].repo_url])
    assert shard_a != shard_b

    bare_a = persistent_cache / "git" / "db_v1" / f"{shard_a}__p"
    bare_b = persistent_cache / "git" / "db_v1" / f"{shard_b}__p"
    assert bare_a.is_dir()
    assert bare_b.is_dir()
    assert _git("--git-dir", str(bare_a), "config", "--get", "remote.origin.url") == _REMOTE_A
    assert _git("--git-dir", str(bare_b), "config", "--get", "remote.origin.url") == _REMOTE_B

    checkout_a = persistent_cache / "git" / "checkouts_v1" / shard_a / commit_a
    checkout_b = persistent_cache / "git" / "checkouts_v1" / shard_b / commit_b
    variants_a = sorted(path for path in checkout_a.iterdir() if path.is_dir())
    variants_b = sorted(path for path in checkout_b.iterdir() if path.is_dir())
    assert len(variants_a) == 2
    assert len(variants_b) == 1
    assert sorted(
        marker.read_text(encoding="utf-8")
        for variant in variants_a
        for marker in variant.rglob("repository.txt")
    ) == ["deployed-bytes-from-repo-a", "deployed-reuse-bytes-from-repo-a"]
    assert [
        marker.read_text(encoding="utf-8") for marker in variants_b[0].rglob("repository.txt")
    ] == ["deployed-bytes-from-repo-b"]

    persistent_lock = LockFile.read(persistent_project / "apm.lock.yaml")
    assert persistent_lock is not None
    for dep in dependencies:
        locked = persistent_lock.get_dependency(dep.get_unique_key())
        assert locked is not None
        assert locked.resolved_commit == expected_commits[dep.get_unique_key()]
        assert locked.deployed_files
        assert locked.deployed_file_hashes

    assert (persistent_project / ".github" / "instructions" / "repo-a.instructions.md").read_text(
        encoding="utf-8"
    ) == "deployed-bytes-from-repo-a"
    assert (
        persistent_project / ".github" / "instructions" / "repo-a-reuse.instructions.md"
    ).read_text(encoding="utf-8") == "deployed-reuse-bytes-from-repo-a"
    assert (persistent_project / ".github" / "instructions" / "repo-b.instructions.md").read_text(
        encoding="utf-8"
    ) == "deployed-bytes-from-repo-b"


def test_nested_gitlab_traversal_cannot_alias_repository_identity() -> None:
    """Encoded traversal cannot manufacture a sibling cache identity."""
    with pytest.raises(PathTraversalError, match="traversal sequence"):
        DependencyReference.parse_from_dict(
            {
                "git": f"{_REMOTE_A}/%252e%252e/repo-b.git",
                "path": "skills/tool",
                "ref": "main",
            }
        )


def _resolve_known_sha() -> str | None:
    """Resolve a real commit SHA on github/awesome-copilot for the sha-https variant.

    Passes ``GH_TOKEN`` to the ``gh`` subprocess so the test does not depend
    on the developer's ambient ``gh auth login`` state -- CI workers will have
    ``GITHUB_APM_PAT`` (or ``GITHUB_TOKEN``) but no ``gh`` config (Copilot
    review #1135).
    """
    import subprocess

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_APM_PAT")
    if not token:
        return None
    env = {**os.environ, "GH_TOKEN": token}

    try:
        result = subprocess.run(
            ["gh", "api", "repos/github/awesome-copilot/commits/main", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            sha = result.stdout.strip()
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                return sha
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return None


@pytest.mark.integration
@pytest.mark.requires_github_token
@pytest.mark.parametrize(
    "ref_kind,ref_value",
    [
        ("symbolic-https", "main"),
        ("default-branch", None),
        ("sha-https", "RESOLVE_AT_RUNTIME"),
    ],
)
def test_two_subdirs_same_repo_parallel(ref_kind: str, ref_value: str | None) -> None:
    """Two sibling subdir deps from same repo+ref download in parallel.

    Asserts:
      1. Both subdir packages materialize with their expected content.
      2. No ``RuntimeError("Subdirectory ... not found")`` raised.
      3. Both consumers receive the same ``resolved_commit`` (cache hit
         on second consumer).
    """
    github_token = os.getenv("GITHUB_APM_PAT") or os.getenv("GITHUB_TOKEN")
    if not github_token:
        pytest.skip("GitHub token required (GITHUB_APM_PAT or GITHUB_TOKEN)")

    if ref_kind == "sha-https":
        ref_value = _resolve_known_sha()
        if ref_value is None:
            pytest.skip("Could not resolve a known SHA on github/awesome-copilot/main")

    test_dir = Path(tempfile.mkdtemp(prefix="apm_e2e_1126_"))
    try:
        # Build two dep refs sharing the same (host, owner, repo, ref)
        # cache key so they race through SharedCloneCache.
        ref_suffix = f"#{ref_value}" if ref_value else ""
        dep_a = DependencyReference.parse(f"github/awesome-copilot/{SUBDIR_A}{ref_suffix}")
        dep_b = DependencyReference.parse(f"github/awesome-copilot/{SUBDIR_B}{ref_suffix}")

        target_a = test_dir / "modules" / "a"
        target_b = test_dir / "modules" / "b"
        target_a.parent.mkdir(parents=True, exist_ok=True)

        # One downloader sharing the cache - mirrors install/phases/resolve.py
        # which attaches a single SharedCloneCache to the downloader.
        downloader = GitHubPackageDownloader()
        (test_dir / ".cache").mkdir()
        with SharedCloneCache(base_dir=test_dir / ".cache") as shared_cache:
            downloader.shared_clone_cache = shared_cache

            # Drive both downloads in parallel via ThreadPoolExecutor
            # (mirrors apm_resolver.py parallel BFS dispatch).
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                fa = ex.submit(downloader.download_subdirectory_package, dep_a, target_a)
                fb = ex.submit(downloader.download_subdirectory_package, dep_b, target_b)
                # Both must succeed without the v1
                # "Subdirectory ... not found" error.
                pkg_a = fa.result(timeout=120)
                pkg_b = fb.result(timeout=120)

        # Both subdirs must have materialized with content.
        assert target_a.exists(), f"{SUBDIR_A} not materialized"
        assert target_b.exists(), f"{SUBDIR_B} not materialized"
        assert any(target_a.iterdir()), f"{SUBDIR_A} is empty"
        assert any(target_b.iterdir()), f"{SUBDIR_B} is empty"

        # Lockfile parity (Copilot review #1135 + panel follow-up):
        # The canonical resolved SHA path is ``pkg.resolved_reference.resolved_commit``
        # (set in ``apm_package.py``). Asserting on it directly catches
        # silent regressions where the cache hit fails to propagate the
        # SHA back into the consumer's resolved reference.
        sha_a = pkg_a.resolved_reference.resolved_commit
        sha_b = pkg_b.resolved_reference.resolved_commit
        assert sha_a is not None and sha_a != "unknown" and len(sha_a) == 40, (
            f"expected resolved 40-char SHA for {SUBDIR_A}, got {sha_a!r}"
        )
        assert sha_b is not None and sha_b != "unknown" and len(sha_b) == 40, (
            f"expected resolved 40-char SHA for {SUBDIR_B}, got {sha_b!r}"
        )
        assert sha_a == sha_b, (
            f"Sibling subdirs from same repo+ref must resolve to "
            f"same commit, got a={sha_a} b={sha_b}"
        )
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)
