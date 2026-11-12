"""Regression tests for #3071: an incomplete promisor checkout must not land.

Git before 2.36 ignores a failed promisor blob fetch during a non-forced
``git checkout``: it moves HEAD and the index, leaves the worktree empty and
exits 0. Anonymous-first subdirectory installs from a private monorepo then
cached an empty shard, and the transitive package failed validation with
"Not a valid APM package" instead of retrying with credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache, IncompleteCheckoutError, _variant_key
from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.models.validation import validate_apm_package

_REPO = "https://github.com/acme/monorepo"
_ANONYMOUS_BLOB_FETCH_STDERR = (
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
    "error: unable to read sha1 file of packages/pkg-shared/apm.yml\n"
)
_SHA_RE = re.compile(r"[0-9a-f]{40}")


@pytest.fixture(autouse=True)
def _allow_bare_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "all")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dependency_entry(path: str) -> str:
    return f"    - git: {_REPO}\n      path: {path}\n      ref: main\n"


def _build_monorepo(tmp_path: Path) -> tuple[Path, str]:
    """Create the diamond monorepo from #3071: pkg-a -> pkg-b -> pkg-shared."""
    work = tmp_path / "monorepo"
    _git("init", "-q", "-b", "main", str(work))
    _git("-C", str(work), "config", "user.email", "t@e")
    _git("-C", str(work), "config", "user.name", "t")
    _write(
        work / "packages" / "pkg-a" / "apm.yml",
        "name: pkg-a\nversion: 1.0.0\ndependencies:\n  apm:\n"
        + _dependency_entry("packages/pkg-b")
        + _dependency_entry("packages/pkg-shared"),
    )
    _write(
        work / "packages" / "pkg-b" / "apm.yml",
        "name: pkg-b\nversion: 1.0.0\ndependencies:\n  apm:\n"
        + _dependency_entry("packages/pkg-shared"),
    )
    _write(
        work / "packages" / "pkg-shared" / "apm.yml",
        "name: pkg-shared\nversion: 1.0.0\ndependencies:\n  apm: []\n",
    )
    _write(
        work / "packages" / "pkg-shared" / ".apm" / "instructions" / "shared.instructions.md",
        "---\napplyTo: '**'\n---\nShared guidance.\n",
    )
    _write(work / "README.md", "monorepo\n")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-q", "-m", "init")
    sha = _git("-C", str(work), "rev-parse", "HEAD")
    bare = tmp_path / "monorepo.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    _git("-C", str(bare), "config", "uploadpack.allowfilter", "true")
    return bare, sha


