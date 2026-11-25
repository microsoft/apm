"""Cached repositories are used only while they hold Git state APM writes.

Every repository APM keeps between runs (cache bares and checkouts, native
snapshots) has its own Git config, which Git always reads. Each cell plants
one setting APM never writes into an otherwise APM-shaped cached repository,
then drives the cache the way ``apm install --update`` does. The repository
must be discarded, and nothing the setting names may run. A control shows an
unmodified cache entry is still used as is.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.url_normalize import cache_shard_key
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-child-repository-isolation"),
]

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="payload commands use a POSIX shell"
)


class _ChallengeHandler(http.server.BaseHTTPRequestHandler):
    """Answer an origin request with 401 and a proxied one with 407."""

    def _deny(self) -> None:
        proxied = self.command == "CONNECT" or self.path.startswith("http")
        self.send_response(407 if proxied else 401)
        header = "Proxy-Authenticate" if proxied else "WWW-Authenticate"
        self.send_header(header, 'Basic realm="apm-test"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_CONNECT = _deny

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in isolated.process_environment.items():
        monkeypatch.setenv(name, value)


@pytest.fixture()
def server_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _origin(root: Path) -> tuple[Path, str, str]:
    """A two-commit repository; return it with its first and second commit."""
    origin = root / "origin"
    _git("init", "-q", "-b", "main", "--template=", str(origin))
    _git("-C", str(origin), "config", "user.email", "t@example.com")
    _git("-C", str(origin), "config", "user.name", "T")
    (origin / "AGENTS.md").write_text("one\n", encoding="ascii")
    _git("-C", str(origin), "add", "-A")
    _git("-C", str(origin), "commit", "-q", "-m", "one")
    first = _git("-C", str(origin), "rev-parse", "HEAD")
    (origin / "AGENTS.md").write_text("two\n", encoding="ascii")
    _git("-C", str(origin), "commit", "-q", "-am", "two")
    return origin, first, _git("-C", str(origin), "rev-parse", "HEAD")


def _cached_bare(
    cache_root: Path, origin: Path, first: str, url: str, *, verified: bool = True
) -> Path:
    """Lay down the bare APM would cache for *url*, holding only the first commit.

    With *verified*, also the reachability verdicts APM records beside the
    commits it admitted (every origin commit here); a cache from an older APM
    has none.
    """
    if verified:
        from apm_cli.cache.commit_reachability import _marker, _verdict_key, _write_marker

        for sha in _git("-C", str(origin), "rev-list", "--all").split():
            _write_marker(
                _marker(cache_root, cache_shard_key(url), sha), _verdict_key(url, url, None)
            )
    bare = cache_root / "git" / "db_v1" / cache_shard_key(url)
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", "--no-tags", "--template=", str(origin), str(bare))
    _git("--git-dir", str(bare), "update-ref", "refs/heads/main", first)
    _git("--git-dir", str(bare), "reflog", "expire", "--expire=now", "--all")
    _git("--git-dir", str(bare), "gc", "-q", "--prune=now")
    _git("--git-dir", str(bare), "config", "remote.origin.url", url)
    return bare


def _helper(marker: Path) -> str:
    return f"!f() {{ echo hit >> '{marker.as_posix()}'; }}; f"


def _payloads(marker: Path, url: str, tmp: Path) -> dict[str, str]:
    include = tmp / "included.gitconfig"
    include.write_text(f'[credential]\n\thelper = "{_helper(marker)}"\n', encoding="utf-8")
    hooks = tmp / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "post-checkout", "pre-auto-gc"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho hit >> '{marker.as_posix()}'\n", encoding="ascii")
        hook.chmod(0o755)
    command = f"echo hit >> '{marker.as_posix()}'"
    return {
        "credential-helper-on-401": f'[credential]\n\thelper = "{_helper(marker)}"\n',
        "proxy-on-407": (
            f'[http]\n\tproxy = {url}\n[credential]\n\thelper = "{_helper(marker)}"\n'
        ),
        "url-scoped-proxy-and-helper": (
            f'[http "{url}/"]\n\tproxy = {url}\n'
            f'[credential "{url}"]\n\thelper = "{_helper(marker)}"\n'
        ),
        "url-scoped-helper": f'[credential "{url}"]\n\thelper = "{_helper(marker)}"\n',
        "include-path-outside-cache": f"[include]\n\tpath = {include.as_posix()}\n",
        "fsmonitor": f'[core]\n\tfsmonitor = "{command}"\n',
        "hooks-path": f"[core]\n\thooksPath = {hooks.as_posix()}\n",
        "filter-driver": f'[filter "x"]\n\tsmudge = "{command}"\n\tclean = "{command}"\n',
        "ssh-command": f'[core]\n\tsshCommand = "{command}"\n',
        "git-proxy": f'[core]\n\tgitProxy = "{command}"\n',
        "tls-verify-off": "[http]\n\tsslVerify = false\n",
        "url-rewrite": f'[url "{url}/rewritten/"]\n\tinsteadOf = {url}/\n',
    }


@_POSIX_ONLY
@pytest.mark.parametrize(
    "vector",
    [
        "credential-helper-on-401",
        "proxy-on-407",
        "url-scoped-proxy-and-helper",
        "url-scoped-helper",
        "include-path-outside-cache",
        "fsmonitor",
        "hooks-path",
        "filter-driver",
        "ssh-command",
        "git-proxy",
        "tls-verify-off",
        "url-rewrite",
    ],
)
def test_a_cached_bare_with_foreign_config_is_discarded_and_nothing_runs(
    tmp_path: Path, server_url: str, vector: str
) -> None:
    origin, first, second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    cache_root = tmp_path / "cache"
    bare = _cached_bare(cache_root, origin, first, url)
    marker = tmp_path / "marker"
    payload = _payloads(marker, server_url, tmp_path)[vector]
    with (bare / "config").open("a", encoding="utf-8") as config:
        config.write(payload)

    with pytest.raises((RuntimeError, subprocess.SubprocessError)):
        GitCache(cache_root).get_checkout(url, None, locked_sha=second)

    assert not marker.exists(), "a setting from the cached repository ran"
    assert not (bare / "config").exists() or payload not in (bare / "config").read_text(
        encoding="utf-8"
    ), "the cached repository was used"


@_POSIX_ONLY
def test_an_active_hook_in_a_cached_bare_is_discarded(tmp_path: Path, server_url: str) -> None:
    origin, first, second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    bare = _cached_bare(tmp_path / "cache", origin, first, url)
    marker = tmp_path / "marker"
    (bare / "hooks").mkdir(exist_ok=True)
    hook = bare / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho hit >> '{marker.as_posix()}'\n", encoding="ascii")
    hook.chmod(0o755)

    with pytest.raises((RuntimeError, subprocess.SubprocessError)):
        GitCache(tmp_path / "cache").get_checkout(url, None, locked_sha=second)

    assert not marker.exists()
    assert not hook.exists()


def test_an_unmodified_cached_bare_is_used_as_is(tmp_path: Path, server_url: str) -> None:
    origin, first, _second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    bare = _cached_bare(tmp_path / "cache", origin, first, url)
    sentinel = bare / "apm-control"
    sentinel.write_text("kept\n", encoding="ascii")

    checkout = GitCache(tmp_path / "cache").get_checkout(url, None, locked_sha=first)

    assert sentinel.read_text(encoding="ascii") == "kept\n", "an unmodified bare was discarded"
    assert (checkout / "AGENTS.md").read_text(encoding="ascii") == "one\n"
    again = GitCache(tmp_path / "cache").get_checkout(url, None, locked_sha=first)
    assert again == checkout
    assert sentinel.exists()


def test_an_upgraded_cache_without_a_verdict_fails_closed_offline(
    tmp_path: Path, server_url: str
) -> None:
    """A cache from an older APM holds no verdict: the one verification needs the origin."""
    origin, first, _second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    _cached_bare(tmp_path / "cache", origin, first, url, verified=False)

    with pytest.raises(RuntimeError) as excinfo:
        GitCache(tmp_path / "cache").get_checkout(url, None, locked_sha=first)

    message = " ".join(str(excinfo.value).split())
    assert "needs network access once" in message
    assert "pin a branch or tag" in message
    assert not (tmp_path / "cache" / "reachability" / "verified-v2").exists()
    assert not list((tmp_path / "cache").rglob("verified-v2/*"))


def test_a_cached_checkout_with_foreign_config_is_rebuilt(tmp_path: Path) -> None:
    origin, first, _second = _origin(tmp_path)
    cache = GitCache(tmp_path / "cache")
    checkout = cache.get_checkout(str(origin), None, locked_sha=first)
    config = checkout / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("[core]\n\tfsmonitor = true\n")

    rebuilt = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)

    assert "fsmonitor" not in (rebuilt / ".git" / "config").read_text(encoding="utf-8")
    assert (rebuilt / "AGENTS.md").read_text(encoding="ascii") == "one\n"


@pytest.mark.parametrize(
    "plant",
    [
        lambda git_dir: (
            (git_dir / "info").mkdir(exist_ok=True)
            or (git_dir / "info" / "attributes").write_text("* filter=x\n", encoding="ascii")
        ),
        lambda git_dir: (git_dir / "commondir").write_text("..\n", encoding="ascii"),
        lambda git_dir: (
            (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
            or (git_dir / "objects" / "info" / "alternates").write_text(
                str(Path(os.sep) / "elsewhere" / "objects") + "\n", encoding="ascii"
            )
        ),
    ],
    ids=["info-attributes", "commondir", "alternates-outside-cache"],
)
def test_side_files_apm_never_writes_are_refused(tmp_path: Path, plant) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError, require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    db_root = tmp_path / "cache" / "git" / "db_v1"
    require_trusted_cached_repo(
        checkout, bare=False, origin_ok=lambda _url: True, alternates_root=db_root
    )
    plant(checkout / ".git")

    with pytest.raises(UntrustedCachedRepoError):
        require_trusted_cached_repo(
            checkout, bare=False, origin_ok=lambda _url: True, alternates_root=db_root
        )


@_POSIX_ONLY
def test_a_native_snapshot_with_foreign_config_is_refused_and_nothing_runs(
    tmp_path: Path,
) -> None:
    from apm_cli.codex_plugins.client import CodexNativeError
    from apm_cli.codex_plugins.snapshot import NativeSnapshot, repair_generation

    snapshot = tmp_path / "snapshot"
    _git("init", "-q", "-b", "main", "--template=", str(snapshot))
    (snapshot / "file.txt").write_text("x\n", encoding="ascii")
    _git("-C", str(snapshot), "add", "-A")
    _git(
        "-C",
        str(snapshot),
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "-m",
        "one",
    )
    commit = _git("-C", str(snapshot), "rev-parse", "HEAD")
    marker = tmp_path / "marker"
    with (snapshot / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = \"echo hit >> '{marker.as_posix()}'\"\n")

    with pytest.raises(CodexNativeError):
        repair_generation(NativeSnapshot(snapshot, commit, commit))

    assert not marker.exists()


def _user_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Give the user a global Git config (user-owned, outside the cache)."""
    user_config = tmp_path / "user.gitconfig"
    user_config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user_config))


