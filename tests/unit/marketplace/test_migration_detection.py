"""Tests for marketplace config-source detection + smart loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.migration import (
    ConfigSource,
    DEPRECATION_MESSAGE,
    detect_config_source,
    load_marketplace_config,
)


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


_LEGACY_BODY = """\
name: my-marketplace
description: A marketplace.
version: 1.0.0
owner:
  name: ACME
output: marketplace.json
build:
  tagPattern: "v{version}"
packages:
  - name: tool-a
    source: acme/tool-a
    ref: main
    version: 1.0.0
"""


_APM_WITH_BLOCK = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  build:
    tagPattern: "v{version}"
  packages:
    - name: tool-a
      source: acme/tool-a
      ref: main
      version: 1.0.0
"""


_APM_WITHOUT_BLOCK = """\
name: my-project
description: A project.
version: 1.0.0
"""


class TestDetectConfigSource:
    def test_apm_yml_only(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", _APM_WITH_BLOCK)
        assert detect_config_source(tmp_path) == ConfigSource.APM_YML

    def test_legacy_only(self, tmp_path: Path) -> None:
        _write(tmp_path / "marketplace.yml", _LEGACY_BODY)
        assert detect_config_source(tmp_path) == ConfigSource.LEGACY_YML

    def test_neither(self, tmp_path: Path) -> None:
        assert detect_config_source(tmp_path) == ConfigSource.NONE

    def test_both_is_hard_error(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", _APM_WITH_BLOCK)
        _write(tmp_path / "marketplace.yml", _LEGACY_BODY)
        with pytest.raises(MarketplaceYmlError, match="Both apm.yml"):
            detect_config_source(tmp_path)

    def test_apm_yml_without_marketplace_block_is_legacy(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "apm.yml", _APM_WITHOUT_BLOCK)
        _write(tmp_path / "marketplace.yml", _LEGACY_BODY)
        # apm.yml has no marketplace: block, so legacy is the active source
        assert detect_config_source(tmp_path) == ConfigSource.LEGACY_YML

    def test_apm_yml_without_marketplace_block_alone_is_none(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "apm.yml", _APM_WITHOUT_BLOCK)
        assert detect_config_source(tmp_path) == ConfigSource.NONE


class TestLoadMarketplaceConfig:
    def test_apm_yml_load(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", _APM_WITH_BLOCK)
        config = load_marketplace_config(tmp_path)
        assert config.name == "my-project"
        assert config.is_legacy is False

    def test_legacy_load_emits_deprecation(self, tmp_path: Path) -> None:
        _write(tmp_path / "marketplace.yml", _LEGACY_BODY)
        warnings: list = []
        config = load_marketplace_config(
            tmp_path, warn_callback=warnings.append
        )
        assert config.is_legacy is True
        assert warnings == [DEPRECATION_MESSAGE]

    def test_no_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="No marketplace config"):
            load_marketplace_config(tmp_path)

    def test_both_files_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "apm.yml", _APM_WITH_BLOCK)
        _write(tmp_path / "marketplace.yml", _LEGACY_BODY)
        with pytest.raises(MarketplaceYmlError, match="Both apm.yml"):
            load_marketplace_config(tmp_path)
