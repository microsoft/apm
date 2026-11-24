"""Which origins the in-process fetch serves, and what each requires (W3d EH1-EH10, P1-P5).

github.com and GHE Cloud run GitHub's server stack; GHES and GitLab packs
also pass git's own collision-detecting ``index-pack``. Every other host, a
URL git would not fetch verbatim, a proxy entry libcurl might read
differently, or a foreign credential stays with git.
"""

from __future__ import annotations

import base64
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest

from apm_cli.cache import inprocess_fetch as fetch
from apm_cli.cache import inprocess_origins as origins
from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.deps import anonymous_ref_listing as arl

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

Entry = namedtuple("Entry", "key value")


@pytest.mark.parametrize(
    ("url", "kind", "upload"),
    [
        ("https://github.com/o/r", "github", "https://github.com/o/r/git-upload-pack"),
        (
            "https://lifecycle.ghe.com/o/r.git",
            "ghe_cloud",
            "https://lifecycle.ghe.com/o/r.git/git-upload-pack",
        ),
        (
            "https://gitlab.com/o/group/r.git",
            "gitlab",
            "https://gitlab.com/o/group/r.git/git-upload-pack",
        ),
    ],
)
def test_classified_origins_fetch_the_verbatim_url(url: str, kind: str, upload: str) -> None:
    origin = origins.origin_of(url)

    assert (origin.kind, origin.upload_url) == (kind, upload)


def test_a_configured_ghes_host_is_an_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example")

    origin = origins.origin_of("https://ghes.corp.example/o/r.git")

    assert (origin.kind, origin.checks_collisions) == ("ghes", True)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/o/r.git",  # unclassified host
        "https://dev.azure.com/o/p/_git/r",  # ADO
        "https://lifecycle.ghe.com:8443/o/r.git",  # port
        "https://user@lifecycle.ghe.com/o/r.git",  # userinfo
        "https://lifecycle.ghe.com/o/r.git?x=1",  # query
        "https://lifecycle.ghe.com/o/g/r.git",  # nested path off GitLab
        "https://lifecycle.ghe.com/o/../r.git",
        "https://LIFECYCLE.ghe.com/o/r.git",  # git would not send this host verbatim
        "http://lifecycle.ghe.com/o/r.git",
    ],
)
def test_any_other_url_is_gits(url: str) -> None:
    with pytest.raises(origins.OriginRefused):
        origins.origin_of(url)


@pytest.mark.parametrize(
    ("no_proxy", "bypasses"),
    [(".corp.example", True), ("corp.example", True), ("example.org", False), ("*", True)],
)
def test_no_proxy_matches_the_enterprise_host_as_curl_does(no_proxy: str, bypasses: bool) -> None:
    assert arl._bypasses_proxy(no_proxy, "ghes.corp.example") is bypasses


@pytest.mark.parametrize("entry", ["corp.example:443", "10.0.0.0/8"])
def test_a_port_or_cidr_no_proxy_entry_is_gits_for_an_enterprise_host(entry: str) -> None:
    with pytest.raises(arl._Fallback, match="port or CIDR"):
        arl._bypasses_proxy(entry, "ghes.corp.example")


def _managed(user: str) -> dict[str, str]:
    value = base64.b64encode(f"{user}:placeholder".encode()).decode()
    return {
        "APM_GIT_MANAGED_AUTH_INTENT": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {value}",
    }


def test_gitlab_takes_only_its_oauth2_basic_header() -> None:
    gitlab = origins.origin_of("https://gitlab.com/o/group/r.git").managed_user

    header, env = fetch._split_managed_auth(_managed("oauth2"), gitlab)

    assert header is not None and header.startswith("Authorization: Basic ")
    assert "GIT_CONFIG_VALUE_0" not in env
    with pytest.raises(fetch._Refuse, match="not github-basic"):
        fetch._split_managed_auth(_managed("x-access-token"), gitlab)


def test_a_breaker_trips_only_its_own_origin() -> None:
    with invocation_scope():
        assert fetch.trip_if_rate_limited(429, {}, "gitlab.com")

        assert fetch._rate_limited("gitlab.com")
        assert not fetch._rate_limited("github.com")
        assert not fetch._rate_limited("lifecycle.ghe.com")


def test_a_proxy_asking_for_credentials_counts_in_the_breaker() -> None:
    with invocation_scope():
        assert fetch.trip_if_rate_limited(407, {}, "lifecycle.ghe.com")
        assert fetch._rate_limited("lifecycle.ghe.com")


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, check=True
    ).stdout


def _shallow_pack(tmp_path: Path) -> bytes:
    """A depth-1 pack, as ``deepen 1`` answers: the tip commit's objects, its parent absent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for n in (1, 2):
        (repo / "f.txt").write_text(f"v{n}\n", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", f"c{n}")
    return _git(repo, "pack-objects", "--stdout", "--revs", stdin=b"HEAD\n^HEAD~1\n")


def _ghes(monkeypatch: pytest.MonkeyPatch) -> origins.Origin:
    monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example")
    return origins.origin_of("https://ghes.corp.example/o/r.git")


def test_a_depth_one_pack_passes_gits_collision_check(monkeypatch, tmp_path: Path) -> None:
    """EH1 engagement: a shallow pack whose parent is absent still indexes (no --strict)."""
    origins.require_collision_checked(_ghes(monkeypatch), _shallow_pack(tmp_path), {}, [])


def test_a_corrupt_pack_is_gits(monkeypatch, tmp_path: Path) -> None:
    origin = _ghes(monkeypatch)
    pack = _shallow_pack(tmp_path)
    corrupt = pack[:20] + bytes([pack[20] ^ 0xFF]) + pack[21:]

    with pytest.raises(origins.OriginRefused, match="index-pack"):
        origins.require_collision_checked(origin, corrupt, {}, [])


def test_github_and_ghe_cloud_spawn_no_index_pack(monkeypatch) -> None:
    def no_spawn(*_args, **_kwargs):
        raise AssertionError("no index-pack for GitHub's own server stack")

    monkeypatch.setattr(origins.subprocess, "run", no_spawn)
    for url in ("https://github.com/o/r", "https://lifecycle.ghe.com/o/r.git"):
        origins.require_collision_checked(origins.origin_of(url), b"x" * 64, {}, [])


@pytest.mark.parametrize(
    ("entries", "fsck"),
    [
        ([], False),
        ([Entry("transfer.fsckObjects", "true")], True),
        ([Entry("transfer.fsckObjects", "true"), Entry("fetch.fsckObjects", "false")], False),
        ([Entry("fetch.fsckObjects", "")], True),
    ],
)
def test_fsck_follows_gits_fetch_then_transfer_setting(entries, fsck: bool) -> None:
    assert origins._fsck_requested(entries) is fsck
