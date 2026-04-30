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
