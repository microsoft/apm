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


_UPSTREAM = "https://git.example.invalid/acme/repo.git"


def _rewrite(origin: _Origin, *, remove: bool = False) -> None:
    """Route the upstream URL to the local origin, as a user's mirror rewrite does."""
    action = ["--unset"] if remove else []
    _REAL_RUN(
        [
            "git",
            "config",
            "--file",
            os.environ["GIT_CONFIG_GLOBAL"],
            *action,
            f"url.{origin.url}.insteadOf",
            *([] if remove else [_UPSTREAM]),
        ],
        check=True,
    )


class _PlanDownloader:
    """The clone transport plan: each attempt runs until one completes."""

    def __init__(self, cache_root: Path, attempts: list[str]) -> None:
        self.persistent_git_cache = type("Cache", (), {"_cache_root": cache_root})()
        self.attempts = attempts
        self.contacted: list[str] = []

    def _persistent_cache_remote(self, _dep_ref, url: str) -> str:
        return url

    def _execute_transport_plan(self, _base, target, *, dep_ref, clone_action, **_kw) -> None:
        for url in self.attempts:
            self.contacted.append(url)
            try:
                clone_action(url, {}, target)
                return
            except subprocess.CalledProcessError:
                continue
        raise RuntimeError("every transport attempt failed")


class _Dep:
    repo_url = "acme/repo"
    artifactory_prefix = None

    @staticmethod
    def to_github_url() -> str:
        return _UPSTREAM


class TestVerdictScope:
    def test_a_verdict_through_a_mirror_never_stands_for_the_origin(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import (
            CommitNotReachableError,
            require_reachable_commit,
        )
        from apm_cli.cache.url_normalize import cache_shard_key

        root = tmp_path / "cache"
        _rewrite(origin)
        require_reachable_commit(root, _UPSTREAM, cache_shard_key(_UPSTREAM), origin.m1)
        _rewrite(origin, remove=True)

        # The upstream itself is now contacted: the mirror's verdict is not reused.
        with pytest.raises(CommitNotReachableError, match="network"):
            require_reachable_commit(root, _UPSTREAM, cache_shard_key(_UPSTREAM), origin.m1)

    def test_an_unknown_contact_url_records_no_verdict(
        self, tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.cache.commit_reachability import require_reachable_commit
        from apm_cli.cache.url_normalize import cache_shard_key

        def _unreadable(*_args, **_kwargs):
            raise ValueError("git config could not be read")

        monkeypatch.setattr("apm_cli.utils.git_env.effective_git_url", _unreadable)
        root = tmp_path / "cache"
        require_reachable_commit(root, origin.url, cache_shard_key(origin.url), origin.m1)

        assert not list((root / "reachability").rglob("verified/*"))

    def test_a_refusal_stops_the_plan_even_if_a_later_attempt_would_admit(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_pinned_commit

        lenient = tmp_path / "lenient.git"
        _REAL_RUN(["git", "clone", "-q", "--mirror", str(origin.path), str(lenient)], check=True)
        origin.git("--git-dir", str(lenient), "update-ref", "refs/heads/feature", origin.f1)
        downloader = _PlanDownloader(tmp_path / "cache", [origin.url, lenient.as_uri()])

        with pytest.raises(CommitNotReachableError, match=_UNREACHABLE):
            admit_pinned_commit(downloader, _Dep(), origin.f1)
        assert downloader.contacted == [origin.url]

    def test_a_read_only_reader_admits_without_persisting(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import admit_pinned_commit

        downloader = _PlanDownloader(tmp_path / "cache", [origin.url])
        downloader.cache_read_only = True

        admit_pinned_commit(downloader, _Dep(), origin.m1)
        assert downloader.contacted == [origin.url]
        assert not (tmp_path / "cache").exists()

    def test_a_failed_contact_tries_the_next_attempt(self, tmp_path: Path, origin: _Origin) -> None:
        from apm_cli.cache.commit_reachability import admit_pinned_commit

        missing = (tmp_path / "missing.git").as_uri()
        downloader = _PlanDownloader(tmp_path / "cache", [missing, origin.url])

        admit_pinned_commit(downloader, _Dep(), origin.m1)
        assert downloader.contacted == [missing, origin.url]
        verified = [p.name for p in (tmp_path / "cache" / "reachability").rglob("verified/*")]
        assert verified == [origin.m1]


class TestCachedBareTrust:
    """Real trust checks (no ``trust_cached_repos``): repositories here are real clones."""

    def test_a_mirror_rewritten_clone_completes_without_a_discard(
        self, tmp_path: Path, origin: _Origin, caplog: pytest.LogCaptureFixture
    ) -> None:
        origin.bare("tag", "v9", origin.f1)
        _rewrite(origin)
        cache = _cache(tmp_path)

        with caplog.at_level("WARNING"):
            first = cache.get_checkout(_UPSTREAM, origin.f1)
            second = cache.get_checkout(_UPSTREAM, origin.m1)

        assert (first / "f1.txt").is_file()
        assert (second / "m1.txt").is_file()
        assert "Discarding" not in caplog.text

    def test_a_bare_recording_the_effective_contact_url_is_kept(
        self, tmp_path: Path, origin: _Origin, caplog: pytest.LogCaptureFixture
    ) -> None:
        _rewrite(origin)
        cache = _cache(tmp_path)
        cache.get_checkout(_UPSTREAM, origin.m1)
        [bare] = [p for p in (tmp_path / "cache" / "git" / "db_v1").iterdir() if p.is_dir()]
        # Some git transports record the rewritten URL they contacted.
        origin.git("--git-dir", str(bare), "config", "remote.origin.url", origin.url)

        with caplog.at_level("WARNING"):
            checkout = cache.get_checkout(_UPSTREAM, origin.m2)

        assert (checkout / "m2.txt").is_file()
        assert "Discarding" not in caplog.text

    def test_a_bare_recording_another_remote_is_discarded(
        self, tmp_path: Path, origin: _Origin, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        cache.get_checkout(origin.url, origin.m2)
        [bare] = [p for p in (tmp_path / "cache" / "git" / "db_v1").iterdir() if p.is_dir()]
        origin.git("--git-dir", str(bare), "config", "remote.origin.url", _UPSTREAM)

        with caplog.at_level("WARNING"):
            checkout = cache.get_checkout(origin.url, origin.m1)

        assert (checkout / "m1.txt").is_file()
        assert "Discarding cached git repository" in caplog.text
