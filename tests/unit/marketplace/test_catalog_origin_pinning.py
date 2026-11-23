"""A marketplace catalog is read only from its registered origin's own refs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.utils.http_credentials import RedirectRefusedError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-catalog-origin")]

_REVIEW_REFS = (
    "refs/pull/7/head",
    "refs/pull/7/merge",
    "refs/merge-requests/7/head",
    "refs/remotes/origin/main",
)
_SOURCES = (
    "contoso/catalog",
    "https://git.example.com/contoso/catalog.git",
    "https://gitlab.com/contoso/catalog",
)
_URLS = (
    "https://github.com/contoso/catalog",
    "https://git.example.com/contoso/catalog.git",
    "https://gitlab.com/contoso/catalog",
)
_TIP = "a" * 40
_STRAY = "b" * 40


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".apm"
    home.mkdir(parents=True)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    registry_mod._invalidate_cache()
    yield home
    registry_mod._invalidate_cache()


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("ref", _REVIEW_REFS)
def test_a_review_ref_is_not_a_catalog_pin(source: str, ref: str) -> None:
    from apm_cli.marketplace.registration import parse_registration_source

    with pytest.raises(ValueError, match="branch or tag"):
        parse_registration_source(source, ref=ref)


@pytest.mark.parametrize("ref", ("", " ", "main..x", "refs/heads/", "-main"))
def test_a_malformed_pin_fails_closed(ref: str) -> None:
    from apm_cli.marketplace.registration import parse_registration_source

    with pytest.raises(ValueError):
        parse_registration_source("contoso/catalog", ref=ref)


def _source(url: str, ref: str) -> MarketplaceSource:
    return MarketplaceSource(name="acme", url=url, ref=ref, path="marketplace.json")


class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.history: list = []

    def close(self) -> None:
        pass


def _record_requests(monkeypatch, response: _Response) -> list[str]:
    """Stub the HTTP boundary; every catalog request is recorded."""
    urls: list[str] = []

    def request(_method, url=None, **_kwargs):
        urls.append(url)
        return response

    monkeypatch.setattr(client_mod._HTTP_SESSION, "request", request)
    return urls


def _list_refs(monkeypatch, listing) -> None:
    """Stub the origin's ``git ls-remote`` (rows of name, sha; an error = unreadable)."""

    def git_remote_refs(_url, *_patterns, **_kwargs):
        if isinstance(listing, Exception):
            raise listing
        out = "".join(f"{sha}\t{name}\n" for name, sha in listing)
        return subprocess.CompletedProcess(["git"], 0, stdout=out, stderr="")

    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", git_remote_refs)


class _AnonymousResolver:
    def try_with_fallback(self, _host, fn, **_kwargs):
        return fn(None, None)


_TIPS = (("refs/heads/main", _TIP), ("refs/tags/v1", _TIP), ("refs/pull/7/head", _STRAY))


@pytest.mark.parametrize("url", _URLS)
def test_a_commit_pin_outside_the_origin_refs_is_refused_before_any_fetch(
    url: str, monkeypatch
) -> None:
    _list_refs(monkeypatch, _TIPS)
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="branch or tag"):
        client_mod._fetch_file(
            _source(url, _STRAY), "marketplace.json", auth_resolver=_AnonymousResolver()
        )
    assert urls == []


def test_a_commit_pin_at_an_origin_tip_is_fetched(monkeypatch) -> None:
    _list_refs(monkeypatch, _TIPS)
    urls = _record_requests(monkeypatch, _Response(404))

    found = client_mod._fetch_file(
        _source("https://github.com/contoso/catalog", _TIP),
        "marketplace.json",
        auth_resolver=_AnonymousResolver(),
    )

    assert found is None
    assert [parse_qs(urlsplit(url).query)["ref"] for url in urls] == [[_TIP]]


@pytest.mark.parametrize("url", _URLS)
def test_an_unreadable_ref_listing_fails_closed(url: str, monkeypatch) -> None:
    _list_refs(monkeypatch, OSError("ls-remote failed"))
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="Cannot verify"):
        client_mod._fetch_file(
            _source(url, _STRAY), "marketplace.json", auth_resolver=_AnonymousResolver()
        )
    assert urls == []


@pytest.mark.parametrize("source", _SOURCES)
def test_a_declared_review_ref_never_passes_the_allow_list(source: str) -> None:
    from apm_cli.marketplace.user_manifest import DeclaredMarketplace
    from apm_cli.policy.policy_checks import MarketplaceOrigins, _check_marketplace_allow
    from apm_cli.policy.schema import MarketplacePolicy

    origins = MarketplaceOrigins(
        declared=(DeclaredMarketplace(name="acme", source=source, ref="refs/pull/7/head"),)
    )
    policy = MarketplacePolicy(
        allow=("github.com/contoso/*", "git.example.com/contoso/*", "gitlab.com/contoso/*")
    )

    assert _check_marketplace_allow(origins, policy).passed is False


def test_a_catalog_redirect_to_another_origin_is_refused(monkeypatch) -> None:
    urls = _record_requests(
        monkeypatch, _Response(302, "https://elsewhere.example/catalog/marketplace.json")
    )
    host_info = SimpleNamespace(api_base="https://api.github.com")

    with pytest.raises(MarketplaceFetchError) as excinfo:
        client_mod._fetch_github(
            _source("https://github.com/contoso/catalog", "main"),
            "marketplace.json",
            host_info=host_info,
            auth_resolver=_AnonymousResolver(),
        )
    assert [urlsplit(url).hostname for url in urls] == ["api.github.com"]
    # The refusal itself, not an unrelated response-handling failure.
    cause = excinfo.value
    while cause is not None and not isinstance(cause, RedirectRefusedError):
        cause = cause.__cause__ or cause.__context__
    assert isinstance(cause, RedirectRefusedError)


