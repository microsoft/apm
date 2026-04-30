"""Schema-conformance test for marketplace.json output (issue #1061).

Validates the output of :class:`MarketplaceBuilder.compose_marketplace_json`
against the official Claude Code marketplace JSON schema published by
SchemaStore (https://www.schemastore.org/claude-code-marketplace.json).

The schema file is vendored under ``tests/fixtures/schemas/`` so the test
suite stays hermetic; refresh it manually when Anthropic publishes a new
version.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.migration import load_marketplace_config

_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "fixtures"
    / "schemas"
    / "claude-code-marketplace.schema.json"
)
_PLUGIN_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "fixtures" / "schemas" / "claude-code-plugin.schema.json"
)
_SHA = "5544f427264d972b0e406d0b11a8ac31db9b18dc"


@pytest.fixture(scope="module")
def marketplace_validator() -> Draft7Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft7Validator(schema)


@pytest.fixture(scope="module")
def plugin_validator() -> Draft7Validator:
    schema = json.loads(_PLUGIN_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft7Validator(schema)


# Marketplace-only fields that must be stripped before treating a
# marketplace plugin entry as a synthetic ``plugin.json``. See
# https://json.schemastore.org/claude-code-marketplace.json -- these are
# defined on the marketplace ``plugins[]`` item, not on the plugin manifest.
_MARKETPLACE_ONLY_PLUGIN_FIELDS = frozenset(
    {
        "source",
        "category",
        "strict",
    }
)


def _entry_as_plugin_json(entry: dict) -> dict:
    """Return a copy of ``entry`` with marketplace-only fields removed,
    suitable for validation against the plugin-manifest schema."""
    return {k: v for k, v in entry.items() if k not in _MARKETPLACE_ONLY_PLUGIN_FIELDS}


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_remote_entry_with_all_passthrough_fields_validates(
    tmp_path: Path, marketplace_validator: Draft7Validator
) -> None:
    """A remote entry exercising every Finding 2 field validates clean."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
            email: v@example.com
          packages:
            - name: azure
              source: microsoft/azure-skills
              ref: main
              version: 2.0.0
              description: Curator override
              author:
                name: Microsoft
                url: https://www.microsoft.com
              license: MIT
              repository: https://github.com/microsoft/azure-skills
              keywords: [azure, cloud, mcp]
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    resolved = [
        ResolvedPackage(
            name="azure",
            source_repo="microsoft/azure-skills",
            subdir=None,
            ref="main",
            sha=_SHA,
            requested_version="2.0.0",
            tags=("azure", "cloud", "mcp"),
            is_prerelease=False,
        ),
    ]
    doc = builder.compose_marketplace_json(resolved)
    errors = sorted(
        marketplace_validator.iter_errors(doc),
        key=lambda e: e.absolute_path,
    )
    assert errors == [], "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


def test_local_entry_validates(tmp_path: Path, marketplace_validator: Draft7Validator) -> None:
    """A local-source entry (post-pluginRoot subtraction) validates clean."""
    plugin_dir = tmp_path / "plugins" / "tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "skills").mkdir()
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
          metadata:
            pluginRoot: ./plugins
          packages:
            - name: tool
              source: ./plugins/tool
              description: Local tool
              version: 1.0.0
              author: Local Author
              license: Apache-2.0
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    doc = builder.compose_marketplace_json(resolved)
    errors = sorted(
        marketplace_validator.iter_errors(doc),
        key=lambda e: e.absolute_path,
    )
    assert errors == [], "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


