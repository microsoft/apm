"""Hosted-catalog credentials stay inside their bound query scope (#3300)."""

from __future__ import annotations

import pytest

from apm_cli.utils.http_credentials import origin_bound_refusal, origin_bounded_request
from tests.unit.utils.test_http_credentials import _BASE, _ScriptedResponse, _ScriptedSession

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-catalog-credentials")]


_CATALOG_SCOPE = "https://catalog.example.com/team/?catalog=a"
_CATALOG_URL = "https://catalog.example.com/team/marketplace.json?catalog=a"


@pytest.mark.parametrize(
    ("location", "keeps_credential"),
    [
        ("/team/v2/marketplace.json?catalog=a", True),
        ("/team/marketplace.json?catalog=b", False),
        ("/team/marketplace.json", False),
        ("/other/marketplace.json?catalog=a", False),
        ("https://cdn.example.net/team/marketplace.json?catalog=a", False),
    ],
)
def test_query_bound_hop_keeps_credentials_only_inside_scope(
    location: str, keeps_credential: bool
) -> None:
    session = _ScriptedSession(_ScriptedResponse(302, location), _ScriptedResponse(200))

    response = origin_bounded_request(
        session,
        "GET",
        _CATALOG_URL,
        base_url=_CATALOG_SCOPE,
        headers={"Authorization": "Bearer t"},
        follow_cross_origin_without_credentials=True,
        bind_query=True,
    )

    assert response.status_code == 200
    assert ("Authorization" in session.calls[1]["headers"]) is keeps_credential
    assert [hop.status_code for hop in response.history] == [302]


def test_credentials_stay_dropped_after_returning_into_scope() -> None:
    session = _ScriptedSession(
        _ScriptedResponse(302, "https://sso.example.net/login"),
        _ScriptedResponse(302, _CATALOG_URL),
        _ScriptedResponse(200),
    )

    origin_bounded_request(
        session,
        "GET",
        _CATALOG_URL,
        base_url=_CATALOG_SCOPE,
        headers={"Authorization": "Bearer t"},
        follow_cross_origin_without_credentials=True,
        bind_query=True,
    )

    assert [("Authorization" in call["headers"]) for call in session.calls] == [True, False, False]


def test_query_binding_is_off_unless_requested() -> None:
    assert origin_bound_refusal(f"{_BASE}/x?a=1", base_url=f"{_BASE}/?a=2") is None
    assert (
        origin_bound_refusal(f"{_BASE}/x?a=1", base_url=f"{_BASE}/?a=2", bind_query=True)
        == "outside-query"
    )
    assert (
        origin_bound_refusal(f"{_BASE}/x?b=2&a=1", base_url=f"{_BASE}/?a=1&b=2", bind_query=True)
        is None
    )
