"""A pinned commit installs only when an origin branch or tag reaches it.

Real git against a local origin. On ``main``: m1 <- m2. f1 sits only under
``refs/pull/1/head`` (a pull request from a fork). d1 sat on a branch that
was then deleted, unmerged.
"""

from __future__ import annotations

import importlib
import os
import shutil
import stat
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

    def test_objects_left_by_a_deleted_branch_never_admit_a_commit(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        origin.git("checkout", "-qb", "feature", origin.m2)
        x1 = origin.commit("x1")
        x2 = origin.commit("x2")
        origin.git("checkout", "-q", "main")
        origin.git("push", "-q", str(origin.path), "feature")
        cache = _cache(tmp_path)
        cache.get_checkout(origin.url, x1)
        origin.bare("branch", "-D", "feature")

        # x2's objects are still in the reachability repository; no ref holds them.
        with pytest.raises(RuntimeError, match=_UNREACHABLE):
            cache.get_checkout(origin.url, x2)

    def test_an_annotated_tag_name_resolves_to_its_commit(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        origin.bare("tag", "-a", "-m", "v9", "v9", origin.m1)

        checkout = _cache(tmp_path).get_checkout(origin.url, "v9")

        assert (checkout / "m1.txt").is_file()
        assert not (checkout / "m2.txt").exists()

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

        assert not list((root / "reachability").rglob("verified-v2/*"))

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

    def test_a_declared_commit_is_refused_before_resolution_can_fetch_it(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_and_resolve

        downloader = _PlanDownloader(tmp_path / "cache", [origin.url])
        resolved: list[str] = []
        downloader.resolve_git_reference = lambda dep: resolved.append(dep.reference)
        dep = _Dep()
        dep.reference = origin.f1

        with pytest.raises(CommitNotReachableError, match=_UNREACHABLE):
            admit_and_resolve(downloader, dep)
        assert resolved == []

    def test_a_failed_contact_tries_the_next_attempt(self, tmp_path: Path, origin: _Origin) -> None:
        from apm_cli.cache.commit_reachability import admit_pinned_commit

        missing = (tmp_path / "missing.git").as_uri()
        downloader = _PlanDownloader(tmp_path / "cache", [missing, origin.url])

        admit_pinned_commit(downloader, _Dep(), origin.m1)
        assert downloader.contacted == [missing, origin.url]
        verified = [p.name for p in (tmp_path / "cache" / "reachability").rglob("verified-v2/*")]
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


class TestVerdictStorageSafety:
    def test_a_planted_link_at_a_verdict_path_is_replaced_never_followed(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import _marker, require_reachable_commit
        from apm_cli.cache.url_normalize import cache_shard_key

        root = tmp_path / "cache"
        outside = tmp_path / "outside.txt"
        outside.write_text("untouched\n", encoding="utf-8")
        marker = _marker(root, cache_shard_key(origin.url), origin.m1)
        marker.parent.mkdir(parents=True)
        marker.symlink_to(outside)

        require_reachable_commit(root, origin.url, cache_shard_key(origin.url), origin.m1)

        assert outside.read_text(encoding="utf-8") == "untouched\n"
        assert marker.is_file() and not marker.is_symlink()

    @pytest.mark.windows_compat
    @pytest.mark.parametrize("nofollow", [True, False], ids=["o-nofollow", "no-o-nofollow"])
    def test_a_linked_verdict_with_matching_contents_never_counts(
        self, tmp_path: Path, nofollow: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.cache.commit_reachability import _recorded

        if not nofollow:
            monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        planted = tmp_path / "planted"
        planted.write_text("verdict", encoding="utf-8")
        marker = tmp_path / "reachability" / "shard" / "verdict"
        marker.parent.mkdir(parents=True)
        try:
            marker.symlink_to(planted)
        except OSError:
            pytest.skip("this account cannot create symbolic links, so none can be planted")

        assert not _recorded(marker, "verdict")
        assert planted.read_text(encoding="utf-8") == "verdict"

    def test_foreign_config_in_the_reachability_repository_is_discarded(
        self, tmp_path: Path, origin: _Origin, caplog: pytest.LogCaptureFixture
    ) -> None:
        from apm_cli.cache.commit_reachability import require_reachable_commit
        from apm_cli.cache.url_normalize import cache_shard_key

        root = tmp_path / "cache"
        shard = cache_shard_key(origin.url)
        require_reachable_commit(root, origin.url, shard, origin.m1)
        config = root / "reachability" / shard / "refs.git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write("[core]\n\tsshCommand = false\n")
        origin.git("checkout", "-qb", "side", origin.m1)
        side = origin.commit("side")
        origin.git("commit", "-q", "--allow-empty", "-m", "side tip")
        origin.git("checkout", "-q", "main")
        origin.git("push", "-q", str(origin.path), "side")

        with caplog.at_level("WARNING"):
            require_reachable_commit(root, origin.url, shard, side)

        assert "Discarding cached git repository refs.git" in caplog.text
        assert "sshCommand" not in config.read_text(encoding="utf-8")


class _RefDownloader(_PlanDownloader):
    """A downloader whose commits API answers nothing, so admission lists refs."""

    def __init__(self, cache_root: Path, attempts: list[str], predicted: str) -> None:
        super().__init__(cache_root, attempts)
        self.git_env: dict[str, str] = {}
        self._predicted = predicted

    def _resolve_commit_sha_for_ref(self, _dep_ref, _ref: str) -> None:
        return None

    def _build_repo_url(self, *_args, **_kwargs) -> str:
        return self._predicted

    def _parse_artifactory_base_url(self) -> None:
        return None


class _RefDep(_Dep):
    reference = "main"

    @staticmethod
    def is_azure_devops() -> bool:
        return False

    @staticmethod
    def is_artifactory() -> bool:
        return False


def test_a_listed_tip_is_recorded_for_the_destination_that_answered(
    tmp_path: Path, origin: _Origin
) -> None:
    from apm_cli.cache.commit_reachability import admit_ref_commit

    missing = (tmp_path / "missing.git").as_uri()
    downloader = _RefDownloader(tmp_path / "cache", [missing, origin.url], predicted=missing)

    assert admit_ref_commit(downloader, _RefDep(), "main") == (origin.m2, origin.m2)
    [marker] = (tmp_path / "cache" / "reachability").rglob("verified-v2/*")
    recorded = marker.read_text(encoding="utf-8")
    assert recorded.endswith(origin.url.removesuffix(".git").removesuffix("/"))
    assert "missing" not in recorded


class _RevParseRefDownloader(_RefDownloader):
    """A commits API that resolves a name like ``git rev-parse`` against the origin."""

    def __init__(self, cache_root: Path, origin: _Origin) -> None:
        super().__init__(cache_root, [origin.url], predicted=origin.url)
        self._origin = origin

    def _resolve_commit_sha_for_ref(self, _dep_ref, ref: str) -> str | None:
        for name in (ref, f"refs/{ref}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
            try:
                return self._origin.bare("rev-parse", "--verify", "-q", f"{name}^{{commit}}")
            except subprocess.CalledProcessError:
                continue
        return None


@pytest.mark.parametrize("ref", ["release", "refs/heads/release"])
def test_a_tag_named_like_a_branch_namespace_never_answers_for_the_branch(
    tmp_path: Path, origin: _Origin, ref: str
) -> None:
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_ref_commit

    origin.bare("tag", "heads/release", origin.m1)
    downloader = _RevParseRefDownloader(tmp_path / "cache", origin)

    with pytest.raises(CommitNotReachableError):
        admit_ref_commit(downloader, _RefDep(), ref)


@trust_cached_repos()
@pytest.mark.parametrize("ref", ["main", "refs/heads/main"])
def test_a_cached_bare_resolves_a_branch_name_only_as_that_branch(
    tmp_path: Path, origin: _Origin, ref: str
) -> None:
    from apm_cli.cache.url_normalize import cache_shard_key

    cache = _cache(tmp_path)
    cache.get_checkout(origin.url, "main")
    bare = cache._db_root / cache_shard_key(origin.url)
    assert cache.bare_ref_sha(origin.url, ref) == origin.m2

    def git(*args: str) -> None:
        _REAL_RUN(["git", "--git-dir", str(bare), *args], check=True, capture_output=True)

    git("update-ref", "-d", "refs/heads/main")
    git("update-ref", "refs/tags/refs/heads/main", origin.m1)
    git("update-ref", "refs/tags/heads/main", origin.m1)

    assert cache.bare_ref_sha(origin.url, ref) is None
    assert not cache._resolved_ref_path(origin.url, ref).exists()


@trust_cached_repos()
@pytest.mark.parametrize("namespace", ["tags", "heads"])
def test_a_cached_bare_resolves_a_hex_named_ref_before_a_commit_it_abbreviates(
    tmp_path: Path, origin: _Origin, namespace: str
) -> None:
    from apm_cli.cache.url_normalize import cache_shard_key

    cache = _cache(tmp_path)
    cache.get_checkout(origin.url, "main")
    bare = cache._db_root / cache_shard_key(origin.url)
    _REAL_RUN(
        ["git", "--git-dir", str(bare), "fetch", "-q", str(origin.path), origin.f1], check=True
    )
    name = origin.f1[:8]
    _REAL_RUN(
        ["git", "--git-dir", str(bare), "update-ref", f"refs/{namespace}/{name}", origin.m1],
        check=True,
    )

    assert cache.bare_ref_sha(origin.url, name) == origin.m1
    assert cache.bare_ref_sha(origin.url, origin.f1[:12]) == origin.f1
    assert not cache._resolved_ref_path(origin.url, name).exists()


class _CommitFirstDownloader(_RefDownloader):
    """A commits API that expands any hex name as a commit abbreviation first."""

    def __init__(self, cache_root: Path, origin: _Origin, expands_to: str) -> None:
        super().__init__(cache_root, [origin.url], predicted=origin.url)
        self._expands_to = expands_to

    def _resolve_commit_sha_for_ref(self, _dep_ref, _ref: str) -> str:
        return self._expands_to


def test_admission_binds_a_hex_named_tag_before_any_commit_abbreviation(
    tmp_path: Path, origin: _Origin
) -> None:
    from apm_cli.cache.commit_reachability import admit_ref_commit

    name = origin.f1[:8]
    origin.bare("tag", name, origin.m1)
    downloader = _CommitFirstDownloader(tmp_path / "cache", origin, expands_to=origin.f1)

    assert admit_ref_commit(downloader, _RefDep(), name) == (origin.m1, origin.m1)
    [marker] = (tmp_path / "cache" / "reachability").rglob("verified-v2/*")
    assert marker.name == origin.m1


def test_admission_expands_a_hex_name_no_ref_holds(tmp_path: Path, origin: _Origin) -> None:
    from apm_cli.cache.commit_reachability import admit_ref_commit

    downloader = _CommitFirstDownloader(tmp_path / "cache", origin, expands_to=origin.m1)

    assert admit_ref_commit(downloader, _RefDep(), origin.m1[:10]) == (origin.m1, origin.m1)
    assert [m.name for m in (tmp_path / "cache" / "reachability").rglob("verified-v2/*")] == [
        origin.m1
    ]


@pytest.mark.parametrize("kind", ["tag", "abbreviation"])
def test_admission_judges_the_full_commit_a_hex_name_resolves_to(
    tmp_path: Path, origin: _Origin, kind: str
) -> None:
    from apm_cli.cache.commit_reachability import admit_pinned_commit
    from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

    if kind == "tag":
        name, ref_type = origin.f1[:8], GitReferenceType.TAG
        origin.bare("tag", name, origin.m1)
    else:
        name, ref_type = origin.m1[:10], GitReferenceType.COMMIT
    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)
    resolved = ResolvedReference(
        original_ref=name, ref_type=ref_type, resolved_commit=origin.m1, ref_name=name
    )

    admit_pinned_commit(downloader, _RefDep(), name, resolved=resolved)

    markers = [m.name for m in (tmp_path / "cache" / "reachability").rglob("verified-v2/*")]
    assert markers == [origin.m1]


def test_a_commit_the_commits_api_expanded_is_verified_where_git_fetches(
    tmp_path: Path, origin: _Origin
) -> None:
    """#5056: an API answer is not a tip of the Git destination; it is verified there."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_pinned_commit
    from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

    name = origin.f1[:10]
    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)
    # The resolver reports an abbreviation the commits API expanded as a branch-like ref.
    resolved = ResolvedReference(
        original_ref=name,
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=origin.f1,
        ref_name=name,
    )

    with pytest.raises(CommitNotReachableError):
        admit_pinned_commit(downloader, _RefDep(), name, resolved=resolved)
    assert not list((tmp_path / "cache" / "reachability").rglob("verified-v2/*"))


def test_an_api_observed_tip_is_verified_at_the_mirror_git_fetches_through(
    tmp_path: Path, origin: _Origin
) -> None:
    """#5056: a tip the API saw upstream is verified at the mirror before admission."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_pinned_commit
    from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

    mirror = tmp_path / "mirror.git"
    _REAL_RUN(["git", "clone", "-q", "--mirror", str(origin.path), str(mirror)], check=True)
    _REAL_RUN(
        ["git", "--git-dir", str(mirror), "update-ref", "refs/heads/main", origin.m1], check=True
    )
    _REAL_RUN(["git", "--git-dir", str(mirror), "update-ref", "-d", "refs/pull/1/head"], check=True)
    name = origin.m2[:10]
    downloader = _RefDownloader(tmp_path / "cache", [mirror.as_uri()], predicted=origin.url)
    resolved = ResolvedReference(
        original_ref=name,
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=origin.m2,
        ref_name=name,
    )

    with pytest.raises(CommitNotReachableError):
        admit_pinned_commit(downloader, _RefDep(), name, resolved=resolved)
    assert not list((tmp_path / "cache" / "reachability").rglob("verified-v2/*"))


@pytest.mark.parametrize(
    "spell",
    [
        lambda sha: sha[:7],
        lambda sha: sha[:39],
        lambda sha: sha[:12].upper(),
        lambda sha: "".join(c.upper() if i % 2 else c for i, c in enumerate(sha[:12])),
    ],
    ids=["length-7", "length-39", "upper-case", "mixed-case"],
)
def test_every_spelling_of_an_expanded_abbreviation_is_verified_where_git_fetches(
    tmp_path: Path, origin: _Origin, spell
) -> None:
    """#5056: no abbreviation length or case makes an API answer an observed tip."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_pinned_commit
    from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

    name = spell(origin.f1)
    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)
    resolved = ResolvedReference(
        original_ref=name,
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=origin.f1,
        ref_name=name,
    )

    with pytest.raises(CommitNotReachableError):
        admit_pinned_commit(downloader, _RefDep(), name, resolved=resolved)
    assert not list((tmp_path / "cache" / "reachability").rglob("verified*/*"))


def test_a_branch_named_like_hex_resolves_and_is_verified(tmp_path: Path, origin: _Origin) -> None:
    """A hex-named branch at a commit its branches reach is admitted and recorded."""
    from apm_cli.cache.commit_reachability import admit_pinned_commit
    from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

    name = "20241201"
    origin.bare("update-ref", f"refs/heads/{name}", origin.m1)
    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)
    resolved = ResolvedReference(
        original_ref=name,
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=origin.m1,
        ref_name=name,
    )

    admit_pinned_commit(downloader, _RefDep(), name, resolved=resolved)

    markers = [m.name for m in (tmp_path / "cache" / "reachability").rglob("verified*/*")]
    assert markers == [origin.m1]


def test_a_verdict_an_earlier_build_recorded_is_not_trusted(
    tmp_path: Path, origin: _Origin
) -> None:
    """A verdict in the pre-versioned namespace is ignored; the commit is verified again."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_pinned_commit

    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)
    admit_pinned_commit(downloader, _RefDep(), origin.m1)
    [recorded] = (tmp_path / "cache" / "reachability").rglob("verified*/*")
    stale = recorded.parent.parent / "verified" / origin.f1
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(recorded.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(CommitNotReachableError):
        admit_pinned_commit(downloader, _RefDep(), origin.f1)
    assert stale.read_text(encoding="utf-8") == recorded.read_text(encoding="utf-8")


_FETCH_FAILED = subprocess.CalledProcessError(128, ["git", "fetch"], stderr=b"fatal: not our ref")
_INVALID = RuntimeError("Invalid APM package: missing apm.yml")


@pytest.mark.parametrize(
    ("admitted", "repo_ref", "error", "named"),
    [
        ("m1", _RefDep(), _FETCH_FAILED, False),
        ("f1", _RefDep(), _FETCH_FAILED, True),
        ("f1", "acme/repo#main", _FETCH_FAILED, True),
        ("f1", _RefDep(), _INVALID, False),
    ],
    ids=[
        "still-reachable-keeps-the-error",
        "gone-is-named",
        "gone-is-named-for-a-string-ref",
        "a-validation-error-is-never-renamed",
    ],
)
def test_a_failed_download_names_a_lost_admitted_commit_only_when_it_is_gone(
    tmp_path: Path, origin: _Origin, admitted: str, repo_ref, error, named: bool
) -> None:
    """#5138: only a commit no branch or tag reaches any more is reported as lost."""
    from apm_cli.cache.commit_reachability import (
        AdmittedCommitGoneError,
        _note_admitted,
        name_a_lost_admitted_commit,
    )

    commit = getattr(origin, admitted)
    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    @name_a_lost_admitted_commit
    def _download(_downloader, _repo_ref):
        _note_admitted("main", commit)
        raise error

    expected = AdmittedCommitGoneError if named else type(error)
    with pytest.raises(expected) as failed:
        _download(downloader, repo_ref)

    assert isinstance(failed.value, AdmittedCommitGoneError) is named
    if named:
        assert f"'main' was admitted at commit {commit[:12]}" in str(failed.value)
        assert f"now points to {origin.m2[:12]}" in str(failed.value)
    else:
        assert failed.value is error
    assert not list((tmp_path / "cache" / "reachability").rglob("verified*/*"))


class _NoTransportDownloader(_RefDownloader):
    """A downloader whose transport plan fails before it contacts the origin."""

    def _execute_transport_plan(self, *_args, **_kwargs) -> None:
        raise RuntimeError("no transport could be prepared")


def test_a_failed_download_is_not_called_lost_when_the_origin_was_never_checked(
    tmp_path: Path, origin: _Origin
) -> None:
    """#5138: without a reachability verdict, the original failure is kept."""
    from apm_cli.cache.commit_reachability import _note_admitted, name_a_lost_admitted_commit

    downloader = _NoTransportDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    @name_a_lost_admitted_commit
    def _download(_downloader, _repo_ref):
        _note_admitted("main", origin.f1)
        raise _FETCH_FAILED

    with pytest.raises(subprocess.CalledProcessError) as failed:
        _download(downloader, _RefDep())

    assert failed.value is _FETCH_FAILED
    assert not (tmp_path / "cache" / "reachability").exists()


def test_a_lost_admitted_commit_message_stays_ascii_for_a_unicode_ref(
    tmp_path: Path, origin: _Origin
) -> None:
    from apm_cli.cache.commit_reachability import (
        AdmittedCommitGoneError,
        _note_admitted,
        name_a_lost_admitted_commit,
    )

    downloader = _RefDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    @name_a_lost_admitted_commit
    def _download(_downloader, _repo_ref):
        _note_admitted("rel\u00e9ase", origin.f1)
        raise _FETCH_FAILED

    with pytest.raises(AdmittedCommitGoneError) as failed:
        _download(downloader, _RefDep())

    assert str(failed.value).isascii()
    assert f"admitted at commit {origin.f1[:12]}" in str(failed.value)


class TestRefCacheLinks:
    """The ref cache never follows a link."""

    def _non_tip(self, origin: _Origin) -> str:
        return origin.m1

    def test_a_linked_ref_repository_is_relinked_and_its_target_untouched(
        self, tmp_path: Path, origin: _Origin
    ) -> None:
        from apm_cli.cache.commit_reachability import require_reachable_commit
        from apm_cli.cache.url_normalize import cache_shard_key

        root = tmp_path / "cache"
        shard = cache_shard_key(origin.url)
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("kept\n", encoding="utf-8")
        target.chmod(0o755)
        link = root / "reachability" / shard / "refs.git"
        link.parent.mkdir(parents=True)
        link.symlink_to(target, target_is_directory=True)

        require_reachable_commit(root, origin.url, shard, self._non_tip(origin))

        assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
        assert (target / "keep.txt").read_text(encoding="utf-8") == "kept\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert link.is_dir() and not link.is_symlink()

    def test_a_linked_shard_directory_fails_closed(self, tmp_path: Path, origin: _Origin) -> None:
        from apm_cli.cache.commit_reachability import (
            CommitNotReachableError,
            require_reachable_commit,
        )
        from apm_cli.cache.url_normalize import cache_shard_key

        root = tmp_path / "cache"
        shard = cache_shard_key(origin.url)
        target = tmp_path / "elsewhere"
        target.mkdir()
        (root / "reachability").mkdir(parents=True)
        (root / "reachability" / shard).symlink_to(target, target_is_directory=True)

        with pytest.raises(CommitNotReachableError):
            require_reachable_commit(root, origin.url, shard, self._non_tip(origin))
        assert list(target.iterdir()) == []


class _CloneFailsDownloader(_RefDownloader):
    """A downloader whose resolver cannot clone the origin (a dumb-HTTP host)."""

    def resolve_git_reference(self, _dep_ref):
        raise RuntimeError("Failed to clone repository acme/repo: no shallow capabilities")


def _ref(name: str):
    return type("Dep", (_RefDep,), {"reference": name})()


@pytest.mark.parametrize(
    ("ref", "tip"),
    [("main", "m2"), ("refs/heads/main", "m2"), ("v1", "m1")],
    ids=["branch", "full-branch-name", "annotated-tag-peeled"],
)
def test_a_name_the_resolver_cannot_clone_is_admitted_at_its_listed_tip(
    tmp_path: Path, origin: _Origin, ref: str, tip: str
) -> None:
    """#5138: an exact ``ls-remote`` match names the commit; the fetch is by that commit."""
    from apm_cli.cache.commit_reachability import admit_fetch_ref

    origin.git("tag", "-a", "-m", "v1", "v1", origin.m1)
    _REAL_RUN(
        ["git", "fetch", "-q", origin.work.as_uri(), "+refs/tags/*:refs/tags/*"],
        cwd=origin.path,
        check=True,
    )
    downloader = _CloneFailsDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    assert admit_fetch_ref(downloader, _ref(ref), ref) == getattr(origin, tip)
    [marker] = (tmp_path / "cache" / "reachability").rglob("verified-v2/*")
    assert marker.name == getattr(origin, tip)


def test_a_name_both_a_branch_and_a_tag_hold_is_refused_when_the_resolver_cannot_clone(
    tmp_path: Path, origin: _Origin
) -> None:
    """#5138: the listing breaks no tie between a branch and a tag, as the resolver does not."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_fetch_ref

    origin.git("tag", "main", origin.m1)
    _REAL_RUN(
        ["git", "fetch", "-q", origin.work.as_uri(), "+refs/tags/*:refs/tags/*"],
        cwd=origin.path,
        check=True,
    )
    downloader = _CloneFailsDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    with pytest.raises(CommitNotReachableError, match="ambiguous"):
        admit_fetch_ref(downloader, _ref("main"), "main")

    assert not (tmp_path / "cache" / "reachability").exists()


@pytest.mark.parametrize("ref", ["missing", "abc1234"], ids=["no-such-name", "hex-like-name"])
def test_a_resolver_failure_is_kept_when_the_listing_cannot_name_the_ref(
    tmp_path: Path, origin: _Origin, ref: str
) -> None:
    """#5138: only an exact branch or tag replaces the resolver's failure."""
    from apm_cli.cache.commit_reachability import admit_fetch_ref

    downloader = _CloneFailsDownloader(tmp_path / "cache", [origin.url], predicted=origin.url)

    with pytest.raises(RuntimeError, match="no shallow capabilities"):
        admit_fetch_ref(downloader, _ref(ref), ref)

    assert not (tmp_path / "cache" / "reachability").exists()


def test_the_ref_listing_goes_where_a_remote_activated_mirror_sends_the_clone(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: config the remote URL activates (a mirror rewrite) applies to ``ls-remote``."""
    from apm_cli.cache.commit_reachability import admit_fetch_ref

    remote = "https://origin.invalid/acme/repo.git"
    mirror = tmp_path / "mirror.gitconfig"
    mirror.write_text(f'[url "{origin.url}"]\n\tinsteadOf = {remote}\n', encoding="ascii")
    config = tmp_path / "global.gitconfig"
    config.write_text(
        f'[includeIf "hasconfig:remote.*.url:{remote}"]\n\tpath = {mirror.as_posix()}\n',
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    downloader = _CloneFailsDownloader(tmp_path / "cache", [remote], predicted=remote)

    assert admit_fetch_ref(downloader, _ref("main"), "main") == origin.m2
    [marker] = (tmp_path / "cache" / "reachability").rglob("verified-v2/*")
    assert marker.name == origin.m2


def test_two_listings_of_one_origin_probe_its_clone_config_once(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: the clone-config probe of a listing (two git spawns) runs once per origin."""
    from apm_cli.cache.commit_reachability import admit_fetch_ref

    git_env = importlib.import_module("apm_cli.utils.git_env")
    spawned: list[list[str]] = []
    real_init_run = git_env._git_init_run

    def _count(args, *rest, **kwargs):
        spawned.append([str(arg) for arg in args])
        return real_init_run(args, *rest, **kwargs)

    monkeypatch.setattr("apm_cli.utils.git_env._git_init_run", _count)
    first = _CloneFailsDownloader(tmp_path / "one", [origin.url], predicted=origin.url)
    second = _CloneFailsDownloader(tmp_path / "two", [origin.url], predicted=origin.url)

    assert admit_fetch_ref(first, _ref("main"), "main") == origin.m2
    assert admit_fetch_ref(second, _ref("main"), "main") == origin.m2

    # One probe: its ``git init`` and ``git remote add``.
    assert [("init" in args, "remote" in args) for args in spawned] == [
        (True, False),
        (False, True),
    ]
    for cache in ("one", "two"):
        [marker] = (tmp_path / cache / "reachability").rglob("verified-v2/*")
        assert marker.name == origin.m2
