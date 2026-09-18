from types import SimpleNamespace

from apm_cli.marketplace.output_mappers import CopilotMarketplaceMapper
from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS
from apm_cli.marketplace.yml_schema import MarketplaceConfig, MarketplaceOwner, PackageEntry


def _resolved(**overrides):
    values = {
        "name": "demo",
        "source_repo": "acme/demo",
        "source_url": None,
        "host": None,
        "subdir": None,
        "ref": None,
        "sha": None,
        "tags": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(entry: PackageEntry) -> MarketplaceConfig:
    return MarketplaceConfig(
        name="my-marketplace",
        description="Curated plugins",
        version="1.0.0",
        owner=MarketplaceOwner(name="Acme", email="plugins@acme.test"),
        outputs=("copilot",),
        packages=(entry,),
    )


def test_copilot_profile_uses_default_discovery_path():
    profile = MARKETPLACE_OUTPUTS["copilot"]

    assert profile.default_output == ".github/plugin/marketplace.json"
    assert profile.mapper == "copilot"


def test_copilot_mapper_nests_marketplace_metadata_and_keeps_local_source():
    entry = PackageEntry(
        name="demo",
        source="./plugins/demo",
        version="2.1.0",
        description="Demo plugin",
        is_local=True,
    )

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(_resolved(),),
    )

    assert result.document["metadata"] == {
        "description": "Curated plugins",
        "version": "1.0.0",
    }
    assert result.document["plugins"] == [
        {
            "name": "demo",
            "description": "Demo plugin",
            "version": "2.1.0",
            "source": "./plugins/demo",
        }
    ]


def test_copilot_mapper_preserves_remote_pin_information():
    entry = PackageEntry(
        name="demo",
        source="acme/demo",
        ref="main",
        description="Demo plugin",
    )

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(
            _resolved(
                ref="v2.1.0",
                sha="0123456789abcdef0123456789abcdef01234567",
                subdir="plugins/demo",
            ),
        ),
        remote_metadata={"demo": {"version": "2.1.0"}},
    )

    plugin = result.document["plugins"][0]
    assert plugin["source"] == {
        "source": "github",
        "repo": "acme/demo",
        "ref": "v2.1.0",
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "path": "plugins/demo",
    }
