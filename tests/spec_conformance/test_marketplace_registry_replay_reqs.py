"""Registry-routed marketplace replay -- sec.5.2 (req-lk-003, req-lk-019).

A catalog entry routed to a package registry locks a registry-sourced entry:
`resolved_url` and `resolved_hash` instead of `resolved_commit`
(req-lk-003), with the registry-resolved `version` as the exact reinstall
selection and `resolved_hash` as the integrity anchor (req-lk-019). A
marketplace declaration replays such an entry on that evidence alone, and
refuses, naming the reason, when it is incomplete or no longer selected.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.drift import marketplace_ref_mismatch
from apm_cli.models.dependency import DependencyReference

pytestmark = [pytest.mark.unit, pytest.mark.promise("req-lk-003", "req-lk-019")]


def _declaration(version: str | None = None) -> DependencyReference:
    entry: dict[str, object] = {"name": "widget", "marketplace": "corp"}
    if version is not None:
        entry["version"] = version
    return DependencyReference.parse_from_dict(entry)


def _registry_row(**changes: object) -> LockedDependency:
    return LockedDependency(
        **{
            "repo_url": "acme/widget",
            "discovered_via": "corp",
            "marketplace_plugin_name": "widget",
            "source": "registry",
            "resolved_ref": "1.0.0",
            "version": "1.0.0",
            "resolved_url": "https://registry.example/apm/v1/packages/acme/widget/versions/1.0.0/download",
            "resolved_hash": "sha256:" + "d" * 64,
            **changes,
        }
    )


@pytest.mark.req("req-lk-003")
def test_registry_marketplace_row_replays_on_url_and_hash_without_a_commit():
    row = _registry_row()
    assert row.resolved_commit is None
    assert marketplace_ref_mismatch(_declaration(), row) is None


@pytest.mark.req("req-lk-003")
@pytest.mark.parametrize("missing", ["resolved_url", "resolved_hash"])
def test_registry_marketplace_row_without_url_or_hash_is_not_replayed(missing: str):
    reason = marketplace_ref_mismatch(_declaration(), _registry_row(**{missing: None}))
    assert reason == "missing locked registry version, URL or hash evidence"


@pytest.mark.req("req-lk-019")
@pytest.mark.parametrize("selector", ["^1.0.0", "~1.0.0", "1.0.0"])
def test_registry_version_is_the_replayed_selection(selector: str):
    assert marketplace_ref_mismatch(_declaration(selector), _registry_row()) is None


@pytest.mark.req("req-lk-019")
def test_registry_marketplace_row_refuses_a_selector_that_excludes_its_version():
    reason = marketplace_ref_mismatch(_declaration("^2.0.0"), _registry_row())
    assert reason == "manifest version '^2.0.0' != lockfile registry version '1.0.0'"
    assert (
        marketplace_ref_mismatch(_declaration(), _registry_row(version=None))
        == "missing locked registry version, URL or hash evidence"
    )
