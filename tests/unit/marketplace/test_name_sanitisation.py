"""Tests for marketplace name sanitisation in output mappers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.marketplace.output_mappers import sanitise_marketplace_name


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("my-marketplace", "my-marketplace"),
        ("My-Marketplace", "my-marketplace"),
        ("my.marketplace", "my-marketplace"),
        ("my_marketplace", "my-marketplace"),
        ("my marketplace", "my-marketplace"),
        ("my/marketplace", "my-marketplace"),
        (r"my\\marketplace", "my-marketplace"),
        ("my@marketplace!", "my-marketplace"),
        ("my--marketplace", "my-marketplace"),
        ("my...marketplace", "my-marketplace"),
        ("--my-marketplace--", "my-marketplace"),
        ("123marketplace", "123marketplace"),
        ("MiXeD_case.Name", "mixed-case-name"),
        ("", "marketplace"),
        ("   ", "marketplace"),
        ("!!!", "marketplace"),
    ],
)
def test_sanitise_marketplace_name(raw_name: str, expected: str) -> None:
    assert sanitise_marketplace_name(raw_name) == expected


def test_claude_output_sanitises_top_level_marketplace_name(tmp_path: Path) -> None:
    _write(
        tmp_path / "apm.yml",
        """\
        name: My.Market_Place
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: local-tool
              source: ./packages/local-tool
        """,
    )

    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(entry for entry in config.packages if entry.is_local)

    doc = builder.compose_marketplace_json([builder._resolve_entry(local_entry)])

    assert config.name == "My.Market_Place"
    assert doc["name"] == "my-market-place"
    assert doc["plugins"][0]["name"] == "local-tool"


def test_codex_output_sanitises_name_but_keeps_display_name(tmp_path: Path) -> None:
    _write(
        tmp_path / "apm.yml",
        """\
        name: My.Market_Place
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          outputs: [codex]
          packages:
            - name: local-tool
              source: ./plugins/local-tool
              category: Productivity
        """,
    )

    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(entry for entry in config.packages if entry.is_local)

    doc, warnings = builder.compose_codex_marketplace_json([builder._resolve_entry(local_entry)])

    assert config.name == "My.Market_Place"
    assert doc["name"] == "my-market-place"
    assert doc["interface"]["displayName"] == "My.Market_Place"
    assert warnings == ()
