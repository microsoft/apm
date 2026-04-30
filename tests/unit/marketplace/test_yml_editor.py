"""Tests for ``apm_cli.marketplace.yml_editor`` – round-trip YAML editor."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.yml_editor import (
    add_plugin_entry,
    remove_plugin_entry,
    update_plugin_entry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yml(tmp_path: Path, content: str) -> Path:
    """Write *content* to ``marketplace.yml`` inside *tmp_path* and return the path."""
    p = tmp_path / "marketplace.yml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_BASIC_YML = """\
name: test-marketplace
description: Test marketplace
version: 1.0.0
owner:
  name: Test Owner
packages:
  - name: existing-package
    source: acme/existing-package
    version: ">=1.0.0"
    description: An existing package
"""


# ---------------------------------------------------------------------------
# add_plugin_entry – happy paths
# ---------------------------------------------------------------------------


class TestAddPluginHappy:
    def test_add_with_version(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        name = add_plugin_entry(yml, source="acme/new-tool", version=">=2.0.0")
        assert name == "new-tool"
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["packages"]]
        assert "new-tool" in names
        added = next(p for p in data["packages"] if p["name"] == "new-tool")
        assert added["version"] == ">=2.0.0"
        assert added["source"] == "acme/new-tool"

    def test_add_with_ref(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        name = add_plugin_entry(yml, source="acme/pinned-tool", ref="abc123")
        assert name == "pinned-tool"
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        added = next(p for p in data["packages"] if p["name"] == "pinned-tool")
        assert added["ref"] == "abc123"
        assert "version" not in added

    def test_add_with_all_optional_fields(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        name = add_plugin_entry(
            yml,
            source="acme/full-tool",
            version=">=3.0.0",
            subdir="src/plugin",
            tag_pattern="v{version}",
            tags=["utilities", "testing"],
            include_prerelease=True,
        )
        assert name == "full-tool"
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        added = next(p for p in data["packages"] if p["name"] == "full-tool")
        assert added["subdir"] == "src/plugin"
        assert added["tag_pattern"] == "v{version}"
        assert added["tags"] == ["utilities", "testing"]
        assert added["include_prerelease"] is True

    def test_name_defaults_to_repo_from_source(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        name = add_plugin_entry(yml, source="some-org/my-awesome-tool", version=">=1.0.0")
        assert name == "my-awesome-tool"

    def test_explicit_name_overrides_source(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        name = add_plugin_entry(
            yml,
            source="acme/repo-name",
            name="custom-name",
            version=">=1.0.0",
        )
        assert name == "custom-name"
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        added = next(p for p in data["packages"] if p["name"] == "custom-name")
        assert added["source"] == "acme/repo-name"

    def test_reparses_correctly(self, tmp_path):
        """The file written by add_plugin_entry re-loads through the schema."""
        from apm_cli.marketplace.yml_schema import load_marketplace_yml

        yml = _write_yml(tmp_path, _BASIC_YML)
        add_plugin_entry(yml, source="acme/new-tool", version=">=2.0.0")
        parsed = load_marketplace_yml(yml)
        names = [p.name for p in parsed.packages]
        assert "new-tool" in names


# ---------------------------------------------------------------------------
# add_plugin_entry – error paths
# ---------------------------------------------------------------------------


class TestAddPluginErrors:
    def test_duplicate_name_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="already exists"):
            add_plugin_entry(yml, source="acme/existing-package", version=">=1.0.0")

    def test_duplicate_name_case_insensitive(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="already exists"):
            add_plugin_entry(
                yml,
                source="acme/other",
                name="Existing-Package",
                version=">=1.0.0",
            )

    def test_both_version_and_ref_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="Cannot specify both"):
            add_plugin_entry(
                yml,
                source="acme/tool",
                version=">=1.0.0",
                ref="abc123",
            )

    def test_neither_version_nor_ref_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="At least one"):
            add_plugin_entry(yml, source="acme/tool")

    def test_invalid_source_no_slash_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="source"):
            add_plugin_entry(yml, source="noslash", version=">=1.0.0")

    def test_path_traversal_in_subdir_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError):
            add_plugin_entry(
                yml,
                source="acme/tool",
                version=">=1.0.0",
                subdir="../etc",
            )


# ---------------------------------------------------------------------------
# add_plugin_entry – comment preservation
# ---------------------------------------------------------------------------


class TestAddPluginCommentPreservation:
    def test_comments_survive_add(self, tmp_path):
        content = """\
        # Top-level comment
        name: test-marketplace
        description: Test marketplace
        version: 1.0.0
        owner:
          name: Test Owner
        packages:
          # Package section comment
          - name: existing-package
            source: acme/existing-package
            version: ">=1.0.0"
            description: An existing package
        """
        yml = _write_yml(tmp_path, content)
        add_plugin_entry(yml, source="acme/new-tool", version=">=2.0.0")
        text = yml.read_text(encoding="utf-8")
        assert "# Top-level comment" in text
        assert "# Package section comment" in text


# ---------------------------------------------------------------------------
# update_plugin_entry – happy paths
# ---------------------------------------------------------------------------


class TestUpdatePluginHappy:
    def test_update_version(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        update_plugin_entry(yml, "existing-package", version=">=2.0.0")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        assert entry["version"] == ">=2.0.0"

    def test_update_subdir(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        update_plugin_entry(yml, "existing-package", subdir="src/plugin")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        assert entry["subdir"] == "src/plugin"

    def test_setting_ref_clears_version(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        update_plugin_entry(yml, "existing-package", ref="deadbeef")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        assert entry["ref"] == "deadbeef"
        assert "version" not in entry

    def test_setting_version_clears_ref(self, tmp_path):
        """Start with a ref-pinned entry, then switch to version."""
        content = """\
        name: test-marketplace
        description: Test marketplace
        version: 1.0.0
        owner:
          name: Test Owner
        packages:
          - name: ref-pkg
            source: acme/ref-pkg
            ref: abc123
        """
        yml = _write_yml(tmp_path, content)
        update_plugin_entry(yml, "ref-pkg", version=">=1.0.0")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        assert entry["version"] == ">=1.0.0"
        assert "ref" not in entry

    def test_unmodified_fields_preserved(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        update_plugin_entry(yml, "existing-package", subdir="sub/dir")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        # Original fields are untouched.
        assert entry["source"] == "acme/existing-package"
        assert entry["version"] == ">=1.0.0"
        assert entry["name"] == "existing-package"

    def test_case_insensitive_match(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        update_plugin_entry(yml, "Existing-Package", subdir="sub/dir")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["packages"][0]
        assert entry["subdir"] == "sub/dir"


# ---------------------------------------------------------------------------
# update_plugin_entry – error paths
# ---------------------------------------------------------------------------


class TestUpdatePluginErrors:
    def test_package_not_found_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="not found"):
            update_plugin_entry(yml, "nonexistent", version=">=1.0.0")

    def test_both_version_and_ref_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="Cannot specify both"):
            update_plugin_entry(
                yml,
                "existing-package",
                version=">=2.0.0",
                ref="abc123",
            )


# ---------------------------------------------------------------------------
# remove_plugin_entry – happy paths
# ---------------------------------------------------------------------------


class TestRemovePluginHappy:
    def test_remove_existing_entry(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        remove_plugin_entry(yml, "existing-package")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        names = [p["name"] for p in (data.get("packages") or [])]
        assert "existing-package" not in names

    def test_case_insensitive_removal(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        remove_plugin_entry(yml, "Existing-Package")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        names = [p["name"] for p in (data.get("packages") or [])]
        assert "existing-package" not in names


# ---------------------------------------------------------------------------
# remove_plugin_entry – error paths
# ---------------------------------------------------------------------------


class TestRemovePluginErrors:
    def test_package_not_found_raises(self, tmp_path):
        yml = _write_yml(tmp_path, _BASIC_YML)
        with pytest.raises(MarketplaceYmlError, match="not found"):
            remove_plugin_entry(yml, "nonexistent")