@_POSIX_ONLY
def test_a_local_filter_is_refused_even_when_the_user_defines_the_same_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benign = tmp_path / "benign"
    marker = tmp_path / "marker"
    _user_global(
        tmp_path,
        monkeypatch,
        f'[filter "lfs"]\n\tsmudge = "cat; echo hit >> \'{benign.as_posix()}\'"\n'
        "\tclean = cat\n\trequired = false\n",
    )
    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    planted = f'[filter "lfs"]\n\tsmudge = "echo hit >> \'{marker.as_posix()}\'"\n'
    with (checkout / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(planted)

    rebuilt = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)

    assert planted not in (rebuilt / ".git" / "config").read_text(encoding="utf-8")
    assert (rebuilt / "AGENTS.md").read_text(encoding="ascii") == "one\n"
    assert not marker.exists(), "a filter from the cached repository ran"


@_POSIX_ONLY
def test_a_local_helper_is_refused_alongside_a_user_global_helper(
    tmp_path: Path, server_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user's own helper is configured; the clone still fails cleanly on the
    # 401 (as it does without the planted keys), and only the user's config applies.
    marker = tmp_path / "marker"
    _user_global(
        tmp_path, monkeypatch, f'[credential]\n\thelper = "{_helper(tmp_path / "benign")}"\n'
    )
    origin, first, second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    bare = _cached_bare(tmp_path / "cache", origin, first, url)
    with (bare / "config").open("a", encoding="utf-8") as config:
        config.write(
            f'[credential]\n\thelper = "{_helper(marker)}"\n'
            f'[credential "{server_url}"]\n\thelper = "{_helper(marker)}"\n'
        )

    with pytest.raises(RuntimeError):
        GitCache(tmp_path / "cache").get_checkout(url, None, locked_sha=second)

    assert not marker.exists(), "a helper from the cached repository ran"
    assert not bare.exists() or "credential" not in (bare / "config").read_text(encoding="utf-8")


def test_an_untrusted_checkout_is_evicted_only_under_its_shard_lock(tmp_path: Path) -> None:
    import apm_cli.cache.git_cache as git_cache_module

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    with (checkout / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("[core]\n\tfsmonitor = true\n")
    present_when_locked: list[bool] = []
    real_lock = git_cache_module.shard_lock

    def observed_lock(path):
        if Path(path) == checkout:
            present_when_locked.append(checkout.exists())
        return real_lock(path)

    with patch.object(git_cache_module, "shard_lock", observed_lock):
        rebuilt = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)

    # The unlocked hit probe left the checkout for the locked re-probe to evict.
    assert present_when_locked[:1] == [True]
    assert "fsmonitor" not in (rebuilt / ".git" / "config").read_text(encoding="utf-8")


def test_a_refused_config_never_prints_a_credential(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from apm_cli.cache.cached_repo import cached_repo_is_trusted

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    with (checkout / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write('[http "https://user:s3cret-token@example.com/"]\n\tproxy = http://x\n')

    with caplog.at_level("WARNING"):
        assert not cached_repo_is_trusted(checkout, bare=False, origin_ok=lambda _url: True)

    assert "s3cret-token" not in caplog.text
    assert "Discarding cached git repository" in caplog.text


def test_alternates_chained_through_another_cache_entry_are_refused(tmp_path: Path) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError, require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    db_root = tmp_path / "cache" / "git" / "db_v1"
    relay = db_root / "relay" / "objects"
    (relay / "info").mkdir(parents=True)
    (relay / "info" / "alternates").write_text(str(tmp_path / "elsewhere") + "\n", encoding="ascii")
    (checkout / ".git" / "objects" / "info" / "alternates").write_text(
        str(relay) + "\n", encoding="ascii"
    )

    with pytest.raises(UntrustedCachedRepoError, match="chained"):
        require_trusted_cached_repo(
            checkout, bare=False, origin_ok=lambda _url: True, alternates_root=db_root
        )


@_POSIX_ONLY
def test_ref_lookup_never_reads_an_untrusted_bare(tmp_path: Path, server_url: str) -> None:
    origin, first, _second = _origin(tmp_path)
    url = f"{server_url}/o/r.git"
    bare = _cached_bare(tmp_path / "cache", origin, first, url)
    cache = GitCache(tmp_path / "cache")
    assert cache.bare_ref_sha(url, "main") == first
    marker = tmp_path / "marker"
    with (bare / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = \"echo hit >> '{marker.as_posix()}'\"\n")

    assert cache.bare_ref_sha(url, "main") is None
    assert not marker.exists()


@_POSIX_ONLY
def test_an_alternate_is_checked_exactly_as_git_reads_it(tmp_path: Path) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError, require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    db_root = tmp_path / "cache" / "git" / "db_v1"
    inside = db_root / "entry" / "objects"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside-objects"
    outside.mkdir()
    (db_root / "entry" / "objects ").symlink_to(outside, target_is_directory=True)
    (checkout / ".git" / "objects" / "info" / "alternates").write_text(
        f"{inside} \n", encoding="ascii"
    )

    with pytest.raises(UntrustedCachedRepoError, match="leave the cache"):
        require_trusted_cached_repo(
            checkout, bare=False, origin_ok=lambda _url: True, alternates_root=db_root
        )
    assert (db_root / "entry" / "objects ").resolve() == outside.resolve()


@_POSIX_ONLY
@pytest.mark.parametrize("name", ["objects", "refs/heads", "info/sparse-checkout"])
def test_git_state_linked_out_of_the_repository_is_refused(tmp_path: Path, name: str) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError, require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    planted = checkout / ".git" / name
    elsewhere = tmp_path / "external" / name
    elsewhere.parent.mkdir(parents=True)
    if planted.exists():
        planted.rename(elsewhere)
    else:
        elsewhere.write_text("/*\n", encoding="ascii")
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(elsewhere, target_is_directory=elsewhere.is_dir())

    with pytest.raises(UntrustedCachedRepoError, match="not a plain file or directory"):
        require_trusted_cached_repo(
            checkout,
            bare=False,
            origin_ok=lambda _url: True,
            alternates_root=tmp_path / "cache" / "git" / "db_v1",
        )
    assert planted.is_symlink()


@_POSIX_ONLY
def test_a_snapshot_without_a_valid_head_never_reaches_the_enclosing_project(
    tmp_path: Path,
) -> None:
    from apm_cli.codex_plugins.client import CodexNativeError
    from apm_cli.codex_plugins.snapshot import NativeSnapshot, repair_generation

    project = tmp_path / "project"
    _git("init", "-q", "-b", "main", "--template=", str(project))
    (project / "work.txt").write_text("one\n", encoding="ascii")
    _git("-C", str(project), "add", "-A")
    _git("-C", str(project), "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "one")
    before = _git("-C", str(project), "rev-parse", "HEAD")
    (project / "work.txt").write_text("staged\n", encoding="ascii")
    _git("-C", str(project), "add", "-A")
    snapshot = project / "apm_modules" / "snapshot"
    _git("init", "-q", "-b", "main", "--template=", str(snapshot))
    (snapshot / ".git" / "HEAD").unlink()  # only the allow-listed config is left that matters

    with pytest.raises(CodexNativeError):
        repair_generation(NativeSnapshot(snapshot, before, before))

    assert _git("-C", str(project), "rev-parse", "HEAD") == before
    assert _git("-C", str(project), "diff", "--cached", "--name-only") == "work.txt"


@_POSIX_ONLY
@pytest.mark.parametrize("name", ["config", "objects/info/alternates"])
def test_a_pipe_in_place_of_git_state_is_refused_without_blocking(
    tmp_path: Path, name: str
) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError, require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    target = checkout / ".git" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    os.mkfifo(target)
    outcome: list[BaseException | None] = []

    def check() -> None:
        try:
            require_trusted_cached_repo(
                checkout,
                bare=False,
                origin_ok=lambda _url: True,
                alternates_root=tmp_path / "cache" / "git" / "db_v1",
            )
            outcome.append(None)
        except UntrustedCachedRepoError as error:
            outcome.append(error)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), f"validation blocked on a pipe at {name}"
    assert outcome and isinstance(outcome[0], UntrustedCachedRepoError)
    assert target.is_fifo() if hasattr(target, "is_fifo") else target.exists()


def test_a_branch_name_with_a_quote_keeps_its_checkout(tmp_path: Path) -> None:
    from apm_cli.cache.cached_repo import require_trusted_cached_repo

    origin = tmp_path / "origin"
    _git("init", "-q", "-b", 'release"prod', "--template=", str(origin))
    (origin / "AGENTS.md").write_text("one\n", encoding="ascii")
    _git("-C", str(origin), "add", "-A")
    _git("-C", str(origin), "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "one")
    clone = tmp_path / "clone"
    _git("clone", "-q", "--template=", str(origin), str(clone))
    config = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert '[branch "release\\"prod"]' in config

    require_trusted_cached_repo(clone, bare=False, origin_ok=lambda _url: True)


def test_a_head_with_trailing_whitespace_is_read_as_git_reads_it(tmp_path: Path) -> None:
    from apm_cli.cache.cached_repo import require_trusted_cached_repo

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    head = checkout / ".git" / "HEAD"
    head.write_bytes(head.read_bytes().rstrip() + b"\r\n")

    require_trusted_cached_repo(
        checkout,
        bare=False,
        origin_ok=lambda _url: True,
        alternates_root=tmp_path / "cache" / "git" / "db_v1",
    )
    assert head.read_bytes().endswith(b"\r\n")


def test_a_snapshot_apm_creates_passes_the_gate_on_every_later_command(tmp_path: Path) -> None:
    from apm_cli.codex_plugins.snapshot import NativeSnapshot, _git, _index_files, repair_generation

    root = tmp_path / "world" / "apm_modules" / "snapshot"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text("{}\n", encoding="ascii")
    _git(root, ["init", "--initial-branch=main", "--template="])
    _index_files(root, [root / "plugin.json"])
    _git(root, ["commit", "-m", "one"])
    content = _git(root, ["rev-parse", "HEAD"])

    repaired = repair_generation(NativeSnapshot(root, content, content))

    assert repaired.content_commit == content
    assert repaired.catalog_commit != content
    assert (root / ".git" / "HEAD").read_text(encoding="ascii").startswith("ref: refs/heads/main")


def _gate(checkout: Path, tmp_path: Path) -> None:
    from apm_cli.cache.cached_repo import require_trusted_cached_repo

    require_trusted_cached_repo(
        checkout,
        bare=False,
        origin_ok=lambda _url: True,
        alternates_root=tmp_path / "cache" / "git" / "db_v1",
    )


@_POSIX_ONLY
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("where", ["objects/ab/" + "c" * 38, "objects/pack/pack-x.pack"])
def test_object_payload_entries_must_be_plain_files(tmp_path: Path, where: str, kind: str) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    planted = checkout / ".git" / where
    planted.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(planted)
    else:
        outside = tmp_path / "outside-object"
        outside.write_bytes(b"x")
        planted.symlink_to(outside)
    outcome: list[BaseException | None] = []

    def check() -> None:
        try:
            _gate(checkout, tmp_path)
            outcome.append(None)
        except UntrustedCachedRepoError as error:
            outcome.append(error)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), f"the gate blocked on {where}"
    assert outcome and isinstance(outcome[0], UntrustedCachedRepoError)
    assert planted.is_symlink() or planted.is_fifo()


@pytest.mark.parametrize("name", ["objects", "refs"])
def test_a_repository_without_its_object_or_ref_directory_is_refused(
    tmp_path: Path, name: str
) -> None:
    import shutil

    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    shutil.rmtree(checkout / ".git" / name)

    with pytest.raises(UntrustedCachedRepoError, match=f"{name} is missing"):
        _gate(checkout, tmp_path)
    assert not (checkout / ".git" / name).exists()


def test_a_symbolic_head_outside_refs_is_refused_and_never_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError
    from apm_cli.cache.integrity import verify_checkout_sha

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    (tmp_path / "outside.txt").write_text("outsidecontent\n", encoding="ascii")
    head = checkout / ".git" / "HEAD"
    relative = os.path.relpath(tmp_path / "outside.txt", checkout / ".git" / "refs")
    head.write_text(f"ref: refs/{relative}\n", encoding="ascii")

    with pytest.raises(UntrustedCachedRepoError):
        _gate(checkout, tmp_path)
    with caplog.at_level("WARNING", logger="apm_cli.cache.integrity"):
        assert verify_checkout_sha(checkout, first) is False
    assert "outsidecon" not in caplog.text
    assert head.read_text(encoding="ascii").startswith("ref: refs/..")


@pytest.mark.parametrize("name", ["objects", "refs"])
def test_a_broken_checkout_inside_a_project_never_reaches_the_project(
    tmp_path: Path, name: str
) -> None:
    import shutil

    from apm_cli.cache.cached_repo import cached_repo_env
    from apm_cli.cache.git_cache import _pin_sparse_completeness
    from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

    project = tmp_path / "project"
    _git("init", "-q", "-b", "main", "--template=", str(project))
    config_before = (project / ".git" / "config").read_bytes()
    origin, first, _second = _origin(tmp_path)
    cache_root = project / ".apm-cache"
    checkout = GitCache(cache_root).get_checkout(str(origin), None, locked_sha=first)
    shutil.rmtree(checkout / ".git" / name)

    rebuilt = GitCache(cache_root).get_checkout(str(origin), None, locked_sha=first)
    assert (rebuilt / ".git" / name).is_dir()
    shutil.rmtree(rebuilt / ".git" / name)
    _pin_sparse_completeness(
        get_git_executable(), rebuilt, env=cached_repo_env(git_subprocess_env(None), rebuilt)
    )

    assert (project / ".git" / "config").read_bytes() == config_before
    assert not (project / ".git" / "index").exists()


@pytest.mark.parametrize(
    "char", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r", "\x00"]
)
@pytest.mark.parametrize("key", ["url", "merge"])
def test_config_lines_are_split_only_where_git_splits_them(
    tmp_path: Path, char: str, key: str
) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    config = checkout / ".git" / "config"
    if key == "url":
        planted = f'[remote "origin"]\n\turl = {origin}{char}[core]\n'
    else:
        planted = f'[branch "main"]\n\tmerge = refs/heads/main{char}[core]\n'
    with config.open("a", encoding="utf-8", newline="") as handle:
        handle.write(planted)

    with pytest.raises(UntrustedCachedRepoError):
        _gate(checkout, tmp_path)
    assert char.encode("utf-8") in config.read_bytes()


@pytest.mark.parametrize(
    "entry",
    [
        "../../../../elsewhere/objects",
        "C:/elsewhere/objects",
        "//host/share/objects",
        "\\\\host\\share\\objects",
        "OTHER_SHARD",
    ],
    ids=["relative-up", "drive", "unc-slash", "unc-backslash", "other-shard-chained"],
)
def test_alternates_never_reach_outside_the_cache(tmp_path: Path, entry: str) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    db_root = tmp_path / "cache" / "git" / "db_v1"
    if entry == "OTHER_SHARD":
        other = db_root / "other-shard" / "objects"
        (other / "info").mkdir(parents=True)
        (other / "info" / "alternates").write_text(str(tmp_path / "x") + "\n", encoding="ascii")
        entry = os.path.relpath(other, checkout / ".git" / "objects")
    alternates = checkout / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(entry + "\n", encoding="ascii")

    with pytest.raises(UntrustedCachedRepoError):
        _gate(checkout, tmp_path)
    assert alternates.read_text(encoding="ascii") == entry + "\n"


@pytest.mark.parametrize("absolute", [True, False], ids=["absolute", "relative"])
def test_a_gitfile_checkout_is_refused(tmp_path: Path, absolute: bool) -> None:
    import shutil

    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    elsewhere = tmp_path / "elsewhere.git"
    shutil.move(str(checkout / ".git"), str(elsewhere))
    target = str(elsewhere) if absolute else os.path.relpath(elsewhere, checkout)
    (checkout / ".git").write_text(f"gitdir: {target}\n", encoding="ascii")

    with pytest.raises(UntrustedCachedRepoError):
        _gate(checkout, tmp_path)
    assert (checkout / ".git").is_file()


def _plant_special(git_dir: Path, case: str, tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "file").write_text("x\n", encoding="ascii")
    plants = {
        "fifo-in-refs": lambda: (
            os.mkfifo(git_dir / "refs" / "heads" / "pipe") or (git_dir / "refs" / "heads" / "pipe")
        ),
        "fifo-in-logs": lambda: (
            ((git_dir / "logs").mkdir(exist_ok=True) or os.mkfifo(git_dir / "logs" / "pipe"))
            or (git_dir / "logs" / "pipe")
        ),
        "symlink-loop": lambda: (git_dir / "loop").symlink_to(git_dir / "loop") or git_dir / "loop",
        "hooks-link": lambda: (
            (
                shutil_rmtree(git_dir / "hooks"),
                (git_dir / "hooks").symlink_to(outside, target_is_directory=True),
            )
            and git_dir / "hooks"
        ),
        "config-link": lambda: _swap_for_link(git_dir / "config", outside / "file"),
        "packed-refs-link": lambda: _swap_for_link(git_dir / "packed-refs", outside / "file"),
        "index-link": lambda: _swap_for_link(git_dir / "index", outside / "file"),
        "head-fifo": lambda: _swap_for_fifo(git_dir / "HEAD"),
        "head-empty": lambda: (
            (git_dir / "HEAD").write_text("", encoding="ascii") or git_dir / "HEAD"
        ),
        "head-missing": lambda: (git_dir / "HEAD").unlink() or git_dir / "HEAD",
        "head-nul": lambda: _write(git_dir / "HEAD", b"ref: refs/heads/main\x00\n"),
    }
    return plants[case]()


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _swap_for_link(path: Path, target: Path) -> Path:
    if path.exists():
        path.unlink()
    path.symlink_to(target)
    return path


def _swap_for_fifo(path: Path) -> Path:
    path.unlink()
    os.mkfifo(path)
    return path


@_POSIX_ONLY
@pytest.mark.parametrize(
    "case",
    [
        "fifo-in-refs",
        "fifo-in-logs",
        "symlink-loop",
        "hooks-link",
        "config-link",
        "packed-refs-link",
        "index-link",
        "head-fifo",
        "head-empty",
        "head-missing",
        "head-nul",
    ],
)
def test_special_or_incomplete_git_state_is_refused_without_blocking(
    tmp_path: Path, case: str
) -> None:
    from apm_cli.cache.cached_repo import UntrustedCachedRepoError

    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    planted = _plant_special(checkout / ".git", case, tmp_path)
    outcome: list[BaseException | None] = []

    def check() -> None:
        try:
            _gate(checkout, tmp_path)
            outcome.append(None)
        except UntrustedCachedRepoError as error:
            outcome.append(error)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), f"the gate blocked on {case}"
    assert outcome and isinstance(outcome[0], UntrustedCachedRepoError), case
    assert planted.is_symlink() or planted.exists() or case == "head-missing"


def test_an_entry_git_removes_while_it_is_listed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, first, _second = _origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), None, locked_sha=first)
    lock = checkout / ".git" / "objects" / "maintenance.lock"
    lock.write_text("", encoding="ascii")
    real_lstat = os.lstat

    def lstat_after_removal(path, *args, **kwargs):
        if str(path).endswith("maintenance.lock"):
            lock.unlink(missing_ok=True)  # Git's background maintenance finished
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat_after_removal)
    _gate(checkout, tmp_path)
    assert not lock.exists()
