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
from pathlib import Path

import pytest
import textwrap
from jsonschema import Draft7Validator

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.migration import load_marketplace_config


_SCHEMA_PATH = Path(__file__).parent.parent.parent / "fixtures" / "schemas" / "claude-code-marketplace.schema.json"
_SHA = "5544f427264d972b0e406d0b11a8ac31db9b18dc"


@pytest.fixture(scope="module")
def marketplace_validator() -> Draft7Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft7Validator(schema)


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_remote_entry_with_all_passthrough_fields_validates(
    tmp_path: Path, marketplace_validator: Draft7Validator
) -> None:
    """A remote entry exercising every Finding 2 field validates clean."""
    _write(
        tmp_path / "apm.yml",
        f"""\
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
    builder = MarketplaceBuilder.from_config(
        config, tmp_path, BuildOptions(offline=True)
    )
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
    assert errors == [], "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )


def test_local_entry_validates(
    tmp_path: Path, marketplace_validator: Draft7Validator
) -> None:
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
    builder = MarketplaceBuilder.from_config(
        config, tmp_path, BuildOptions(offline=True)
    )
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    doc = builder.compose_marketplace_json(resolved)
    errors = sorted(
        marketplace_validator.iter_errors(doc),
        key=lambda e: e.absolute_path,
    )
    assert errors == [], "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )


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
    builder = MarketplaceBuilder.from_config(
        config, tmp_path, BuildOptions(offline=True)
    )
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
    assert errors == [], "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )
