"""Live authoring catalogs share native interpretation and filesystem safety."""

from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace import registry
from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.client import fetch_marketplace
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.output_profiles import DEFAULT_MARKETPLACE_OUTPUT
from apm_cli.marketplace.resolver import resolve_local_plugin_path
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

pytestmark = pytest.mark.component


def write_catalog(path: Path, *, name: str = "first", source: str = "./tools/one") -> None:
    path.write_text(
        f"name: preview\nversion: 1.0.0\nmarketplace:\n  owner:\n    name: Alice\n"
        f"  packages:\n    - name: {name}\n      source: {source}\n",
        encoding="utf-8",
    )


def test_invocation_snapshot_is_shared_but_next_invocation_rereads(tmp_path):
    path = tmp_path / "apm.yml"
    source = MarketplaceSource(name="preview", url=path.as_uri())
    write_catalog(path)
    with click.Context(click.Command("test")):
        first = fetch_marketplace(source, offline=True)
        write_catalog(path, name="second")
        second = fetch_marketplace(source, offline=True)
        assert first.plugins == second.plugins
    with click.Context(click.Command("test")):
        assert fetch_marketplace(source, offline=True).plugins[0].name == "second"


@pytest.mark.parametrize(
    "content", ["name: no-marketplace\n", "marketplace: [", "marketplace: []\n"]
)
def test_invalid_declaration_keeps_existing_registration(tmp_path, monkeypatch, content):
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / "config/config.json"))
    monkeypatch.setattr(registry, "_registry_cache", None)
    original = MarketplaceSource(name="preview", url="https://github.com/alice/original")
    registry.add_marketplace(original)
    path = tmp_path / "apm.yml"
    path.write_text(content, encoding="utf-8")
    result = CliRunner().invoke(marketplace, ["add", str(path), "--name", "preview"])
    assert result.exit_code == 1
    assert "marketplace declaration" in result.output
    assert registry.get_marketplace_by_name("preview") == original
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("escape", ["./../outside", "./tools/../../../outside"])
def test_paths_cannot_escape_authoring_root(tmp_path, escape):
    path = tmp_path / "apm.yml"
    write_catalog(path, source=escape)
    with pytest.raises(MarketplaceFetchError):
        fetch_marketplace(MarketplaceSource(name="preview", url=path.as_uri()), offline=True)


def test_symlink_cannot_escape_authoring_root(tmp_path):
    root = tmp_path / "author"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this host")
    path = root / "apm.yml"
    write_catalog(path, source="./escape")
    with pytest.raises(MarketplaceFetchError, match=r"outside|escape"):
        fetch_marketplace(MarketplaceSource(name="preview", url=path.as_uri()), offline=True)


def test_catalog_read_never_invokes_writing_pipeline(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path)
    before = path.read_bytes()
    with (
        patch("apm_cli.marketplace.builder.MarketplaceBuilder.build", side_effect=AssertionError),
        patch(
            "apm_cli.marketplace.builder.MarketplaceBuilder.write_output",
            side_effect=AssertionError,
        ),
    ):
        result = fetch_marketplace(
            MarketplaceSource(name="preview", url=path.as_uri()), offline=True
        )
    assert result.name == "preview"
    assert result.plugins[0].source == "./tools/one"
    assert path.read_bytes() == before
    assert not (tmp_path / ".claude-plugin").exists()


def test_offline_remote_range_reports_missing_refs(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path, source="alice/no-such-repo")
    path.write_text(path.read_text() + "      version: ^1.0.0\n", encoding="utf-8")
    with pytest.raises(MarketplaceFetchError, match=r"offline|Offline"):
        fetch_marketplace(MarketplaceSource(name="preview", url=path.as_uri()), offline=True)


def test_local_metadata_and_plugin_root_match_native_composition(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path)
    path.write_text(path.read_text() + "  metadata:\n    pluginRoot: tools\n", encoding="utf-8")
    package = tmp_path / "tools/one"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text(
        "name: one\nversion: 1.2.3\ndescription: Local tool\n", encoding="utf-8"
    )
    source = MarketplaceSource(name="preview", url=path.as_uri())
    manifest = fetch_marketplace(source, offline=True)
    plugin = manifest.plugins[0]
    assert manifest.owner_name == "Alice"
    assert plugin.version == "1.2.3"
    assert plugin.description == "Local tool"
    assert resolve_local_plugin_path(plugin.source, source, manifest.plugin_root) == package


def test_pinned_remote_commit_needs_no_ref_or_metadata_network(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path, source="alice/tool")
    sha = "a" * 40
    path.write_text(path.read_text() + f"      ref: {sha}\n", encoding="utf-8")
    with (
        patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=AssertionError,
        ),
        patch(
            "apm_cli.marketplace.builder.MarketplaceBuilder._fetch_remote_metadata",
            side_effect=AssertionError,
        ),
    ):
        manifest = fetch_marketplace(
            MarketplaceSource(name="preview", url=path.as_uri()), offline=True
        )
    assert manifest.plugins[0].source["sha"] == sha


def test_offline_repository_registration_fails_before_any_probe():
    with patch("apm_cli.marketplace.client._auto_detect_path", side_effect=AssertionError):
        result = CliRunner().invoke(marketplace, ["add", "alice/catalog", "--offline"])
    assert result.exit_code == 1
    assert "requires an explicitly selected local apm.yml" in result.output


@pytest.mark.parametrize("relative_source", ["./tools/nested/one", "./one"])
def test_plugin_root_never_rebases_authored_package_paths(tmp_path, relative_source):
    path = tmp_path / "apm.yml"
    write_catalog(path, source=relative_source)
    path.write_text(path.read_text() + "  metadata:\n    pluginRoot: tools\n", encoding="utf-8")
    source = MarketplaceSource(name="preview", url=path.as_uri())
    manifest = fetch_marketplace(source, offline=True)
    actual = resolve_local_plugin_path(manifest.plugins[0].source, source, manifest.plugin_root)
    assert actual == (tmp_path / relative_source).resolve()


def test_local_catalog_does_not_resolve_remote_credentials(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path)
    with patch(
        "apm_cli.marketplace.builder.MarketplaceBuilder._ensure_auth", side_effect=AssertionError
    ):
        manifest = fetch_marketplace(MarketplaceSource(name="preview", url=path.as_uri()))
    assert manifest.plugins[0].name == "first"


def test_discovery_composition_does_not_change_native_output(tmp_path):
    path = tmp_path / "apm.yml"
    write_catalog(path, source="./tools/nested/one")
    path.write_text(path.read_text() + "  metadata:\n    pluginRoot: tools\n", encoding="utf-8")
    config = load_marketplace_from_apm_yml(path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, options=BuildOptions(offline=True))
    resolved = builder.resolve().entries
    before, _, _ = builder.compose_output(DEFAULT_MARKETPLACE_OUTPUT, resolved)
    preview = builder.compose_discovery_catalog(resolved)
    after, _, _ = builder.compose_output(DEFAULT_MARKETPLACE_OUTPUT, resolved)
    assert before == after
    assert after["metadata"]["pluginRoot"] == "tools"
    assert after["plugins"][0]["source"] == "./nested/one"
    assert preview["plugins"][0]["source"] == "./tools/nested/one"
    assert "pluginRoot" not in preview.get("metadata", {})
    assert config.metadata == {"pluginRoot": "tools"}
