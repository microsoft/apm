"""Legacy ``marketplaces.json`` migration test.

Loads a fixture with the pre-PR shape (``{owner, repo, host?, branch?}``),
round-trips through ``_load`` + ``_save``, and asserts the resulting JSON
file has the new URL-first shape while still emitting legacy mirror fields
for one release of downgrade safety.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.marketplace import registry
from apm_cli.marketplace.models import MarketplaceSource

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_marketplaces.json"


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the registry at a temp directory with a pre-seeded legacy fixture."""
    config_dir = tmp_path / "apm-config"
    config_dir.mkdir()
    shutil.copy(FIXTURE, config_dir / "marketplaces.json")

    monkeypatch.setattr(registry, "_registry_cache", None)
    with (
        patch("apm_cli.config.CONFIG_DIR", str(config_dir)),
        patch("apm_cli.config.ensure_config_exists", lambda: None),
    ):
        yield config_dir


def test_legacy_fixture_loads_and_upgrades_to_url_shape(isolated_config: Path) -> None:
    sources = registry._load()
    assert len(sources) == 3

    by_name = {s.name: s for s in sources}

    acme = by_name["acme-default"]
    assert acme.url == "https://github.com/acme/marketplace"
    assert acme.ref == "main"
    assert acme.kind == "github"

    ghes = by_name["ghes-mkt"]
    assert ghes.url == "https://ghe.contoso.com/team/internal-marketplace"
    assert ghes.ref == "release"

    gitlab = by_name["gitlab-mkt"]
    assert gitlab.url == "https://gitlab.com/group/sub/tools"
    assert gitlab.ref == "develop"
    assert gitlab.path == "custom/marketplace.json"


def test_legacy_round_trip_emits_url_and_legacy_mirror(isolated_config: Path) -> None:
    sources = registry._load()
    registry._save(sources)

    path = isolated_config / "marketplaces.json"
    raw = json.loads(path.read_text())

    for entry in raw["marketplaces"]:
        # The new canonical field is present
        assert "url" in entry, f"entry {entry['name']!r} missing url after save"
        # Legacy mirror fields preserved for one release
        assert "owner" in entry
        assert "repo" in entry


def test_branch_field_accepted_as_ref_alias() -> None:
    src = MarketplaceSource.from_dict(
        {"name": "alias-mkt", "owner": "a", "repo": "b", "branch": "feature/x"}
    )
    assert src.ref == "feature/x"
