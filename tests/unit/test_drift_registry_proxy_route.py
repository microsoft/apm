"""Proxy-routed lock rows keep their manifest identity (#3657).

A transparent registry proxy records the proxy host and ``registry_prefix``
instead of the upstream host. Drift asks the proxy routing owner
(``ArtifactoryRouter``) whether the manifest dep still lands on that same
proxy route; only then is the provider difference not drift.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.artifactory_orchestrator import ArtifactoryRouter
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.drift import build_download_ref, detect_ref_change
from apm_cli.models.dependency.reference import DependencyReference

_PROXY_HOST = "registry-proxy.example.invalid"
_PROXY_URL = f"https://{_PROXY_HOST}/artifactory/github"


def _locked(**overrides) -> LockedDependency:
    fields = {
        "repo_url": "acme/pkg",
        "host": _PROXY_HOST,
        "registry_prefix": "artifactory/github",
        "resolved_ref": "1.0.0",
        "version": "1.0.0",
    }
    fields.update(overrides)
    return LockedDependency(**fields)


class _Lock:
    def __init__(self, locked: LockedDependency) -> None:
        self._locked = locked

    def get_dependency(self, key):
        return self._locked if key == self._locked.get_unique_key() else None


@pytest.fixture
def registry_only(monkeypatch):
    for name in ("ARTIFACTORY_BASE_URL", "ARTIFACTORY_ONLY", "PROXY_REGISTRY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROXY_REGISTRY_URL", _PROXY_URL)
    monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")


def test_same_proxy_route_is_not_drift(registry_only) -> None:
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    assert ArtifactoryRouter.routes_to_locked_proxy(dep, _locked())
    assert detect_ref_change(dep, _locked()) is False


def test_other_proxy_route_is_drift(registry_only, monkeypatch) -> None:
    monkeypatch.setenv(
        "PROXY_REGISTRY_URL", "https://other-proxy.example.invalid/artifactory/github"
    )
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    assert not ArtifactoryRouter.routes_to_locked_proxy(dep, _locked())
    assert detect_ref_change(dep, _locked()) is True


def test_other_prefix_is_drift(registry_only) -> None:
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    assert detect_ref_change(dep, _locked(registry_prefix="artifactory/other")) is True


def test_no_proxy_configured_is_drift(monkeypatch) -> None:
    for name in ("PROXY_REGISTRY_URL", "ARTIFACTORY_BASE_URL", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    assert not ArtifactoryRouter.routes_to_locked_proxy(dep, _locked())
    assert detect_ref_change(dep, _locked()) is True


def test_changed_ref_on_same_route_is_drift(registry_only) -> None:
    dep = DependencyReference.parse("acme/pkg#1.1.0")
    assert detect_ref_change(dep, _locked()) is True


def test_direct_row_is_never_a_proxy_route(registry_only) -> None:
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    direct = _locked(host="github.com", registry_prefix=None)
    assert not ArtifactoryRouter.routes_to_locked_proxy(dep, direct)


def test_semver_constraint_still_compared_when_recorded(registry_only) -> None:
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    assert detect_ref_change(dep, _locked(constraint="^1.0.0")) is True


def test_replay_keeps_current_route_on_same_proxy(registry_only) -> None:
    """Replay does not pin the lock's host when the route already lands there."""
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    replay = build_download_ref(dep, _Lock(_locked()), update_refs=False, ref_changed=False)
    assert replay.artifactory_prefix is None
    assert ArtifactoryRouter.routing_target(replay)[2] == "https"
    assert ArtifactoryRouter.routing_target(replay)[3] is False


def test_replay_pins_lock_proxy_without_current_route(monkeypatch) -> None:
    for name in ("PROXY_REGISTRY_URL", "ARTIFACTORY_BASE_URL", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    dep = DependencyReference.parse("acme/pkg#1.0.0")
    replay = build_download_ref(dep, _Lock(_locked()), update_refs=False, ref_changed=False)
    assert (replay.host, replay.artifactory_prefix) == (_PROXY_HOST, "artifactory/github")
