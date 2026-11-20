"""Hosted marketplace catalog credential resolution (#3300)."""

from __future__ import annotations

import base64

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.catalog_credentials import (
    CatalogCredentialError,
    catalog_scope_url,
    resolve_catalog_credential,
)

_URL = "https://catalog.example.com/team/marketplace.json?catalog=a"


def _resolve(environ: dict[str, str], *, name: str = "corp-main", peers: tuple[str, ...] = ()):
    return resolve_catalog_credential(
        marketplace=name, binding_url=_URL, peer_names=peers, environ=environ
    )


def test_bearer_token_wins_over_basic_pair() -> None:
    credential = _resolve(
        {
            "APM_MARKETPLACE_TOKEN_CORP_MAIN": "tok-123",
            "APM_MARKETPLACE_USER_CORP_MAIN": "alice",
            "APM_MARKETPLACE_PASS_CORP_MAIN": "pw",
        }
    )

    assert credential.headers() == {"Authorization": "Bearer tok-123"}
    assert credential.source_variable == "APM_MARKETPLACE_TOKEN_CORP_MAIN"


def test_basic_pair_is_encoded() -> None:
    credential = _resolve(
        {"APM_MARKETPLACE_USER_CORP_MAIN": "alice", "APM_MARKETPLACE_PASS_CORP_MAIN": "p:w"}
    )

    encoded = base64.b64encode(b"alice:p:w").decode()
    assert credential.headers() == {"Authorization": f"Basic {encoded}"}
    assert credential.scheme == "basic"


def test_no_variables_is_anonymous_and_ignores_other_credentials() -> None:
    credential = _resolve(
        {
            "GITHUB_APM_PAT": "ghp_x",
            "APM_REGISTRY_TOKEN_CORP_MAIN": "registry-token",
            "APM_MARKETPLACE_TOKEN_OTHER": "other",
        }
    )

    assert not credential.presented
    assert credential.headers() == {}


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({"APM_MARKETPLACE_TOKEN_CORP_MAIN": "tok en-secret"}, "APM_MARKETPLACE_TOKEN_CORP_MAIN"),
        ({"APM_MARKETPLACE_TOKEN_CORP_MAIN": "tok-secret\n"}, "APM_MARKETPLACE_TOKEN_CORP_MAIN"),
        (
            {"APM_MARKETPLACE_USER_CORP_MAIN": "a:b", "APM_MARKETPLACE_PASS_CORP_MAIN": "secret"},
            "APM_MARKETPLACE_USER_CORP_MAIN",
        ),
        (
            {"APM_MARKETPLACE_USER_CORP_MAIN": "a", "APM_MARKETPLACE_PASS_CORP_MAIN": "se\ncret"},
            "APM_MARKETPLACE_PASS_CORP_MAIN",
        ),
    ],
)
def test_malformed_values_fail_without_echoing_them(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(CatalogCredentialError) as excinfo:
        _resolve(environ)

    message = str(excinfo.value)
    assert variable in message
    assert "secret" not in message
    assert excinfo.value.__cause__ is None


def test_incomplete_basic_pair_names_the_missing_variable() -> None:
    with pytest.raises(CatalogCredentialError, match="APM_MARKETPLACE_PASS_CORP_MAIN is not"):
        _resolve({"APM_MARKETPLACE_USER_CORP_MAIN": "alice"})


def test_ambiguous_normalized_names_are_rejected() -> None:
    with pytest.raises(CatalogCredentialError, match=r"'corp-main' and 'corp\.main'"):
        _resolve({"APM_MARKETPLACE_TOKEN_CORP_MAIN": "t"}, peers=("corp.main",))


def test_same_name_in_another_case_is_not_ambiguous() -> None:
    assert _resolve({"APM_MARKETPLACE_TOKEN_CORP_MAIN": "t"}, peers=("Corp-Main",)).presented


def test_repr_never_contains_the_secret() -> None:
    credential = _resolve({"APM_MARKETPLACE_TOKEN_CORP_MAIN": "tok-secret"})

    assert "tok-secret" not in repr(credential)


def test_scope_is_the_catalog_directory_and_query() -> None:
    assert catalog_scope_url(_URL) == "https://catalog.example.com/team/?catalog=a"
    assert catalog_scope_url("https://h:8443/marketplace.json") == "https://h:8443/"


def test_auth_resolver_entry_point_reads_only_marketplace_variables(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_APM_PAT", "ghp_x")
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP_MAIN", "tok")

    credential = AuthResolver.resolve_marketplace_catalog_credential(
        marketplace="corp-main", binding_url=_URL, peer_names=()
    )

    assert credential.headers() == {"Authorization": "Bearer tok"}


def test_auth_resolver_entry_point_requires_every_argument() -> None:
    with pytest.raises(TypeError):
        AuthResolver.resolve_marketplace_catalog_credential(marketplace="corp", binding_url=_URL)
