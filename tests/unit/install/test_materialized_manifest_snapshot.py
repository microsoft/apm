"""Snapshot metadata and existing lock version-authority contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.source_helpers import materialized_manifest_snapshot
from apm_cli.models.apm_package import APMPackage, PackageType
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


@pytest.mark.parametrize("version", ["0.0.0", "1.1.0"])
def test_snapshot_reads_acquired_bytes_without_mutating_path_cache(
    tmp_path: Path, version: str
) -> None:
    """A fresh materialization uses current metadata and retains its source anchor."""
    manifest = tmp_path / "apm.yml"
    original_source = tmp_path / "source"
    dump_yaml({"name": "demo", "version": "1.0.0"}, manifest)
    stale = APMPackage.from_apm_yml(manifest, source_path=original_source)
    stale.source = "owner/demo"
    stale.resolved_commit = "a" * 40
    dump_yaml(
        {
            "name": "demo",
            "version": version,
            "dependencies": {"apm": ["../sibling"]},
            "x-snapshot": "current",
        },
        manifest,
    )

    snapshot = materialized_manifest_snapshot(stale, tmp_path, PackageType.APM_PACKAGE)

    assert snapshot.version == version
    assert snapshot.source_path == original_source.resolve()
    assert snapshot.package_path == tmp_path
    assert snapshot.source == "owner/demo"
    assert snapshot.resolved_commit == "a" * 40
    assert snapshot.extensions == {"x-snapshot": "current"}
    assert snapshot.get_apm_dependencies()[0].local_path == "../sibling"
    assert APMPackage.from_apm_yml(manifest, source_path=original_source) is stale
    assert stale.version == "1.0.0"


@pytest.mark.parametrize(
    "invalid",
    [
        "name: demo\n",
        "name: demo\nversion: ''\n",
        "name: demo\nversion: 2\n",
        "name: [\n",
    ],
)
def test_snapshot_does_not_hide_invalid_required_metadata(tmp_path: Path, invalid: str) -> None:
    """A cached valid descriptor cannot conceal invalid replacement metadata."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    stale = APMPackage.from_apm_yml(manifest)
    manifest.write_text(invalid, encoding="utf-8")

    with pytest.raises(DirectDependencyError, match="Invalid materialized manifest for demo"):
        materialized_manifest_snapshot(stale, tmp_path)


def test_snapshot_preserves_legacy_synthesized_projection(tmp_path: Path) -> None:
    """Synthetic manifest rewriting and SHA stamping stay with their existing owner."""
    from apm_cli.deps.package_validator import stamp_plugin_version
    from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin
    from apm_cli.models.validation import validate_legacy_marketplace_plugin

    plugin_manifest = tmp_path / "plugin.json"
    plugin_manifest.write_text('{"name": "demo"}', encoding="utf-8")
    validation = validate_legacy_marketplace_plugin(tmp_path, plugin_manifest)
    assert validation.is_valid, validation.errors
    assert validation.package_type == PackageType.MARKETPLACE_PLUGIN
    package = validation.package
    assert package is not None
    stamp_plugin_version(package, validation.package_type, "abcdef0" + "a" * 33, tmp_path)
    manifest = synthesize_apm_yml_from_plugin(tmp_path, {"name": "demo"})
    baseline = manifest.read_bytes()

    snapshot = materialized_manifest_snapshot(package, tmp_path, validation.package_type)

    assert snapshot is package
    assert snapshot.version == "abcdef0"
    assert manifest.read_bytes() == baseline
