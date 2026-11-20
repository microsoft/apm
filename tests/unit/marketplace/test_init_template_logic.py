"""Tests for ``apm_cli.marketplace.init_template`` (pure logic cases)."""

from __future__ import annotations

import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest
import yaml

from apm_cli.marketplace.init_template import render_marketplace_yml_template

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


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
