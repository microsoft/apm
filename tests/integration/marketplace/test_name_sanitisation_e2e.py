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

    def test_rewrite_emits_warning_diagnostic(self, tmp_path: Path, mock_ref_resolver):
        """When the name is rewritten the build surfaces a warning diagnostic."""
        _write_yml(tmp_path, NON_KEBAB_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report = builder.build()

        name_diags = [
            d
            for d in report.diagnostics
            if d.level == "warning" and RAW_NAME in d.message and EXPECTED_NAME in d.message
        ]
        assert name_diags, "expected a warning diagnostic naming the old and new name"

    def test_kebab_name_emits_no_diagnostic(self, tmp_path: Path, mock_ref_resolver):
        """An already-kebab-case name must not trigger the rewrite warning."""
        _write_yml(tmp_path, NON_KEBAB_YML.replace(RAW_NAME, "already-kebab"))

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report = builder.build()

        assert not [d for d in report.diagnostics if "not kebab-case" in d.message]

    def test_all_special_name_falls_back(self, tmp_path: Path, mock_ref_resolver):
        """A name of only special characters falls back to the literal 'marketplace'."""
        _write_yml(tmp_path, NON_KEBAB_YML.replace(RAW_NAME, "..."))

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = json.loads((tmp_path / "marketplace.json").read_text(encoding="utf-8"))
        assert data["name"] == "marketplace"


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
