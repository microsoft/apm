"""Local-path package compose tests for MarketplaceBuilder.

Verifies that local sources (``./foo``) bypass git resolution and emit
plain-string ``source`` values per the Anthropic spec.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import MarketplaceBuilder, BuildOptions
from apm_cli.marketplace.migration import load_marketplace_config


_APM_WITH_LOCAL_BLOCK = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: A locally vendored tool.
      homepage: https://example.com/local-tool
      version: 0.1.0
      tags: [local, demo]
    - name: remote-tool
      source: acme/remote-tool
      ref: v1.0.0
      tags: [remote]
"""


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.fixture()
def project_with_local(tmp_path: Path) -> Path:
    _write(tmp_path / "apm.yml", _APM_WITH_LOCAL_BLOCK)
    return tmp_path


def test_local_package_skips_git_resolution(
    project_with_local: Path,
) -> None:
    """Local-path packages must not call git ls-remote."""
    config = load_marketplace_config(project_with_local)
    builder = MarketplaceBuilder.from_config(
        config, project_with_local, BuildOptions(offline=True)
    )
    # Resolve only the local entry.
    local_entry = next(p for p in config.packages if p.is_local)
    resolved = builder._resolve_entry(local_entry)
    assert resolved.source_repo == ""
    assert resolved.ref == ""
    assert resolved.sha == ""
    assert resolved.subdir == "./packages/local-tool"


def test_compose_emits_local_source_as_string(
    project_with_local: Path,
) -> None:
    """Local-path packages must emit ``source`` as a plain string."""
    config = load_marketplace_config(project_with_local)
    builder = MarketplaceBuilder.from_config(
        config, project_with_local, BuildOptions(offline=True)
    )

    local_entry = next(p for p in config.packages if p.is_local)
    local_resolved = builder._resolve_entry(local_entry)
    doc = builder.compose_marketplace_json([local_resolved])

    assert "plugins" in doc
    plugin = doc["plugins"][0]
    assert plugin["name"] == "local-tool"
    assert plugin["source"] == "./packages/local-tool"
    assert isinstance(plugin["source"], str)
    assert plugin["description"] == "A locally vendored tool."
    assert plugin["version"] == "0.1.0"
    assert plugin["homepage"] == "https://example.com/local-tool"


def test_compose_inherited_top_level_omits_description_and_version(
    project_with_local: Path,
) -> None:
    """When marketplace block inherits name/desc/version from the project,
    the resulting marketplace.json omits description and version at the
    top level (Anthropic spec: only emit what the maintainer set).
    """
    config = load_marketplace_config(project_with_local)
    builder = MarketplaceBuilder.from_config(
        config, project_with_local, BuildOptions(offline=True)
    )
    local_entry = next(p for p in config.packages if p.is_local)
    local_resolved = builder._resolve_entry(local_entry)
    doc = builder.compose_marketplace_json([local_resolved])

    assert doc["name"] == "my-project"
    assert "description" not in doc
    assert "version" not in doc


def test_legacy_compose_keeps_top_level_description(tmp_path: Path) -> None:
    """Legacy marketplace.yml files always set the override flags so
    the resulting marketplace.json keeps top-level description/version.
    """
    legacy = """\
        name: legacy-mp
        description: Legacy marketplace.
        version: 2.0.0
        owner:
          name: ACME
        packages:
          - name: tool
            source: acme/tool
            ref: v1.0.0
        """
    (tmp_path / "marketplace.yml").write_text(
        textwrap.dedent(legacy), encoding="utf-8"
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(
        config, tmp_path, BuildOptions(offline=True)
    )
    # Compose with no resolved packages -- we only inspect the top-level shape.
    doc = builder.compose_marketplace_json([])
    assert doc["name"] == "legacy-mp"
    assert doc["description"] == "Legacy marketplace."
    assert doc["version"] == "2.0.0"
