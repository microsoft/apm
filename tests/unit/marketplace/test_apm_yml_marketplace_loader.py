"""Tests for ``load_marketplace_from_apm_yml``.

Covers inheritance of name/description/version from the apm.yml top
level, override semantics inside the marketplace block, and rejection
of unknown keys at both levels.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


_MIN_BLOCK_INHERIT = """\
name: my-project
description: Project description.
version: 1.2.3
marketplace:
  owner:
    name: ACME
  packages:
    - name: tool-a
      source: acme/tool-a
      ref: v1.0.0
"""


_MIN_BLOCK_OVERRIDE = """\
name: my-project
description: Project description.
version: 1.2.3
marketplace:
  name: my-marketplace
  description: A separate marketplace.
  version: 9.9.9
  owner:
    name: ACME
  packages:
    - name: tool-a
      source: acme/tool-a
      ref: v1.0.0
"""


class TestInheritance:
    def test_name_description_version_inherited(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_INHERIT)
        config = load_marketplace_from_apm_yml(apm)
        assert config.name == "my-project"
        assert config.description == "Project description."
        assert config.version == "1.2.3"
        assert config.is_legacy is False
        assert config.name_overridden is False
        assert config.description_overridden is False
        assert config.version_overridden is False

    def test_overrides_take_precedence(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_OVERRIDE)
        config = load_marketplace_from_apm_yml(apm)
        assert config.name == "my-marketplace"
        assert config.description == "A separate marketplace."
        assert config.version == "9.9.9"
        assert config.name_overridden is True
        assert config.description_overridden is True
        assert config.version_overridden is True

    def test_default_output_is_claude_plugin(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_INHERIT)
        config = load_marketplace_from_apm_yml(apm)
        assert config.output == ".claude-plugin/marketplace.json"


class TestValidation:
    def test_missing_marketplace_block_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, "name: foo\nversion: 1.0.0\n")
        with pytest.raises(MarketplaceYmlError, match="marketplace"):
            load_marketplace_from_apm_yml(apm)

    def test_unknown_key_in_block_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, """\
            name: my-project
            marketplace:
              owner:
                name: A
              bogus: 1
              packages: []
            """)
        with pytest.raises(MarketplaceYmlError, match="bogus"):
            load_marketplace_from_apm_yml(apm)

    def test_missing_owner_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, """\
            name: foo
            version: 1.0.0
            description: x
            marketplace:
              packages: []
            """)
        with pytest.raises(MarketplaceYmlError, match="owner"):
            load_marketplace_from_apm_yml(apm)


class TestLocalPackages:
    def test_local_source_skips_version_requirement(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, """\
            name: my-project
            version: 1.0.0
            marketplace:
              owner:
                name: A
              packages:
                - name: local-tool
                  source: ./packages/local-tool
            """)
        config = load_marketplace_from_apm_yml(apm)
        pkg = config.packages[0]
        assert pkg.is_local is True
        assert pkg.source == "./packages/local-tool"
        assert pkg.version is None
        assert pkg.ref is None
