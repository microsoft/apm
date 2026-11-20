"""Tests for ``apm_cli.marketplace.init_template`` (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from apm_cli.marketplace.init_template import render_marketplace_yml_template
from apm_cli.marketplace.yml_schema import load_marketplace_yml

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_roundtrips_through_load_marketplace_yml(self, tmp_path):
        text = render_marketplace_yml_template()
        fp = tmp_path / "marketplace.yml"
        fp.write_text(text, encoding="utf-8")
        yml = load_marketplace_yml(fp)
        assert yml.name == "my-marketplace"


# ---------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# render_marketplace_block (apm.yml marketplace: block scaffold)
# ---------------------------------------------------------------------------


class TestRenderMarketplaceBlock:
    """Coverage for the inline ``marketplace:`` block scaffolded into apm.yml."""

    def test_uncommented_tag_pattern_parses_under_packages(self, tmp_path):
        """G5: when a producer uncomments the suggested tag_pattern,
        the resulting apm.yml passes schema validation."""
        from apm_cli.marketplace.init_template import render_marketplace_block
        from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

        block = render_marketplace_block(owner="acme")
        # Build a minimal apm.yml around the block, with one package
        # whose tag_pattern is the suggested snake_case form.
        apm_yml = ("name: demo\ndescription: d\nversion: 0.1.0\n") + block.replace(
            '      # tag_pattern: "{name}-v{version}"',
            '      tag_pattern: "{name}-v{version}"',
            1,
        )
        path = tmp_path / "apm.yml"
        path.write_text(apm_yml, encoding="utf-8")

        cfg = load_marketplace_from_apm_yml(path)
        assert any((p.tag_pattern or "") == "{name}-v{version}" for p in cfg.packages), (
            "snake_case tag_pattern should parse cleanly under packages[]"
        )
