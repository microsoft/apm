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


def _cached_bare(cache_root: Path, origin: Path, first: str, url: str) -> Path:
    """Lay down the bare APM would cache for *url*, holding only the first commit."""
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
