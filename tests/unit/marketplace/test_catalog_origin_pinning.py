"""A marketplace catalog is read only from its registered origin's own refs."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-catalog-origin")]

_REVIEW_REFS = ("refs/pull/7/head", "refs/pull/7/merge", "refs/merge-requests/7/head")
_SOURCES = ("contoso/catalog", "https://git.example.com/contoso/catalog.git")
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


def _stub_fetchers(monkeypatch) -> list[str]:
    fetched: list[str] = []

    def fetch(source, file_path, **_kwargs):
        fetched.append(source.ref)
        return {"name": "acme", "plugins": []}

    monkeypatch.setattr(client_mod, "_FETCHERS", {kind: fetch for kind in ("github", "git")})
    return fetched


@pytest.mark.parametrize(
    "url", ("https://github.com/contoso/catalog", "https://git.example.com/contoso/catalog.git")
)
def test_a_commit_pin_outside_the_origin_refs_is_refused_before_any_fetch(
    url: str, monkeypatch
) -> None:
    from apm_cli.marketplace import catalog_ref_pins

    monkeypatch.setattr(
        catalog_ref_pins,
        "origin_ref_tips",
        lambda _source: {"refs/heads/main": _TIP, "refs/tags/v1": _TIP},
    )
    fetched = _stub_fetchers(monkeypatch)

    with pytest.raises(MarketplaceFetchError, match="branch or tag"):
        client_mod._fetch_file(_source(url, _STRAY), "marketplace.json", auth_resolver=object())
    assert fetched == []

    assert client_mod._fetch_file(_source(url, _TIP), "marketplace.json", auth_resolver=object())
    assert fetched == [_TIP]


def test_an_unreadable_ref_listing_fails_closed(monkeypatch) -> None:
    from apm_cli.marketplace import catalog_ref_pins

    def unreadable(_source):
        raise OSError("ls-remote failed")

    monkeypatch.setattr(catalog_ref_pins, "origin_ref_tips", unreadable)
    fetched = _stub_fetchers(monkeypatch)

    with pytest.raises(MarketplaceFetchError):
        client_mod._fetch_file(
            _source("https://github.com/contoso/catalog", _STRAY),
            "marketplace.json",
            auth_resolver=object(),
        )
    assert fetched == []


@pytest.mark.parametrize("source", _SOURCES)
def test_a_declared_review_ref_never_passes_the_allow_list(source: str) -> None:
    from apm_cli.marketplace.user_manifest import DeclaredMarketplace
    from apm_cli.policy.policy_checks import MarketplaceOrigins, _check_marketplace_allow
    from apm_cli.policy.schema import MarketplacePolicy

    origins = MarketplaceOrigins(
        declared=(DeclaredMarketplace(name="acme", source=source, ref="refs/pull/7/head"),)
    )
    policy = MarketplacePolicy(allow=("github.com/contoso/*", "git.example.com/contoso/*"))

    assert _check_marketplace_allow(origins, policy).passed is False


class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.history: list = []

    def close(self) -> None:
        pass


class _RedirectingSession:
    def __init__(self) -> None:
        self.cookies = None
        self.urls: list[str] = []

    def request(self, method, url, **_kwargs):
        self.urls.append(url)
        return _Response(302, "https://elsewhere.example/catalog/marketplace.json")


class _AnonymousResolver:
    def try_with_fallback(self, _host, fn, **_kwargs):
        return fn(None, None)


def test_a_catalog_redirect_to_another_origin_is_refused(monkeypatch) -> None:
    session = _RedirectingSession()
    monkeypatch.setattr(client_mod, "_HTTP_SESSION", session)
    host_info = type("HostInfo", (), {"api_base": "https://api.github.com"})()

    with pytest.raises(MarketplaceFetchError):
        client_mod._fetch_github(
            _source("https://github.com/contoso/catalog", "main"),
            "marketplace.json",
            host_info=host_info,
            auth_resolver=_AnonymousResolver(),
        )
    assert len(session.urls) == 1
    assert not any("elsewhere.example" in url for url in session.urls)


def test_replacing_an_alias_needs_confirmation(monkeypatch) -> None:
    from apm_cli.cli import cli

    registry_mod.add_marketplace(
        MarketplaceSource(name="acme", url="https://github.com/contoso/first", ref="main")
    )
    fetched: list[str] = []

    def no_fetch(source, *_args, **_kwargs):
        fetched.append(source.url)
        raise AssertionError("no catalog request before the replacement is confirmed")

    monkeypatch.setattr(client_mod, "_fetch_file", no_fetch)

    result = CliRunner().invoke(cli, ["marketplace", "add", "contoso/second", "--name", "acme"])

    assert result.exit_code == 1, result.output
    assert "acme" in result.output
    assert fetched == []
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
    fetched: list[str] = []

    def no_fetch(source, *_args, **_kwargs):
        fetched.append(source.url)
        return {"name": "acme", "plugins": []}

    monkeypatch.setattr(client_mod, "_fetch_file", no_fetch)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 1, result.output
    assert "refs/pull/1/head" in result.output
    assert fetched == []
    after = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*")}
    assert after == before
