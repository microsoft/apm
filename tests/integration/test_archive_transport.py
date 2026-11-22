"""Archive transport: one verified codeload archive instead of a git clone.

The archive is accepted only when its recomputed git tree equals the commit's
tree, so the landed shard holds the bytes a git checkout writes. Anything
else (tampered content, unsafe members, attributes) must leave the cache
untouched so the git transport runs.
"""

from __future__ import annotations

import gzip
import io
import os
import subprocess
import tarfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from apm_cli.cache.git_cache import GitCache
from apm_cli.deps import archive_checkout
from apm_cli.deps import archive_transport as at
from apm_cli.deps.archive_checkout import try_archive_tree
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-git-transport-selection",
        "P-OWN-archive-extraction-limits",
        "P-OWN-package-content-hash",
    ),
]

URL = "https://github.com/acme/pkg"


def _git_bytes(repo: Path, *args: str) -> bytes:
    """Run git with no system/global config, like the server that builds codeload archives."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", *args],
        check=True,
        capture_output=True,
        env=env,
    ).stdout


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """A real repository with nested dirs, an exec bit and git's tricky sort order."""
    root = tmp_path / "repo"
    (root / "a" / "deep").mkdir(parents=True)
    (root / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
    (root / "a.b").write_text("sorts before the a/ directory\n")
    (root / "a" / "deep" / "x.md").write_bytes(b"# x\r\nCRLF committed\r\n")
    (root / "a" / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    _git(root, "init", "-q")
    _git(root, "-c", "core.autocrlf=false", "add", "-A")
    _git(root, "update-index", "--chmod=+x", "a/run.sh")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    archive = _git_bytes(root, "archive", "--format=tar.gz", f"--prefix=acme-pkg-{sha[:7]}/", sha)
    return SimpleNamespace(root=root, sha=sha, tree=tree, archive=archive)


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))}
        self._body = body

    def iter_content(self, _size: int):
        yield self._body

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_run() -> None:
    at.reset_run_state()


def _serve(monkeypatch, *, archive: bytes, tree: str) -> list[str]:
    """Answer codeload and git/trees at the HTTP boundary; return the URLs requested."""
    requested: list[str] = []

    def fake_request(_session, method: str, url: str, **_kwargs) -> _Response:
        requested.append(url)
        if "/git/trees/" in url:
            return _Response(200, f'{{"sha": "{tree}", "tree": []}}'.encode())
        return _Response(200, archive)

    monkeypatch.setattr(at, "origin_bounded_request", fake_request)
    return requested


def _fetch(cache: GitCache, sha: str, limits: at.ArchiveLimits = at.DEFAULT_LIMITS) -> Path:
    return at.fetch_verified_tree(
        cache,
        url=URL,
        api_base="https://api.github.com",
        owner="acme",
        repo="pkg",
        commit=sha,
        api_headers={},
        limits=limits,
    )


