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


def test_copilot_mapper_emits_relative_path_string_for_remote_subdir():
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
    assert plugin["source"] == "./plugins/demo"
    assert "ref" not in plugin
    assert "sha" not in plugin
    assert "author" not in plugin
    assert "tags" not in plugin
    assert "homepage" not in plugin
    assert "repository" not in plugin
    assert "license" not in plugin
    assert "category" not in plugin


def test_copilot_mapper_emits_relative_path_string_for_remote_without_subdir():
    entry = PackageEntry(
        name="demo",
        source="example.org/acme/demo",
        description="Demo plugin",
    )

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(
            _resolved(
                host="example.org",
                source_url="https://example.org/acme/demo",
            ),
        ),
        remote_metadata={"demo": {"version": "2.1.0"}},
    )

    plugin = result.document["plugins"][0]
    assert isinstance(plugin["source"], str)
    assert plugin["source"] == "./demo"


def test_copilot_mapper_omits_empty_description():
    entry = PackageEntry(
        name="demo",
        source="./plugins/demo",
        is_local=True,
    )

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(_resolved(),),
    )

    plugin = result.document["plugins"][0]
    assert "description" not in plugin


def test_copilot_mapper_normalizes_subdir_with_leading_dot_slash():
    """A ``subdir`` already written as ``./x`` is passed through unchanged."""
    entry = PackageEntry(name="demo", source="acme/demo", ref="main")

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(_resolved(subdir="./plugins/demo"),),
    )

    assert result.document["plugins"][0]["source"] == "./plugins/demo"


def test_copilot_mapper_normalizes_subdir_with_trailing_slash():
    """A ``subdir`` written with a trailing slash (``x/``) is normalized to
    a leading ``./`` and no trailing slash.
    """
    entry = PackageEntry(name="demo", source="acme/demo", ref="main")

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(_resolved(subdir="plugins/demo/"),),
    )

    assert result.document["plugins"][0]["source"] == "./plugins/demo"


def test_copilot_mapper_emits_verbose_diagnostic_when_dropping_pin_metadata():
    """Dropping a resolved ref/sha pin (schema has no field for it) is
    surfaced as a verbose diagnostic rather than silently discarded.
    """
    entry = PackageEntry(name="demo", source="acme/demo", ref="main")

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(
            _resolved(
                ref="v2.1.0",
                sha="0123456789abcdef0123456789abcdef01234567",
                subdir="plugins/demo",
            ),
        ),
    )

    messages = [d.message for d in result.diagnostics if d.level == "verbose"]
    assert any("demo" in m and "pin" in m for m in messages)


def test_copilot_mapper_emits_verbose_diagnostic_when_fabricating_subdir():
    """A remote package with no resolved ``subdir`` gets a fabricated
    ``./<name>`` path; this placeholder is surfaced as a verbose diagnostic.
    """
    entry = PackageEntry(name="demo", source="acme/demo", ref="main")

    result = CopilotMarketplaceMapper().compose(
        config=_config(entry),
        resolved=(_resolved(),),
    )

    messages = [d.message for d in result.diagnostics if d.level == "verbose"]
    assert any("demo" in m and "fabricated" in m for m in messages)
    assert result.document["plugins"][0]["source"] == "./demo"
