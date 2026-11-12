"""Persistent Git cache honors a declared SSH transport on non-GitHub hosts (#3513).

A cold install of an SSH-declared non-GitHub dependency must not contact the
host over HTTPS before the SSH clone. The test drives the real downloader and a
real :class:`GitCache` against a local repository reached through a Git
``insteadOf`` rewrite of the SSH remote, and records every cache git argv.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.url_normalize import normalize_repo_url
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.apm_package import GitReferenceType, ResolvedReference
from apm_cli.models.dependency.reference import DependencyReference

_HOST = "gitlab.example.com"
_SSH_SPEC = f"git@{_HOST}:org/repo.git#main"


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def ssh_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Serve a local repository for SSH remotes of ``_HOST`` via ``insteadOf``."""
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", "main", cwd=source)
    (source / "apm.yml").write_text("name: repo\nversion: 1.0.0\n", encoding="utf-8")
    (source / ".apm" / "instructions").mkdir(parents=True)
    (source / ".apm" / "instructions" / "a.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nbody\n", encoding="utf-8"
    )
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\nbody\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=source)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=source,
    )
    sha = _git("rev-parse", "HEAD", cwd=source)
    served_root = tmp_path / "served"
    (served_root / "org").mkdir(parents=True)
    _git("clone", "-q", "--bare", str(source), str(served_root / "org" / "repo.git"), cwd=tmp_path)

    served = f"{served_root.as_uri()}/"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "{served}"]\n'
        f"\tinsteadOf = ssh://git@{_HOST}/\n"
        f"\tinsteadOf = git@{_HOST}:\n"
        '[protocol "file"]\n\tallow = always\n'
        "[uploadpack]\n\tallowFilter = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return sha


def _remote_urls(argvs: list[list[str]]) -> list[str]:
    """Return every remote-looking argv token, normalized to URL form."""
    urls = []
    for argv in argvs:
        for token in argv:
            if "://" in token or token.startswith(f"git@{_HOST}:"):
                urls.append(normalize_repo_url(token))
    return urls


@pytest.mark.parametrize(
    ("entry", "marker"),
    [
        (_SSH_SPEC, "apm.yml"),
        ({"git": f"git@{_HOST}:org/repo.git", "path": "skills/demo", "ref": "main"}, "SKILL.md"),
    ],
    ids=["whole-repo", "subdirectory"],
)
def test_cold_install_of_ssh_non_github_dep_never_uses_https(
    tmp_path: Path, ssh_remote: str, entry: str | dict, marker: str
) -> None:
    sha = ssh_remote
    dep = (
        DependencyReference.parse(entry)
        if isinstance(entry, str)
        else DependencyReference.parse_from_dict(entry)
    )
    downloader = GitHubPackageDownloader()
    downloader.persistent_git_cache = GitCache(tmp_path / "cache")
    resolved = ResolvedReference(
        original_ref="main",
        ref_name="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=sha,
    )

    argvs: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(args, *a, **kw):
        argv = [str(x) for x in args]
        argvs.append(argv)
        for url in _remote_urls([argv]):
            parsed = urlparse(url)
            if parsed.hostname == _HOST and parsed.scheme in ("http", "https"):
                raise subprocess.CalledProcessError(128, argv, stderr="web remote refused")
        return real_run(args, *a, **kw)

    def _no_fallback_clone(*_a, **_kw):
        raise AssertionError("cold install must be served by the persistent cache")

    with (
        patch("apm_cli.cache.git_cache.subprocess.run", side_effect=_recording_run),
        patch.object(downloader, "resolve_git_reference", return_value=resolved),
        patch.object(downloader, "_clone_with_fallback", side_effect=_no_fallback_clone),
    ):
        info = downloader.download_package(dep, tmp_path / "out")

    host_remotes = [u for u in _remote_urls(argvs) if urlparse(u).hostname == _HOST]
    assert host_remotes, "the cache must contact the declared remote"
    assert {urlparse(u).scheme for u in host_remotes} == {"ssh"}
    cache_clones = [
        argv
        for argv in argvs
        if "clone" in argv and any(urlparse(u).hostname == _HOST for u in _remote_urls([argv]))
    ]
    assert len(cache_clones) == 1
    assert ("--filter=blob:none" in cache_clones[0]) is (marker == "SKILL.md")
    assert (tmp_path / "out" / marker).is_file()

    locked = LockedDependency.from_dependency_ref(
        dep, info.resolved_reference.resolved_commit, 1, None
    )
    assert locked.resolved_commit == sha
    assert locked.host == _HOST
    assert locked.repo_url == "org/repo"
    assert info.package.source == dep.to_github_url()


@pytest.mark.parametrize(
    ("spec", "scheme"),
    [
        ("git@github.com:org/repo.git#main", "https"),
        ("https://gitlab.example.com/org/repo.git#main", "https"),
        ("http://gitlab.example.com/org/repo.git#main", "http"),
        ("gitlab.example.com/org/repo#main", "https"),
    ],
)
def test_other_transports_keep_canonical_cache_remote(spec: str, scheme: str) -> None:
    dep = DependencyReference.parse(spec)
    downloader = GitHubPackageDownloader()
    remote = downloader._persistent_cache_remote(dep, dep.to_github_url())
    assert remote == dep.to_github_url()
    assert urlparse(remote).scheme == scheme