def _tarball(sha: str, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": sha}
    ) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _file(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    return info, data


def _assert_nothing_landed(cache: GitCache, sha: str) -> None:
    assert not cache.has_full_checkout(URL, sha)
    shard = cache.cache_root / "git" / "checkouts_v1"
    assert not [p for p in shard.rglob("*") if p.is_file() and not p.name.endswith(".lock")]


@pytest.mark.windows_compat
def test_verified_archive_lands_a_cache_hit_with_git_checkout_bytes(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    requested = _serve(monkeypatch, archive=repo.archive, tree=repo.tree)

    landed = _fetch(cache, repo.sha)

    assert sorted(u.split("/")[2] for u in requested) == ["api.github.com", "codeload.github.com"]
    # The landed shard is an ordinary cache hit: no clone, no network.
    assert cache.get_checkout(URL, repo.sha, locked_sha=repo.sha) == landed
    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
        "clone",
        "-q",
        str(repo.root),
        str(clone),
    )
    assert compute_package_hash(landed) == compute_package_hash(clone)
    assert (landed / "a" / "deep" / "x.md").read_bytes() == b"# x\r\nCRLF committed\r\n"
    assert os.access(landed / "a" / "run.sh", os.X_OK) or os.name == "nt"
    # Landing implies the recomputed tree (nested dirs, exec bit, git's "a.b" <
    # "a/" order) equals git's own; the shard records it.
    config = (landed / ".git" / "config").read_text(encoding="ascii")
    assert f"tree = {repo.tree}" in config
    assert (landed / ".git" / "HEAD").read_text(encoding="ascii").strip() == repo.sha


def test_archive_digest_mismatch_rejected_before_deployment(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    tampered = _tarball(
        repo.sha,
        [_file(f"acme-pkg-{repo.sha[:7]}/apm.yml", b"name: pkg\nversion: 6.6.6\n")],
    )
    _serve(monkeypatch, archive=tampered, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="does not match the commit's tree"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_file("p/../escape.txt", b"x"), "non-canonical archive path"),
        (_file("/etc/escape.txt", b"x"), "non-canonical archive path"),
        (_file("p/a/./b.txt", b"x"), "non-canonical archive path"),
    ],
    ids=["traversal", "absolute", "dot-segment"],
)
def test_unsafe_member_paths_rejected(repo, tmp_path, monkeypatch, member, reason):
    cache = GitCache(tmp_path / "cache")
    archive = _tarball(repo.sha, [_file("p/apm.yml", b"name: pkg\n"), member])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match=reason):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_link_members_rejected(repo, tmp_path, monkeypatch, kind):
    cache = GitCache(tmp_path / "cache")
    link = tarfile.TarInfo("p/link")
    link.type = kind
    link.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else "p/apm.yml"
    archive = _tarball(repo.sha, [_file("p/apm.yml", b"name: pkg\n"), (link, None)])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="Refusing archive member type"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (at.ArchiveLimits(uncompressed_bytes=64 * 1024), "size limit"),
        (at.ArchiveLimits(entries=3), "more than 3 entries"),
    ],
    ids=["uncompressed-bytes", "member-count"],
)
def test_decompression_bounds_fall_back_before_anything_lands(
    repo, tmp_path, monkeypatch, limits, reason
):
    """A zip-bomb shaped archive (1 MiB of zeros, a few KiB gzipped) or too many members."""
    cache = GitCache(tmp_path / "cache")
    members = [_file(f"p/f{i}.md", b"x") for i in range(5)]
    members.append(_file("p/bomb.bin", bytes(1024 * 1024)))
    archive = _tarball(repo.sha, members)
    assert len(archive) < 16 * 1024
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match=reason):
        _fetch(cache, repo.sha, limits)
    _assert_nothing_landed(cache, repo.sha)


