"""Tests for ``apm_cli.marketplace.init_template``."""

from __future__ import annotations

import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest  # noqa: F401
import yaml

from apm_cli.marketplace.init_template import render_marketplace_yml_template
from apm_cli.marketplace.yml_schema import load_marketplace_yml

# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_returns_non_empty_string(self):
        result = render_marketplace_yml_template()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_parseable_by_yaml_safe_load(self):
        text = render_marketplace_yml_template()
        data = yaml.safe_load(text)
        assert isinstance(data, dict)

    def test_roundtrips_through_load_marketplace_yml(self, tmp_path):
        text = render_marketplace_yml_template()
        fp = tmp_path / "marketplace.yml"
        fp.write_text(text, encoding="utf-8")
        yml = load_marketplace_yml(fp)
        assert yml.name == "my-marketplace"

    def test_contains_required_top_level_keys(self):
        text = render_marketplace_yml_template()
        data = yaml.safe_load(text)
        for key in ("name", "description", "version", "owner", "packages"):
            assert key in data, f"Missing top-level key: {key}"

    def test_owner_has_name(self):
        text = render_marketplace_yml_template()
        data = yaml.safe_load(text)
        assert "name" in data["owner"]

    def test_packages_is_list(self):
        text = render_marketplace_yml_template()
        data = yaml.safe_load(text)
        assert isinstance(data["packages"], list)
        assert len(data["packages"]) >= 1


# ---------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------


class TestTemplateSafety:
    def test_pure_ascii(self):
        text = render_marketplace_yml_template()
        text.encode("ascii")  # raises UnicodeEncodeError if non-ASCII

    def test_no_epam_references(self):
        text = render_marketplace_yml_template().lower()
        assert "epam" not in text
        assert "bookstore" not in text
        assert "agent-forge" not in text

    def test_contains_acme_org(self):
        text = render_marketplace_yml_template()
        assert "acme-org" in text

    def test_contains_build_section(self):
        text = render_marketplace_yml_template()
        data = yaml.safe_load(text)
        assert "build" in data
        assert "tagPattern" in data["build"]


# ---------------------------------------------------------------------------
# render_marketplace_block (apm.yml marketplace: block scaffold)
# ---------------------------------------------------------------------------


class TestRenderMarketplaceBlock:
    """Coverage for the inline ``marketplace:`` block scaffolded into apm.yml."""

    def test_roundtrips_through_yaml_safe_load(self):
        from apm_cli.marketplace.init_template import render_marketplace_block

        text = render_marketplace_block()
        data = yaml.safe_load(text)
        assert isinstance(data, dict)
        assert "marketplace" in data
        assert isinstance(data["marketplace"]["outputs"], dict)
        # G4: outputs is map form, claude enabled, codex commented
        assert "claude" in data["marketplace"]["outputs"]
        assert "codex" not in data["marketplace"]["outputs"]

    def test_outputs_codex_toggle_is_single_line(self):
        """G4: the commented codex toggle should be a one-liner."""
        from apm_cli.marketplace.init_template import render_marketplace_block

        text = render_marketplace_block()
        # The single-line commented toggle should be present, not a
        # multi-line block with 'path:'.
        assert "# codex: {}" in text

    def test_block_template_uses_snake_case_per_package_tag_pattern(self):
        """G5: per-package tag override must use the snake_case
        ``tag_pattern`` field (the camelCase ``tagPattern`` is the
        marketplace-level field and would fail schema validation if
        uncommented inside packages:)."""
        from apm_cli.marketplace.init_template import render_marketplace_block

        text = render_marketplace_block()
        # Per-package examples must use snake_case
        # (find the packages block)
        packages_section = text.split("packages:", 1)[1]
        # No camelCase tagPattern under packages
        assert "tagPattern" not in packages_section
        # The snake_case form is present (commented) in the example and
        # the local-path entry.
        assert packages_section.count("# tag_pattern:") >= 1

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
