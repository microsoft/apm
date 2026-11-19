"""Lock-vs-alias catalog origin checks (#3912)."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.marketplace import catalog_origin
from apm_cli.marketplace.errors import MarketplaceOriginConflictError
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.source_identity import same_marketplace_origin

_A = "https://git.example.com/team/catalog-a"
_B = "https://git.example.com/team/catalog-b"


def _lock(*origins: str | None, alias: str = "company") -> LockFile:
    lockfile = LockFile()
    for index, origin in enumerate(origins):
        lockfile.add_dependency(
            LockedDependency(
                repo_url=f"team/pkg-{index}",
                resolved_commit="a" * 40,
                discovered_via=alias,
                marketplace_plugin_name="plugin",
                source_url=origin,
            )
        )
    return lockfile


@pytest.fixture
def registered(monkeypatch):
    def _register(url: str) -> None:
        monkeypatch.setattr(
            catalog_origin,
            "get_marketplace_by_name",
            lambda name: MarketplaceSource(name=name, url=url),
        )

    return _register


def _register_path(monkeypatch, url: str, path: str) -> None:
    monkeypatch.setattr(
        catalog_origin,
        "get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, url=url, path=path),
    )


def test_ambiguous_association_still_rejects_a_rebound_alias(registered) -> None:
    registered(_B)
    with pytest.raises(MarketplaceOriginConflictError) as exc:
        catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A, _A))
    assert _A in str(exc.value)
    assert _B in str(exc.value)


def test_matching_and_legacy_rows_are_accepted(registered) -> None:
    registered(f"{_A}.git")
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A, None))
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(None))


def test_hosted_catalog_query_selects_a_distinct_origin() -> None:
    hosted = "https://catalog.example.com/marketplace.json"
    assert same_marketplace_origin(f"{hosted}?tenant=a", f"{hosted}?tenant=a")
    assert not same_marketplace_origin(f"{hosted}?tenant=a", f"{hosted}?tenant=b")
    assert not same_marketplace_origin(f"{hosted}?tenant=a", hosted)


def test_recased_alias_cannot_skip_the_check(registered) -> None:
    registered(_B)
    with pytest.raises(MarketplaceOriginConflictError):
        catalog_origin.require_locked_catalog_origin("Company", "Plugin", _lock(_A))


def test_catalog_path_move_in_the_same_repo_is_a_known_limit(monkeypatch) -> None:
    """The lock records only the origin, so a path move is not detected (0.38)."""
    _register_path(monkeypatch, _A, "catalogs/other/marketplace.json")
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A))


def test_conflict_remediation_quotes_the_locked_origin() -> None:
    message = str(MarketplaceOriginConflictError("company", "plugin", _A, _B))
    assert f"apm marketplace add {_A} --name company" in message
    assert "remove 'plugin' from apm.yml" in message
    unsafe = str(MarketplaceOriginConflictError("company", "plugin", "https://x/$(rm)", _B))
    assert "apm marketplace add" in unsafe
    assert "run: apm marketplace add" not in unsafe or "'https://x/$(rm)'" in unsafe
