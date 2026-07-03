"""End-to-end integration tests for marketplace name sanitisation.

Strategy
--------
The unit suite (``tests/unit/marketplace/test_name_sanitisation.py``) proves
``sanitize_marketplace_name`` in isolation.  These tests provide the empirical
proof that the sanitisation actually reaches the on-disk artefact: they write a
real ``marketplace.yml`` whose ``name`` is NOT kebab-case, run the full build
pipeline (load -> resolve -> compose -> write), and assert against the produced
JSON documents for BOTH output mappers (Claude and Codex).

``RefResolver.list_remote_refs`` is patched via ``mock_ref_resolver`` so no
network calls are made; every assertion is against real file-system / composed
output produced by the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.marketplace.builder import MarketplaceBuilder

from .conftest import run_cli  # noqa: F401

# A marketplace name that GitHub accepts as a repo/manifest identifier but the
# Copilot App rejects: contains an uppercase letter, a dot, and an underscore.
RAW_NAME = "My.Marketplace_Name"
EXPECTED_NAME = "my-marketplace-name"

NON_KEBAB_YML = f"""\
name: {RAW_NAME}
description: Marketplace with a non-kebab-case name
version: 1.0.0
owner:
  name: Test Org
  email: test@example.com
  url: https://example.com
metadata:
  pluginRoot: plugins
  category: testing
packages:
  - name: code-reviewer
    description: Automated code review assistant
    source: acme/code-reviewer
    version: "^2.0.0"
    category: tools
    tags:
      - review
      - quality
  - name: test-generator
    description: Test generation tool
    source: acme/test-generator
    version: "^1.0.0"
    subdir: src/plugin
    category: tools
    tags:
      - testing
"""


def _write_yml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "marketplace.yml"
    p.write_text(content, encoding="utf-8")
    return p


class TestClaudeOutputSanitisesName:
    """The Claude marketplace.json written to disk must carry a kebab-case name."""

    def test_written_json_name_is_kebab_case(self, tmp_path: Path, mock_ref_resolver):
        """A non-kebab config name is normalised in the emitted marketplace.json."""
        _write_yml(tmp_path, NON_KEBAB_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        out_path = tmp_path / "marketplace.json"
        assert out_path.exists(), "marketplace.json was not produced"
        data = json.loads(out_path.read_text(encoding="utf-8"))

        assert data["name"] == EXPECTED_NAME

    def test_internal_package_names_untouched(self, tmp_path: Path, mock_ref_resolver):
        """Sanitisation is scoped to the top-level name -- plugin names are verbatim."""
        _write_yml(tmp_path, NON_KEBAB_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = json.loads((tmp_path / "marketplace.json").read_text(encoding="utf-8"))
        plugin_names = {p["name"] for p in data["plugins"]}
        assert plugin_names == {"code-reviewer", "test-generator"}


class TestCodexOutputSanitisesName:
    """The Codex document must sanitise ``name`` but preserve the display name."""

    def test_codex_name_sanitised_display_name_preserved(self, tmp_path: Path, mock_ref_resolver):
        """Codex ``name`` is kebab-case while ``interface.displayName`` keeps the raw name."""
        _write_yml(tmp_path, NON_KEBAB_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        result = builder.resolve()
        doc, _warnings = builder.compose_codex_marketplace_json(list(result.entries))

        assert doc["name"] == EXPECTED_NAME
        assert doc["interface"]["displayName"] == RAW_NAME