def test_compressed_download_bound_falls_back(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    _serve(monkeypatch, archive=repo.archive, tree=repo.tree)
    limits = at.ArchiveLimits(compressed_bytes=len(repo.archive) - 1)

    with pytest.raises(at.ArchiveUnavailable, match="larger than the archive transport cap"):
        _fetch(cache, repo.sha, limits)
    _assert_nothing_landed(cache, repo.sha)


def test_archive_of_another_commit_rejected(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    _serve(monkeypatch, archive=repo.archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="requested commit"):
        _fetch(cache, "f" * 40)
    _assert_nothing_landed(cache, "f" * 40)


def test_gitattributes_keeps_the_git_transport(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    archive = _tarball(repo.sha, [_file("p/.gitattributes", b"* text eol=crlf\n")])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="gitattributes"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


def test_unreachable_codeload_disables_archives_for_the_run(tmp_path, monkeypatch):
    calls: list[str] = []

    def refused(_session, _method, url, **_kwargs):
        calls.append(url)
        raise requests.exceptions.ConnectionError("blocked")

    monkeypatch.setattr(at, "origin_bounded_request", refused)
    cache = GitCache(tmp_path / "cache")
    for _ in range(2):
        with pytest.raises(at.ArchiveUnavailable):
            _fetch(cache, "a" * 40)
    assert len(calls) == 2  # first attempt only (archive + trees); none afterwards
    _assert_nothing_landed(cache, "a" * 40)


def _file_mode(name: str, data: bytes, mode: int) -> tuple[tarfile.TarInfo, bytes]:
    info, data = _file(name, data)
    info.mode = mode
    return info, data


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_exec_bit_comes_from_the_verified_tree_not_the_archive_mode(repo, tmp_path, monkeypatch):
    """0654 hashes as git mode 100755; the landed file must be 0755, not 0654."""
    cache = GitCache(tmp_path / "cache")
    files = {
        "apm.yml": (b"name: pkg\nversion: 1.0.0\n", 0o644),
        "a.b": (b"sorts before the a/ directory\n", 0o600),
        "a/deep/x.md": (b"# x\r\nCRLF committed\r\n", 0o644),
        "a/run.sh": (b"#!/bin/sh\necho hi\n", 0o654),
    }
    members = [_file_mode(f"p/{name}", data, mode) for name, (data, mode) in files.items()]
    _serve(monkeypatch, archive=_tarball(repo.sha, members), tree=repo.tree)

    landed = _fetch(cache, repo.sha)

    assert (landed / "a" / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (landed / "a.b").stat().st_mode & 0o777 == 0o644


def test_directory_members_count_toward_the_uncompressed_bound(repo, tmp_path, monkeypatch):
    cache = GitCache(tmp_path / "cache")
    directory = tarfile.TarInfo("p/huge")
    directory.type = tarfile.DIRTYPE
    directory.size = 1024 * 1024
    archive = _tarball(repo.sha, [(directory, None), _file("p/apm.yml", b"name: pkg\n")])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="size limit"):
        _fetch(cache, repo.sha, at.ArchiveLimits(uncompressed_bytes=4096))
    _assert_nothing_landed(cache, repo.sha)


# -- routing: only the anonymous public github.com HTTPS attempt uses an archive --


@pytest.fixture
def isolated_git_home(tmp_path, monkeypatch) -> Path:
    """No system/global Git attributes or config: the routing tests own every input."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    (home / ".gitconfig").write_text("")
    for name in (
        "GITHUB_HOST",
        "APM_GITHUB_AUTH_FIRST",
        "APM_GIT_PROTOCOL",
        "APM_ARCHIVE_TRANSPORT",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_ALLOW_PROTOCOL",
    ):
        monkeypatch.delenv(name, raising=False)
    archive_checkout._AMBIENT_ATTRIBUTES.clear()
    return home


def _route(spec: str, tmp_path: Path, monkeypatch, *, status: int = 404) -> list[str]:
    """Run the archive gate for *spec* on a real downloader; return the URLs requested."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.models.apm_package import DependencyReference

    requested: list[str] = []

    def answer(_session, _method, url, **_kwargs):
        requested.append(url)
        return _Response(status, b"")

    monkeypatch.setattr(at, "origin_bounded_request", answer)
    cache = GitCache(tmp_path / "cache")
    result = try_archive_tree(
        GitHubPackageDownloader(),
        cache,
        DependencyReference.parse(spec),
        SimpleNamespace(resolved_commit="a" * 40),
    )
    assert result is None
    _assert_nothing_landed(cache, "a" * 40)
    return requested


def test_public_github_https_dependency_requests_the_archive(
    isolated_git_home, tmp_path, monkeypatch
):
    """Positive control for the routing table; a private repo's 404 falls back to git."""
    requested = _route("acme/pkg#main", tmp_path, monkeypatch, status=404)
    assert sorted(urllib.parse.urlsplit(u).hostname for u in requested) == [
        "api.github.com",
        "codeload.github.com",
    ]
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


@pytest.mark.parametrize(
    ("spec", "rewritten"),
    [
        ("acme/pkg#main", "https://github.com/"),
        ("acme/pkg#main", "https://github.com/acme/pkg.git"),
        ("https://github.com/acme/pkg.git", "https://github.com/acme/pkg.git"),
        ("https://github.com/acme/pkg.git", "https://github.com/"),
        ("https://github.com/acme/pkg", "https://github.com/acme/pkg"),
    ],
    ids=[
        "shorthand-host-rule",
        "shorthand-dot-git-only-rule",
        "explicit-dot-git-dot-git-rule",
        "explicit-dot-git-host-rule",
        "explicit-bare-exact-rule",
    ],
)
def test_insteadof_rewrite_to_ssh_keeps_the_git_transport(
    isolated_git_home, tmp_path, monkeypatch, spec, rewritten
):
    """SECURITY (regent ruling): any https->ssh rewrite git would apply must win over the archive."""
    target = "git@github.com:" if rewritten.endswith("/") else "git@github.com:acme/pkg.git"
    (isolated_git_home / ".gitconfig").write_text(f'[url "{target}"]\n\tinsteadOf = {rewritten}\n')
    assert _route(spec, tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


@pytest.mark.parametrize(
    ("condition", "included"),
    [
        (
            "hasconfig:remote.*.url:https://github.com/**",
            '[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n',
        ),
        ("gitdir:**/checkouts_v1/**", "[core]\n\tattributesFile = {attributes}\n"),
    ],
    ids=["remote-activated-insteadof", "checkout-activated-attributes"],
)
def test_conditional_git_config_keeps_the_git_transport(
    isolated_git_home, tmp_path, monkeypatch, condition, included
):
    """A rule that only activates inside the clone must still win over the archive."""
    attributes = isolated_git_home / "crlf.attributes"
    attributes.write_text("* text eol=crlf\n")
    include = isolated_git_home / "conditional.gitconfig"
    include.write_text(included.format(attributes=attributes.as_posix()))
    (isolated_git_home / ".gitconfig").write_text(
        f'[includeIf "{condition}"]\n\tpath = {include.as_posix()}\n'
    )
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_conditional_include_behind_an_unconditional_include_keeps_git(
    isolated_git_home, tmp_path, monkeypatch
):
    """Dotfiles pattern: ~/.gitconfig [include] -> a file holding the includeIf."""
    rewrite = isolated_git_home / "rewrite.gitconfig"
    rewrite.write_text('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    nested = isolated_git_home / "nested.gitconfig"
    nested.write_text(
        f'[includeIf "hasconfig:remote.*.url:https://github.com/**"]\n\tpath = {rewrite.as_posix()}\n'
    )
    (isolated_git_home / ".gitconfig").write_text(f"[include]\n\tpath = {nested.as_posix()}\n")
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_conditional_include_from_environment_config_keeps_git(
    isolated_git_home, tmp_path, monkeypatch
):
    """GIT_CONFIG_COUNT/KEY/VALUE config is read by git inside the clone too."""
    rewrite = isolated_git_home / "rewrite.gitconfig"
    rewrite.write_text('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv(
        "GIT_CONFIG_KEY_0", "includeIf.hasconfig:remote.*.url:https://github.com/**.path"
    )
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", rewrite.as_posix())
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_process_scoped_attributes_do_not_share_a_cached_verdict(
    isolated_git_home, tmp_path, monkeypatch
):
    """Same HOME, then GIT_CONFIG_COUNT sets core.attributesFile: the verdict is not reused."""
    assert _route("acme/pkg#main", tmp_path, monkeypatch)  # clean: the archive is tried
    crlf = isolated_git_home / "crlf.attributes"
    crlf.write_text("* text eol=crlf\n")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.attributesFile")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", crlf.as_posix())
    assert _route("acme/pkg#main", tmp_path / "second", monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "second" / "cache"), "a" * 40)


def test_project_local_config_does_not_hide_global_attributes(
    isolated_git_home, tmp_path, monkeypatch
):
    """The install project's own config never applies inside the cache clone."""
    (isolated_git_home / ".config" / "git").mkdir()
    (isolated_git_home / ".config" / "git" / "attributes").write_text("* text eol=crlf\n")
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "core.attributesFile", os.devnull)
    monkeypatch.chdir(project)
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_relative_attributes_file_keeps_the_git_transport(isolated_git_home, tmp_path, monkeypatch):
    """Git resolves a relative core.attributesFile in the clone's context: unprovable, so git."""
    (isolated_git_home / ".gitconfig").write_text("[core]\n\tattributesFile = rel/attributes\n")
    project = tmp_path / "project"
    (project / "rel").mkdir(parents=True)
    monkeypatch.chdir(project)  # no rules here, so a cwd-relative read would wrongly pass
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


@pytest.mark.parametrize(
    ("env", "config"),
    [
        ({"GIT_ALLOW_PROTOCOL": "ssh"}, ""),
        ({"GIT_ALLOW_PROTOCOL": "file:ssh"}, ""),
        ({}, '[protocol "https"]\n\tallow = never\n'),
        ({}, "[protocol]\n\tallow = never\n"),
        ({}, '[protocol "https"]\n\tallow = user\n'),
    ],
    ids=["allow-list-ssh", "allow-list-file-ssh", "https-never", "all-never", "https-user"],
)
def test_git_protocol_policy_against_https_keeps_git(
    isolated_git_home, tmp_path, monkeypatch, env, config
):
    """Git would refuse or restrict HTTPS here; the archive must not fetch it either."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (isolated_git_home / ".gitconfig").write_text(config)
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_ambient_git_attributes_keep_the_git_transport(isolated_git_home, tmp_path, monkeypatch):
    """A global attributes file could make git write other bytes than the archive."""
    (isolated_git_home / ".config" / "git").mkdir()
    (isolated_git_home / ".config" / "git" / "attributes").write_text("* text eol=crlf\n")
    assert _route("acme/pkg#main", tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


@pytest.mark.parametrize(
    ("spec", "env"),
    [
        ("acme.ghe.com/acme/pkg", {}),
        ("ghes.example.com/acme/pkg", {"GITHUB_HOST": "ghes.example.com"}),
        ("dev.azure.com/org/proj/_git/pkg", {}),
        ("git@github.com:acme/pkg.git", {}),
        ("https://github.com:8443/acme/pkg.git", {}),
        ("http://github.com/acme/pkg.git", {}),
        ("acme/pkg/skills/one", {}),
        ("acme/pkg#main", {"APM_GITHUB_AUTH_FIRST": "1"}),
        ("acme/pkg#main", {"APM_GIT_PROTOCOL": "ssh"}),
    ],
    ids=[
        "ghe-cloud",
        "ghes",
        "ado",
        "ssh",
        "custom-port",
        "insecure",
        "virtual",
        "auth-first",
        "ssh-pref",
    ],
)
def test_other_routes_keep_the_git_transport(isolated_git_home, tmp_path, monkeypatch, spec, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _route(spec, tmp_path, monkeypatch) == []
    _assert_nothing_landed(GitCache(tmp_path / "cache"), "a" * 40)


def test_codeload_redirect_is_refused(isolated_git_home, tmp_path, monkeypatch):
    """No redirect is followed off the requested URL: a 3xx means git, nothing lands."""
    seen: list[str] = []
    responses: list[requests.Response] = []

    class _Tracked(requests.Response):
        released = False

        def close(self) -> None:
            self.released = True
            super().close()

    class _Redirecting(requests.Session):
        def request(self, method, url, **_kwargs):
            seen.append(url)
            response = _Tracked()
            responses.append(response)
            response.status_code = 302
            response.headers["Location"] = "https://elsewhere.example/acme/pkg.tar.gz"
            response.url = url
            response._content = b""
            return response

    monkeypatch.setattr(at, "CredentialScopedSession", lambda **_kwargs: _Redirecting())
    at.reset_run_state()
    cache = GitCache(tmp_path / "cache")
    with pytest.raises(at.ArchiveUnavailable, match="redirect refused"):
        _fetch(cache, "a" * 40)
    assert seen and not any("elsewhere" in url for url in seen)
    assert responses and all(r.released for r in responses), (
        "a refused redirect leaks its connection"
    )
    _assert_nothing_landed(cache, "a" * 40)


def _metadata_bomb(sha: str, kind: str) -> bytes:
    """A tiny gzip whose tar metadata (PAX value or GNU long name) inflates to 1 MiB."""
    buffer = io.BytesIO()
    big = "A" * (1024 * 1024)
    pax = kind == "pax"
    options = (
        {"format": tarfile.PAX_FORMAT, "pax_headers": {"comment": sha, "apm.bomb": big}}
        if pax
        else {"format": tarfile.GNU_FORMAT}
    )
    with tarfile.open(fileobj=buffer, mode="w:gz", **options) as tar:
        info, data = _file("p/apm.yml" if pax else "p/" + big, b"x")
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize("kind", ["pax", "gnu-longname"])
def test_metadata_bomb_is_bounded_before_tar_parsing(repo, tmp_path, monkeypatch, kind):
    """PAX/GNU extension headers count toward the bound; nothing is parsed past it."""
    cache = GitCache(tmp_path / "cache")
    archive = _metadata_bomb(repo.sha, kind)
    assert len(archive) < 16 * 1024
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="size limit"):
        _fetch(cache, repo.sha, at.ArchiveLimits(uncompressed_bytes=64 * 1024))
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.parametrize(
    "directory", ["p//a/", "p/a/./deep/", "p/a/../a/", "p/a//", "p/" + "d" * 120 + "//"]
)
def test_non_canonical_directory_members_are_rejected(repo, tmp_path, monkeypatch, directory):
    """Directory members obey the same canonical-path contract as files."""
    cache = GitCache(tmp_path / "cache")
    member = tarfile.TarInfo(directory)
    member.type = tarfile.DIRTYPE
    archive = _tarball(repo.sha, [(member, None), _file("p/apm.yml", b"name: pkg\n")])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="non-canonical archive path"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.parametrize("directory", ["p/.apm/", "p/a/empty/"], ids=["apm-marker", "nested"])
def test_directory_without_tracked_files_is_rejected(repo, tmp_path, monkeypatch, directory):
    """Git trees cannot hold an empty directory; landing one could change package detection."""
    cache = GitCache(tmp_path / "cache")
    files = {
        "apm.yml": b"name: pkg\nversion: 1.0.0\n",
        "a.b": b"sorts before the a/ directory\n",
        "a/deep/x.md": b"# x\r\nCRLF committed\r\n",
    }
    members = [_file(f"p/{name}", data) for name, data in files.items()]
    members.append(_file_mode("p/a/run.sh", b"#!/bin/sh\necho hi\n", 0o755))
    empty = tarfile.TarInfo(directory)
    empty.type = tarfile.DIRTYPE
    members.append((empty, None))
    _serve(monkeypatch, archive=_tarball(repo.sha, members), tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="directory outside the tree"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.parametrize("status", [403, 429])
def test_throttled_trees_api_disables_archives_for_the_run(repo, tmp_path, monkeypatch, status):
    calls: list[str] = []

    def throttled(_session, _method, url, **_kwargs):
        calls.append(url)
        return _Response(status if "/git/trees/" in url else 200, repo.archive)

    monkeypatch.setattr(at, "origin_bounded_request", throttled)
    cache = GitCache(tmp_path / "cache")
    for _ in range(2):
        with pytest.raises(at.ArchiveUnavailable):
            _fetch(cache, repo.sha)
    assert len(calls) == 2  # the first attempt only; the throttle disables the rest
    _assert_nothing_landed(cache, repo.sha)


def test_archive_landing_records_the_authorized_ref_receipt(
    repo, isolated_git_home, tmp_path, monkeypatch
):
    """Like a clone, a landed archive persists the current-remote ref observation."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.models.apm_package import DependencyReference

    _serve(monkeypatch, archive=repo.archive, tree=repo.tree)
    downloader = GitHubPackageDownloader()
    downloader._tiered_resolver = SimpleNamespace(
        remotely_resolved=lambda dep, sha: sha == repo.sha
    )
    dep = DependencyReference.parse("acme/pkg#main")
    cache = GitCache(tmp_path / "cache")

    landed = try_archive_tree(downloader, cache, dep, SimpleNamespace(resolved_commit=repo.sha))

    assert landed is not None and (landed / "apm.yml").is_file()
    assert cache.read_resolved_ref(dep.to_github_url(), "main") == (True, repo.sha)


@pytest.mark.parametrize(
    "member",
    [
        "p/.GIT/config",
        "p/sub/.git./x",
        "p/git~1/x",
        "p/.Apm-Archive/x",
        "p/a\\b.md",
        "p/.g\u200cit/config",
    ],
    ids=[
        "dot-git-upper",
        "dot-git-trailing-dot",
        "ntfs-short-name",
        "scratch-case",
        "backslash",
        "hfs-ignorable",
    ],
)
def test_names_git_would_refuse_are_rejected(repo, tmp_path, monkeypatch, member):
    cache = GitCache(tmp_path / "cache")
    archive = _tarball(repo.sha, [_file("p/apm.yml", b"name: pkg\n"), _file(member, b"x")])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match=r"reserved path|non-canonical|non-portable"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


class _BrokenStream(_Response):
    def iter_content(self, _size: int):
        yield self._body[:10]
        raise requests.exceptions.ChunkedEncodingError("connection reset mid-body")


def test_mid_stream_failure_disables_archives_for_the_run(repo, tmp_path, monkeypatch):
    """A body that dies mid-stream is a transport failure: later deps do not retry it."""
    calls: list[str] = []

    def answer(_session, _method, url, **_kwargs):
        calls.append(url)
        if "/git/trees/" in url:
            return _Response(200, f'{{"sha": "{repo.tree}"}}'.encode())
        return _BrokenStream(200, repo.archive)

    monkeypatch.setattr(at, "origin_bounded_request", answer)
    cache = GitCache(tmp_path / "cache")
    for _ in range(2):
        with pytest.raises(at.ArchiveUnavailable):
            _fetch(cache, repo.sha)
    assert len(calls) == 2  # archive + trees once; the second dependency sends nothing
    _assert_nothing_landed(cache, repo.sha)


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    "member",
    [
        "p/notes:stream.md",
        "p/nul.txt",
        "p/CON",
        "p/docs/Com1.md",
        "p/lpt9",
        "p/aux .md",
        "p/trailing.",
        "p/trailing ",
        "p/dir./x.md",
    ],
    ids=[
        "ntfs-ads",
        "device-with-extension",
        "device-upper",
        "device-nested-mixed-case",
        "device-lpt",
        "device-trailing-space-stem",
        "trailing-dot",
        "trailing-space",
        "dir-trailing-dot",
    ],
)
def test_windows_unsafe_segments_fall_back_on_every_host(repo, tmp_path, monkeypatch, member):
    """#4819: names Windows would rename or refuse never land, on any OS."""
    cache = GitCache(tmp_path / "cache")
    archive = _tarball(repo.sha, [_file("p/apm.yml", b"name: pkg\n"), _file(member, b"x")])
    _serve(monkeypatch, archive=archive, tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match=r"non-portable|non-canonical"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


def _metadata_heavy(sha: str, kind: str) -> bytes:
    """Within the byte bound, but metadata tarfile would copy into every member."""
    buffer = io.BytesIO()
    many = kind == "many-global-keys"
    headers = {"comment": sha, **({f"apm.k{i}": "v" for i in range(4096)} if many else {})}
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=headers
    ) as tar:
        for i in range(60):
            info, data = _file(f"p/f{i}.md", b"x")
            if kind == "per-member-extended":
                info.pax_headers = {"apm.pad": "A" * 20000}
            tar.addfile(info, io.BytesIO(data))
    if kind != "second-global-header":
        return buffer.getvalue()
    raw = gzip.decompress(buffer.getvalue())
    size = tarfile.nti(raw[124:136])
    record = raw[: tarfile.BLOCKSIZE + -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE]
    return gzip.compress(record + raw)  # a second global header ahead of the first


@pytest.mark.parametrize(
    "kind", ["many-global-keys", "per-member-extended", "second-global-header"]
)
def test_tar_metadata_is_bounded_before_parsing(repo, tmp_path, monkeypatch, kind):
    """tarfile copies global PAX records into every member: bound them before parsing."""
    cache = GitCache(tmp_path / "cache")
    _serve(monkeypatch, archive=_metadata_heavy(repo.sha, kind), tree=repo.tree)

    with pytest.raises(at.ArchiveUnavailable, match="metadata exceeds its limit"):
        _fetch(cache, repo.sha)
    _assert_nothing_landed(cache, repo.sha)


def test_reading_an_archive_does_not_preallocate_the_bound(repo, tmp_path, monkeypatch):
    """Memory follows the archive, not the 256 MiB bound (read sizes and inflate steps)."""
    import tracemalloc

    cache = GitCache(tmp_path / "cache")
    _serve(monkeypatch, archive=repo.archive, tree=repo.tree)
    tracemalloc.start()
    try:
        landed = _fetch(cache, repo.sha)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (landed / "apm.yml").is_file()
    assert peak < 32 * 1024 * 1024, f"peak {peak} bytes for a tiny archive"
