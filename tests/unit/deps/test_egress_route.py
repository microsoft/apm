"""egress-route-policy owner: the route every outbound surface takes (#4121)."""

from __future__ import annotations

import pytest

from apm_cli.deps.egress_route import (
    EgressRoute,
    EgressSurface,
    is_direct_exception,
    is_proxy_only,
    is_refused,
    route_for,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-egress-route-policy")]

REGISTRY_ONLY = {
    EgressSurface.SOURCE_ARCHIVE: EgressRoute.PROXY,
    EgressSurface.SOURCE_PROBE: EgressRoute.REFUSED,
    EgressSurface.SOURCE_ENUMERATION: EgressRoute.REFUSED,
    EgressSurface.MCP_REGISTRY: EgressRoute.REFUSED,
    EgressSurface.MARKETPLACE_FILE: EgressRoute.PROXY,
    EgressSurface.POLICY_FETCH: EgressRoute.DIRECT,
    EgressSurface.VERSION_CHECK: EgressRoute.REFUSED,
    EgressSurface.SELF_UPDATE: EgressRoute.DIRECT,
}


@pytest.fixture
def registry_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTIFACTORY_ONLY", raising=False)
    monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")


@pytest.fixture
def open_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_REGISTRY_ONLY", raising=False)
    monkeypatch.delenv("ARTIFACTORY_ONLY", raising=False)


def test_registry_only_table_covers_every_surface() -> None:
    assert set(REGISTRY_ONLY) == set(EgressSurface)


@pytest.mark.usefixtures("registry_only")
@pytest.mark.parametrize(("surface", "route"), sorted(REGISTRY_ONLY.items(), key=str))
def test_registry_only_routes_each_surface(surface: EgressSurface, route: EgressRoute) -> None:
    assert route_for(surface) is route
    assert is_refused(surface) is (route is EgressRoute.REFUSED)
    assert is_proxy_only(surface) is (route is EgressRoute.PROXY)
    assert is_direct_exception(surface) is (route is EgressRoute.DIRECT)


@pytest.mark.usefixtures("registry_only")
@pytest.mark.parametrize("surface", list(EgressSurface))
def test_operator_mirror_goes_direct_in_registry_only_mode(surface: EgressSurface) -> None:
    assert route_for(surface, operator_mirror=True) is EgressRoute.DIRECT
    assert is_refused(surface, operator_mirror=True) is False
    assert is_refused(surface, operator_mirror=False) is (
        REGISTRY_ONLY[surface] is EgressRoute.REFUSED
    )


@pytest.mark.usefixtures("open_egress")
@pytest.mark.parametrize("surface", list(EgressSurface))
def test_every_surface_is_direct_outside_registry_only_mode(surface: EgressSurface) -> None:
    assert route_for(surface) is EgressRoute.DIRECT
    assert route_for(surface, operator_mirror=True) is EgressRoute.DIRECT
    assert is_refused(surface) is False
    assert is_proxy_only(surface) is False
    assert is_direct_exception(surface) is False


@pytest.mark.parametrize("flag", ["PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY"])
@pytest.mark.parametrize(("value", "active"), [("1", True), ("0", False), ("", False)])
def test_registry_only_flag_decides_the_mode(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str, active: bool
) -> None:
    monkeypatch.delenv("PROXY_REGISTRY_ONLY", raising=False)
    monkeypatch.delenv("ARTIFACTORY_ONLY", raising=False)
    monkeypatch.setenv(flag, value)
    expected = EgressRoute.REFUSED if active else EgressRoute.DIRECT
    assert route_for(EgressSurface.SOURCE_PROBE) is expected
