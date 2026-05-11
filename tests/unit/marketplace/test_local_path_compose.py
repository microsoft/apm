"""Local-path package compose tests for MarketplaceBuilder.

Verifies that local sources (``./foo``) bypass git resolution and emit
plain-string ``source`` values per the Anthropic spec.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder, ResolvedPackage
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS

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
      category: Productivity
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
    builder = MarketplaceBuilder.from_config(config, project_with_local, BuildOptions(offline=True))
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
    builder = MarketplaceBuilder.from_config(config, project_with_local, BuildOptions(offline=True))

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
    assert "category" not in plugin


def test_compose_codex_marketplace_includes_local_and_remote_plugins(tmp_path: Path) -> None:
    _write(
        tmp_path / "apm.yml",
        """\
        name: codex-marketplace
        description: A project.
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          outputs: [codex]
          packages:
            - name: local-tool
              source: ./plugins/local-tool
              version: 0.1.0
              category: Productivity
            - name: remote-tool
              source: acme/remote-tool
              ref: v1.0.0
              category: Developer Tools
            - name: remote-subdir-tool
              source: acme/monorepo
              subdir: plugins/remote-subdir-tool
              ref: v2.0.0
              category: Coding
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(p for p in config.packages if p.is_local)
    remote_entry = next(p for p in config.packages if p.name == "remote-tool")
    remote_subdir_entry = next(p for p in config.packages if p.name == "remote-subdir-tool")
    resolved = [
        builder._resolve_entry(local_entry),
        # Construct the remote resolved shape directly; this test is about
        # Codex composition, not git ref resolution.
        ResolvedPackage(
            name=remote_entry.name,
            source_repo=remote_entry.source,
            subdir=remote_entry.subdir,
            ref="v1.0.0",
            sha="a" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        ),
        ResolvedPackage(
            name=remote_subdir_entry.name,
            source_repo=remote_subdir_entry.source,
            subdir=remote_subdir_entry.subdir,
            ref="v2.0.0",
            sha="b" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        ),
    ]

    doc, warnings = builder.compose_codex_marketplace_json(resolved)

    assert doc == {
        "name": "codex-marketplace",
        "interface": {"displayName": "codex-marketplace"},
        "plugins": [
            {
                "name": "local-tool",
                "source": {"source": "local", "path": "./plugins/local-tool"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            },
            {
                "name": "remote-tool",
                "source": {
                    "source": "url",
                    "url": "acme/remote-tool",
                    "ref": "v1.0.0",
                    "sha": "a" * 40,
                },
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Developer Tools",
            },
            {
                "name": "remote-subdir-tool",
                "source": {
                    "source": "git-subdir",
                    "url": "acme/monorepo",
                    "path": "plugins/remote-subdir-tool",
                    "ref": "v2.0.0",
                    "sha": "b" * 40,
                },
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Coding",
            },
        ],
    }
    assert warnings == ()


def test_write_codex_output_profile(tmp_path: Path) -> None:
    _write(
        tmp_path / "apm.yml",
        """\
        name: codex-marketplace
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          outputs: [codex]
          codex:
            output: .agents/plugins/marketplace.json
          packages:
            - name: local-tool
              source: ./plugins/local-tool
              category: Productivity
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = config.packages[0]
    resolved = (builder._resolve_entry(local_entry),)

    report = builder.write_output(
        MARKETPLACE_OUTPUTS["codex"],
        resolved,
        tmp_path / ".agents" / "plugins" / "marketplace.json",
    )

    assert report.warnings == ()
    assert report.output_path == tmp_path / ".agents" / "plugins" / "marketplace.json"
    assert report.resolved == resolved
    text = report.output_path.read_text(encoding="utf-8")
    assert '"source": "local"' in text
    assert '"path": "./plugins/local-tool"' in text


def test_compose_inherited_top_level_omits_description_and_version(
    project_with_local: Path,
) -> None:
    """When marketplace block inherits name/desc/version from the project,
    the resulting marketplace.json omits description and version at the
    top level (Anthropic spec: only emit what the maintainer set).
    """
    config = load_marketplace_config(project_with_local)
    builder = MarketplaceBuilder.from_config(config, project_with_local, BuildOptions(offline=True))
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
    (tmp_path / "marketplace.yml").write_text(textwrap.dedent(legacy), encoding="utf-8")
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    # Compose with no resolved packages -- we only inspect the top-level shape.
    doc = builder.compose_marketplace_json([])
    assert doc["name"] == "legacy-mp"
    assert doc["description"] == "Legacy marketplace."
    assert doc["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# pluginRoot subtraction tests (#1061)
# ---------------------------------------------------------------------------


_APM_WITH_PLUGIN_ROOT = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  metadata:
    pluginRoot: "./plugins"
  packages:
    - name: foo-tool
      source: ./plugins/foo-tool
      description: Foo tool.
    - name: nested-tool
      source: ./plugins/sub/deep
      description: Nested.
"""


@pytest.fixture()
def project_with_plugin_root(tmp_path: Path) -> Path:
    _write(tmp_path / "apm.yml", _APM_WITH_PLUGIN_ROOT)
    return tmp_path


def test_plugin_root_subtraction_strips_prefix(
    project_with_plugin_root: Path,
) -> None:
    """pluginRoot prefix is subtracted from local source paths."""
    config = load_marketplace_config(project_with_plugin_root)
    builder = MarketplaceBuilder.from_config(
        config, project_with_plugin_root, BuildOptions(offline=True)
    )
    local_entries = [e for e in config.packages if e.is_local]
    resolved = [builder._resolve_entry(e) for e in local_entries]
    doc = builder.compose_marketplace_json(resolved)

    plugins = doc["plugins"]
    assert plugins[0]["source"] == "./foo-tool"


def test_plugin_root_subtraction_nested(
    project_with_plugin_root: Path,
) -> None:
    """Nested paths under pluginRoot are correctly subtracted."""
    config = load_marketplace_config(project_with_plugin_root)
    builder = MarketplaceBuilder.from_config(
        config, project_with_plugin_root, BuildOptions(offline=True)
    )
    local_entries = [e for e in config.packages if e.is_local]
    resolved = [builder._resolve_entry(e) for e in local_entries]
    doc = builder.compose_marketplace_json(resolved)

    plugins = doc["plugins"]
    assert plugins[1]["source"] == "./sub/deep"


def test_plugin_root_unset_emits_verbatim(tmp_path: Path) -> None:
    """When pluginRoot is not set, source is emitted verbatim."""
    content = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: tool
      source: ./packages/bar
"""
    _write(tmp_path / "apm.yml", content)
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    doc = builder.compose_marketplace_json(resolved)
    assert doc["plugins"][0]["source"] == "./packages/bar"


def test_plugin_root_mismatch_emits_verbatim_with_warning(
    tmp_path: Path,
) -> None:
    """Source outside pluginRoot is emitted verbatim with W1 warning."""
    content = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  metadata:
    pluginRoot: "./plugins"
  packages:
    - name: baz
      source: ./other/baz
"""
    _write(tmp_path / "apm.yml", content)
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    doc = builder.compose_marketplace_json(resolved)
    assert doc["plugins"][0]["source"] == "./other/baz"
    # Check warning was recorded
    diagnostics = getattr(builder, "_compose_diagnostics", ())
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert any("outside pluginRoot" in w.message for w in warnings)


def test_plugin_root_subtraction_empty_path_errors(tmp_path: Path) -> None:
    """Source == pluginRoot yields empty path -> BuildError."""
    from apm_cli.marketplace.errors import BuildError

    content = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  metadata:
    pluginRoot: "./plugins"
  packages:
    - name: bad
      source: ./plugins
"""
    _write(tmp_path / "apm.yml", content)
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entry = next(e for e in config.packages if e.is_local)
    resolved = [builder._resolve_entry(local_entry)]
    with pytest.raises(BuildError, match="yields empty path"):
        builder.compose_marketplace_json(resolved)