def test_remote_subdir_entry_uses_git_subdir_form(
    tmp_path: Path, marketplace_validator: Draft7Validator
) -> None:
    """Remote entries with ``subdir`` emit the ``git-subdir`` source form."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
          packages:
            - name: subdir-tool
              source: acme/monorepo
              subdir: tools/claude-plugin
              ref: main
              version: 1.0.0
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    resolved = [
        ResolvedPackage(
            name="subdir-tool",
            source_repo="acme/monorepo",
            subdir="tools/claude-plugin",
            ref="main",
            sha=_SHA,
            requested_version="1.0.0",
            tags=(),
            is_prerelease=False,
        ),
    ]
    doc = builder.compose_marketplace_json(resolved)
    src = doc["plugins"][0]["source"]
    assert src["source"] == "git-subdir"
    assert src["url"] == "acme/monorepo"
    assert src["path"] == "tools/claude-plugin"
    assert src["sha"] == _SHA
    errors = sorted(
        marketplace_validator.iter_errors(doc),
        key=lambda e: e.absolute_path,
    )
    assert errors == [], "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


# ---------------------------------------------------------------------------
# Plugin-manifest schema conformance
# ---------------------------------------------------------------------------
#
# ``apm pack`` does not emit per-plugin ``plugin.json`` files (authors
# hand-write those; APM aggregates references). However, the marketplace
# schema permits each entry in ``plugins[]`` to carry plugin-manifest
# fields (description, version, author, license, repository, ...) for use
# when ``strict: false`` makes the marketplace entry authoritative. To
# guard against drift between APM's emit shape and the official plugin
# manifest schema, the tests below extract each marketplace entry, strip
# marketplace-only fields, and validate the remainder against the plugin
# manifest schema.


def test_remote_entry_validates_as_synthetic_plugin_json(
    tmp_path: Path,
    marketplace_validator: Draft7Validator,
    plugin_validator: Draft7Validator,
) -> None:
    """Each remote marketplace entry must validate as a plugin.json."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
          packages:
            - name: azure
              source: microsoft/azure-skills
              ref: main
              version: 2.0.0
              description: Azure skills curated.
              author:
                name: Microsoft
                email: opensource@microsoft.com
                url: https://www.microsoft.com
              license: MIT
              repository: https://github.com/microsoft/azure-skills
              keywords: [azure, cloud]
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    resolved = [
        ResolvedPackage(
            name="azure",
            source_repo="microsoft/azure-skills",
            subdir=None,
            ref="main",
            sha=_SHA,
            requested_version="2.0.0",
            tags=("azure", "cloud"),
            is_prerelease=False,
        ),
    ]
    doc = builder.compose_marketplace_json(resolved)
    # First confirm the marketplace doc is valid.
    mkt_errors = list(marketplace_validator.iter_errors(doc))
    assert mkt_errors == [], "marketplace failed: " + "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in mkt_errors
    )
    # Then confirm each entry, stripped of marketplace-only fields, is a
    # valid plugin.json.
    for entry in doc["plugins"]:
        synthetic = _entry_as_plugin_json(entry)
        plugin_errors = sorted(
            plugin_validator.iter_errors(synthetic),
            key=lambda e: e.absolute_path,
        )
        assert plugin_errors == [], (
            f"entry {entry.get('name')!r} fails plugin schema:\n"
            + "\n".join(f"  {list(e.absolute_path)}: {e.message}" for e in plugin_errors)
        )


def test_local_entry_validates_as_synthetic_plugin_json(
    tmp_path: Path,
    marketplace_validator: Draft7Validator,
    plugin_validator: Draft7Validator,
) -> None:
    """Local marketplace entries must also validate as plugin.json."""
    plugin_dir = tmp_path / "plugins" / "tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "skills").mkdir()
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
          metadata:
            pluginRoot: ./plugins
          packages:
            - name: tool
              source: ./plugins/tool
              description: Local tool
              version: 1.0.0
              author:
                name: Local Author
              license: Apache-2.0
              keywords: [local, demo]
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    doc = builder.compose_marketplace_json(resolved)
    for entry in doc["plugins"]:
        synthetic = _entry_as_plugin_json(entry)
        errors = sorted(
            plugin_validator.iter_errors(synthetic),
            key=lambda e: e.absolute_path,
        )
        assert errors == [], "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


def test_minimal_entry_validates_as_synthetic_plugin_json(
    tmp_path: Path,
    plugin_validator: Draft7Validator,
) -> None:
    """The minimum marketplace entry (just name+source) must satisfy the
    plugin-manifest schema after stripping marketplace-only fields."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: validation
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: Validator
          packages:
            - name: minimal
              source: acme/minimal
              version: 0.1.0
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    resolved = [
        ResolvedPackage(
            name="minimal",
            source_repo="acme/minimal",
            subdir=None,
            ref=None,
            sha=_SHA,
            requested_version="0.1.0",
            tags=(),
            is_prerelease=False,
        ),
    ]
    doc = builder.compose_marketplace_json(resolved)
    for entry in doc["plugins"]:
        synthetic = _entry_as_plugin_json(entry)
        errors = sorted(
            plugin_validator.iter_errors(synthetic),
            key=lambda e: e.absolute_path,
        )
        assert errors == [], "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
