"""A pinned commit installs only when an origin branch or tag reaches it.

Real git against a local origin. On ``main``: m1 <- m2. f1 sits only under
``refs/pull/1/head`` (a pull request from a fork). d1 sat on a branch that
was then deleted, unmerged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache
from tests.utils.cached_repo_trust import trust_cached_repos
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-ref-freshness")]

_REAL_RUN = subprocess.run
_UNREACHABLE = "not reachable from any branch or tag"


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in isolated.process_environment.items():
        monkeypatch.setenv(name, value)


class _Origin:
    def __init__(self, root: Path, name: str = "origin") -> None:
        self.work = root / f"{name}-work"
        self.work.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.m1 = self.commit("m1")
        self.m2 = self.commit("m2")
        self.git("checkout", "-qb", "fork", self.m1)
        self.f1 = self.commit("f1")
        self.git("checkout", "-qb", "tmp", self.m1)
        self.d1 = self.commit("d1")
        self.git("checkout", "-q", "main")
        self.git("update-ref", "refs/pull/1/head", self.f1)
        self.git("branch", "-qD", "fork", "tmp")
        self.path = root / f"{name}.git"
        _REAL_RUN(["git", "clone", "-q", "--mirror", str(self.work), str(self.path)], check=True)
        self.url = self.path.as_uri()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = _REAL_RUN(
            ["git", "-c", "user.name=APM Test", "-c", "user.email=apm@example.com", *args],
            cwd=cwd or self.work,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit(self, label: str) -> str:
        (self.work / f"{label}.txt").write_text(label, encoding="utf-8")
        self.git("add", f"{label}.txt")
        self.git("commit", "-qm", label)
        return self.git("rev-parse", "HEAD")

    def bare(self, *args: str) -> str:
        return self.git("--git-dir", str(self.path), *args, cwd=self.path)


@pytest.fixture()
def origin(tmp_path: Path) -> _Origin:
    return _Origin(tmp_path)


def _cache(tmp_path: Path, name: str = "cache") -> GitCache:
    return GitCache(tmp_path / name)


@trust_cached_repos()
class TestPinnedCommitReachability:
    def test_a_non_tip_commit_on_main_installs(self, tmp_path: Path, origin: _Origin) -> None:
        checkout = _cache(tmp_path).get_checkout(origin.url, origin.m1)
        assert (checkout / "m1.txt").is_file()

    def test_a_commit_only_under_a_pull_ref_is_refused(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        with pytest.raises(RuntimeError, match=_UNREACHABLE):
            _cache(tmp_path).get_checkout(origin.url, origin.f1)

    def test_a_locked_commit_only_under_a_pull_ref_is_refused(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        with pytest.raises(RuntimeError, match=_UNREACHABLE):
            _cache(tmp_path).get_checkout(origin.url, None, locked_sha=origin.f1)

    def test_a_commit_on_a_deleted_unmerged_branch_is_refused(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        with pytest.raises(RuntimeError, match=_UNREACHABLE):
            _cache(tmp_path).get_checkout(origin.url, origin.d1)

    def test_a_pull_commit_installs_once_merged(self, tmp_path: Path, origin: _Origin) -> None:
        origin.git("merge", "-q", "--no-ff", "-m", "merge", origin.f1)
        origin.git("push", "-q", "--force", str(origin.path), "main")

        checkout = _cache(tmp_path).get_checkout(origin.url, origin.f1)
        assert (checkout / "f1.txt").is_file()

    @pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
    def test_a_tag_at_a_pull_commit_makes_it_install(
        self, tmp_path: Path, origin: _Origin, annotated: bool
    ) -> None:
        tag = ["tag", "-a", "-m", "v9", "v9", origin.f1] if annotated else ["tag", "v9", origin.f1]
        origin.bare(*tag)

        checkout = _cache(tmp_path).get_checkout(origin.url, origin.f1)
        assert (checkout / "f1.txt").is_file()


@trust_cached_repos()
class TestReachabilityMarker:
    def test_a_verified_commit_reinstalls_offline(self, tmp_path: Path, origin: _Origin) -> None:
        cache = _cache(tmp_path)
        cache.get_checkout(origin.url, origin.m1)
        shutil.rmtree(origin.path)

        checkout = cache.get_checkout(origin.url, origin.m1)
        assert (checkout / "m1.txt").is_file()

    def test_an_unverified_commit_fails_closed_offline(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        cache = _cache(tmp_path)
        cache.get_checkout(origin.url, origin.m1)
        shutil.rmtree(tmp_path / "cache" / "reachability")
        shutil.rmtree(origin.path)

        with pytest.raises(RuntimeError, match=origin.m1) as excinfo:
            cache.get_checkout(origin.url, origin.m1)
        assert "network" in str(excinfo.value)

    def test_a_marker_for_one_origin_never_admits_the_same_commit_from_another(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        fork = _Origin(tmp_path, "fork")
        fork.bare("fetch", "-q", str(origin.path), f"{origin.f1}:refs/pull/2/head")
        origin.bare("tag", "v9", origin.f1)
        cache = _cache(tmp_path)
        cache.get_checkout(origin.url, origin.f1)

        with pytest.raises(RuntimeError, match=_UNREACHABLE):
            cache.get_checkout(fork.url, origin.f1)
