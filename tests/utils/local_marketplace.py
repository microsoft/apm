"""Source-only marketplace authoring helpers for hermetic lifecycle tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from apm_cli.marketplace.tag_pattern import validate_tag_pattern
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_package import LocalPackage, LocalPackageFactory


@dataclass(frozen=True)
class LocalMarketplace:
    """Paths identifying an authored marketplace producer."""

    name: str
    package: LocalPackage
    output_path: Path


class LocalMarketplaceFactory:
    """Author marketplace producer inputs without invoking product code."""

    def __init__(self, package_factory: LocalPackageFactory) -> None:
        self._packages = package_factory

    def create(
        self,
        name: str,
        *,
        source_base: str,
        packages: Sequence[Mapping[str, object]],
        tag_pattern: str,
    ) -> LocalMarketplace:
        """Create an ``apm.yml`` marketplace block for a later real CLI pack."""
        producer = self._packages.create(name)
        manifest = load_yaml(producer.manifest_path)
        manifest["marketplace"] = {
            "owner": {
                "name": "APM Lifecycle Tests",
                "url": "https://example.invalid/apm-lifecycle",
            },
            "sourceBase": source_base,
            "build": {
                "tagPattern": validate_tag_pattern(
                    tag_pattern,
                    context="marketplace.build.tagPattern",
                )
            },
            "packages": [dict(package) for package in packages],
        }
        dump_yaml(manifest, producer.manifest_path)
        return LocalMarketplace(
            name=name,
            package=producer,
            output_path=producer.root / ".claude-plugin" / "marketplace.json",
        )