def test_replacing_an_alias_needs_confirmation(monkeypatch) -> None:
    from apm_cli.cli import cli

    registry_mod.add_marketplace(
        MarketplaceSource(name="acme", url="https://github.com/contoso/first", ref="main")
    )
    urls = _record_requests(monkeypatch, _Response(404))

    result = CliRunner().invoke(cli, ["marketplace", "add", "contoso/second", "--name", "acme"])

    assert result.exit_code == 1, result.output
    assert "acme" in result.output
    assert urls == []
    (kept,) = registry_mod.get_registered_marketplaces()
    assert kept.url == "https://github.com/contoso/first"


def test_install_refuses_a_declared_review_ref_before_any_catalog_request(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.cli import cli

    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\nversion: 1.0.0\n"
        "marketplaces:\n  - name: acme\n    source: contoso/catalog\n    ref: refs/pull/1/head\n"
        "dependencies:\n  apm: []\n",
        encoding="ascii",
    )
    before = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*")}
    urls = _record_requests(monkeypatch, _Response(404))
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 1, result.output
    assert "refs/pull/1/head" in result.output
    assert urls == []
    after = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*")}
    assert after == before


@pytest.mark.parametrize("url", _URLS)
def test_a_short_review_name_that_is_no_branch_or_tag_is_refused_before_any_fetch(
    url: str, monkeypatch
) -> None:
    _list_refs(monkeypatch, _TIPS)
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="branch or tag"):
        client_mod._fetch_file(
            _source(url, "pull/7/head"), "marketplace.json", auth_resolver=_AnonymousResolver()
        )
    assert urls == []


def test_a_real_branch_with_a_review_like_name_is_fetched(monkeypatch) -> None:
    _list_refs(monkeypatch, (*_TIPS, ("refs/heads/pull/7/head", _TIP)))
    urls = _record_requests(monkeypatch, _Response(404))

    client_mod._fetch_file(
        _source("https://github.com/contoso/catalog", "pull/7/head"),
        "marketplace.json",
        auth_resolver=_AnonymousResolver(),
    )

    # Fetched by the verified branch's commit, never by the review-like name.
    assert [parse_qs(urlsplit(url).query)["ref"] for url in urls] == [[_TIP]]


@pytest.mark.parametrize("url", _URLS)
def test_an_abbreviated_commit_pin_is_refused_unless_it_is_a_branch_or_tag(
    url: str, monkeypatch
) -> None:
    _list_refs(monkeypatch, _TIPS)
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="branch or tag"):
        client_mod._fetch_file(
            _source(url, _STRAY[:12]), "marketplace.json", auth_resolver=_AnonymousResolver()
        )
    assert urls == []


def test_a_branch_named_like_an_abbreviated_commit_is_fetched(monkeypatch) -> None:
    _list_refs(monkeypatch, (*_TIPS, ("refs/heads/cafe1234", _TIP)))
    urls = _record_requests(monkeypatch, _Response(404))

    client_mod._fetch_file(
        _source("https://github.com/contoso/catalog", "cafe1234"),
        "marketplace.json",
        auth_resolver=_AnonymousResolver(),
    )

    assert [parse_qs(urlsplit(url).query)["ref"] for url in urls] == [[_TIP]]


def test_registry_only_mode_refuses_a_pin_it_cannot_verify_without_contacting_the_origin(
    monkeypatch,
) -> None:
    listed: list[str] = []
    monkeypatch.setattr(
        "apm_cli.utils.git_env.git_remote_refs", lambda url, *_a, **_k: listed.append(url)
    )
    monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="PROXY_REGISTRY_ONLY"):
        client_mod._fetch_file(
            _source("https://github.com/contoso/catalog", _TIP),
            "marketplace.json",
            auth_resolver=_AnonymousResolver(),
        )
    assert listed == []
    assert urls == []


def test_an_ado_catalog_pin_is_listed_through_the_ado_auth_chain(monkeypatch) -> None:
    _list_refs(monkeypatch, _TIPS)
    chained: list[str] = []

    def try_with_fallback(_self, host, operation, **kwargs):
        chained.append(kwargs["path"])
        return operation(None, {})

    monkeypatch.setattr("apm_cli.core.auth.AuthResolver.try_with_fallback", try_with_fallback)
    source = _source("https://dev.azure.com/contoso/proj/_git/catalog", _STRAY)

    with pytest.raises(MarketplaceFetchError, match="branch or tag"):
        from apm_cli.marketplace.catalog_ref_pins import require_origin_pin

        require_origin_pin(source)
    assert chained == ["contoso/proj/_git/catalog"]


@pytest.mark.parametrize("tag_commit", [_STRAY, _TIP], ids=["different-commits", "same-commit"])
def test_a_short_name_that_is_both_a_branch_and_a_tag_is_refused(
    tag_commit: str, monkeypatch
) -> None:
    _list_refs(
        monkeypatch,
        (*_TIPS, ("refs/heads/pull/7/head", _TIP), ("refs/tags/pull/7/head", tag_commit)),
    )
    urls = _record_requests(monkeypatch, _Response(404))

    with pytest.raises(MarketplaceFetchError, match="both a branch and a tag"):
        client_mod._fetch_file(
            _source("https://github.com/contoso/catalog", "pull/7/head"),
            "marketplace.json",
            auth_resolver=_AnonymousResolver(),
        )
    assert urls == []