class _SilentPromisorCheckout:
    """Make SHA checkouts behave like git < 2.36 while ``fail_next()`` is true.

    The emulation updates HEAD and the index without writing the worktree and
    exits 0 with the stderr that an anonymous blob fetch prints.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail_next: Callable[[], bool]) -> None:
        self.triggered = 0
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self._is_sha_checkout(cmd) and fail_next():
                self.triggered += 1
                repo = cmd[cmd.index("-C") + 1]
                sha = cmd[-1]
                env = kwargs.get("env")
                for step in (("read-tree", sha), ("update-ref", "--no-deref", "HEAD", sha)):
                    real_run(
                        [cmd[0], "-C", repo, *step],
                        check=True,
                        capture_output=True,
                        env=env,
                    )
                return subprocess.CompletedProcess(cmd, 0, "", _ANONYMOUS_BLOB_FETCH_STDERR)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)

    @staticmethod
    def _is_sha_checkout(cmd: object) -> bool:
        return (
            isinstance(cmd, list)
            and len(cmd) >= 2
            and cmd[-2] == "checkout"
            and _SHA_RE.fullmatch(str(cmd[-1])) is not None
        )


def _shard_dir(cache_root: Path, sha: str) -> Path:
    shards = list((cache_root / "git" / "checkouts_v1").glob(f"*/{sha}"))
    assert len(shards) == 1
    return shards[0]


def test_silent_promisor_checkout_raises_auth_signal_and_lands_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, sha = _build_monorepo(tmp_path)
    cache_root = tmp_path / "cache"
    cache = GitCache(cache_root)
    url = bare.as_uri()
    sparse = ["packages/pkg-shared"]
    cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["packages/pkg-a"])
    silent = _SilentPromisorCheckout(monkeypatch, fail_next=lambda: silent.triggered == 0)

    with pytest.raises(IncompleteCheckoutError) as excinfo:
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=sparse)

    assert silent.triggered == 1
    assert AuthResolver.is_public_github_auth_failure(excinfo.value) is True
    assert "not materialized" in str(excinfo.value)
    assert sorted(p.name for p in _shard_dir(cache_root, sha).iterdir()) == [
        _variant_key(["packages/pkg-a"])
    ]
    retried = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=sparse)
    assert validate_apm_package(retried / "packages" / "pkg-shared").is_valid


def test_cached_incomplete_sparse_shard_is_rematerialized(tmp_path: Path) -> None:
    bare, sha = _build_monorepo(tmp_path)
    cache = GitCache(tmp_path / "cache")
    url = bare.as_uri()
    sparse = ["packages/pkg-shared"]
    first = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=sparse)
    manifest = first / "packages" / "pkg-shared" / "apm.yml"
    manifest.unlink()

    again = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=sparse)

    assert again == first
    assert manifest.is_file()
    assert validate_apm_package(again / "packages" / "pkg-shared").is_valid


def test_transitive_monorepo_subdirectory_retries_with_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """consumer -> pkg-a -> pkg-b -> pkg-shared resolves the declared subdirectory."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APM_PAT_ACME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_APM_PAT", "fixture-token")
    for source in ("resolve_credential_from_gh_cli", "resolve_credential_from_git"):
        monkeypatch.setattr(GitHubTokenManager, source, lambda *a, **k: None)
    bare, sha = _build_monorepo(tmp_path)
    url = bare.as_uri()
    cache = GitCache(tmp_path / "cache")
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    downloader.auth_resolver = AuthResolver(allow_external_fallback=False)
    downloader.git_env = dict(os.environ)
    downloader._tiered_resolver = None
    authenticated: list[str] = []
    auth_envs: list[dict[str, str]] = []
    build_auth_env = downloader.auth_resolver.build_public_github_authenticated_git_env

    def record_auth_env(token: str, **kwargs: object) -> dict[str, str]:
        authenticated.append(token)
        auth_envs.append(build_auth_env(token, **kwargs))
        return auth_envs[-1]

    monkeypatch.setattr(
        downloader.auth_resolver, "build_public_github_authenticated_git_env", record_auth_env
    )
    anonymous_attempt = [False]
    cache_get_checkout = cache.get_checkout

    def get_checkout(*args: object, env: dict[str, str] | None = None, **kwargs: object) -> Path:
        anonymous_attempt[0] = not any(env is auth_env for auth_env in auth_envs)
        return cache_get_checkout(*args, env=env, **kwargs)

    monkeypatch.setattr(cache, "get_checkout", get_checkout)

    def install(dep: DependencyReference) -> Path:
        assert dep.virtual_path
        checkout = downloader._persistent_cache_checkout(
            cache, dep, url, "main", locked_sha=sha, sparse_paths=[dep.virtual_path]
        )
        package_dir = checkout / dep.virtual_path
        result = validate_apm_package(package_dir)
        assert result.is_valid, result.errors
        return package_dir

    parent = install(DependencyReference.parse("acme/monorepo/packages/pkg-a#main"))
    parent_deps = APMPackage.from_apm_yml(parent / "apm.yml").get_apm_dependencies()
    assert [d.virtual_path for d in parent_deps] == ["packages/pkg-b", "packages/pkg-shared"]
    assert all(d.repo_url == "acme/monorepo" for d in parent_deps)

    # The private remote now rejects anonymous blob fetches, as in #3071.
    silent = _SilentPromisorCheckout(monkeypatch, fail_next=lambda: anonymous_attempt[0])
    child = install(parent_deps[0])
    grandchild_deps = APMPackage.from_apm_yml(child / "apm.yml").get_apm_dependencies()
    leaf = install(grandchild_deps[0])
    diamond_leaf = install(parent_deps[1])

    assert silent.triggered == 2
    assert authenticated == ["fixture-token", "fixture-token"]
    assert leaf == diamond_leaf
    assert (leaf / ".apm" / "instructions" / "shared.instructions.md").is_file()
