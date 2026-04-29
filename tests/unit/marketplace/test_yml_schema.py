"""Tests for marketplace.yml schema loader and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.yml_schema import (
    MarketplaceBuild,
    MarketplaceOwner,
    MarketplaceYml,
    PackageEntry,
    load_marketplace_yml,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yml(tmp_path: Path, content: str) -> Path:
    """Write *content* to ``marketplace.yml`` inside *tmp_path* and return the path."""
    p = tmp_path / "marketplace.yml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _minimal_yml(**overrides: object) -> str:
    """Return a minimal valid marketplace.yml with optional field overrides.

    Supports overriding top-level scalar fields, ``packages`` (as a raw
    YAML fragment), and ``owner`` (as a raw YAML fragment).
    """
    fields = {
        "name": "acme-tools",
        "description": "Acme marketplace",
        "version": "1.0.0",
    }
    fields.update(overrides)

    owner = fields.pop("owner", None)
    packages = fields.pop("packages", None)
    build = fields.pop("build", None)
    metadata = fields.pop("metadata", None)

    lines = []
    for k, v in fields.items():
        lines.append(f"{k}: \"{v}\"" if isinstance(v, str) else f"{k}: {v}")

    if owner is None:
        lines.append("owner:")
        lines.append("  name: Acme Corp")
    else:
        lines.append(owner)

    if metadata is not None:
        lines.append(metadata)

    if build is not None:
        lines.append(build)

    if packages is None:
        lines.append("packages:")
        lines.append("  - name: tool-a")
        lines.append("    source: acme/tool-a")
        lines.append("    version: \">=1.0.0\"")
    else:
        lines.append(packages)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestLoadHappyPath:
    """Verify that a well-formed marketplace.yml parses correctly."""

    def test_minimal_valid(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        assert result.name == "acme-tools"
        assert result.description == "Acme marketplace"
        assert result.version == "1.0.0"
        assert result.owner.name == "Acme Corp"
        assert result.output == "marketplace.json"
        assert result.metadata == {}
        assert result.build.tag_pattern == "v{version}"
        assert len(result.packages) == 1
        assert result.packages[0].name == "tool-a"

    def test_full_featured(self, tmp_path: Path):
        content = """\
        name: acme-tools
        description: Full-featured marketplace
        version: 2.1.0
        owner:
          name: Acme Corp
          email: tools@example.com
          url: https://example.com
        output: dist/marketplace.json
        metadata:
          pluginRoot: ./plugins
          customKey: some-value
        build:
          tagPattern: "release-{version}"
        packages:
          - name: linter
            source: acme/linter
            subdir: packages/core
            version: "^1.0.0"
            ref: v1.2.3
            tag_pattern: "linter-v{version}"
            include_prerelease: true
            description: A linting tool
            tags:
              - lint
              - quality
          - name: formatter
            source: acme/formatter
            ref: main
        """
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)

        assert result.name == "acme-tools"
        assert result.version == "2.1.0"
        assert result.owner.email == "tools@example.com"
        assert result.owner.url == "https://example.com"
        assert result.output == "dist/marketplace.json"
        # metadata preserves original casing
        assert result.metadata["pluginRoot"] == "./plugins"
        assert result.metadata["customKey"] == "some-value"
        assert result.build.tag_pattern == "release-{version}"

        linter = result.packages[0]
        assert linter.name == "linter"
        assert linter.source == "acme/linter"
        assert linter.subdir == "packages/core"
        assert linter.version == "^1.0.0"
        assert linter.ref == "v1.2.3"
        assert linter.tag_pattern == "linter-v{version}"
        assert linter.include_prerelease is True
        assert linter.tags == ("lint", "quality")

        formatter = result.packages[1]
        assert formatter.name == "formatter"
        assert formatter.ref == "main"
        assert formatter.version is None
        assert formatter.include_prerelease is False

    def test_metadata_preserved_verbatim(self, tmp_path: Path):
        """Anthropic-standard keys in metadata must round-trip with original casing."""
        content = _minimal_yml(
            metadata="metadata:\n  pluginRoot: ./src\n  anotherKey: 42"
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert "pluginRoot" in result.metadata
        assert result.metadata["pluginRoot"] == "./src"
        assert result.metadata["anotherKey"] == 42

    def test_empty_packages_list(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml(packages="packages: []"))
        result = load_marketplace_yml(yml)
        assert result.packages == ()

    def test_packages_omitted(self, tmp_path: Path):
        """``packages`` key missing defaults to empty tuple."""
        content = """\
        name: acme-tools
        description: No packages
        version: 0.1.0
        owner:
          name: Acme Corp
        """
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages == ()

    def test_output_default(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        assert result.output == "marketplace.json"

    def test_version_with_prerelease(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml(version="1.0.0-alpha.1"))
        result = load_marketplace_yml(yml)
        assert result.version == "1.0.0-alpha.1"

    def test_version_with_build_metadata(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml(version="1.0.0+build.42"))
        result = load_marketplace_yml(yml)
        assert result.version == "1.0.0+build.42"

    def test_tag_pattern_with_name_placeholder(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    version: \">=1.0.0\"\n"
                "    tag_pattern: \"{name}-v{version}\""
            )
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages[0].tag_pattern == "{name}-v{version}"


# ---------------------------------------------------------------------------
# Frozen / immutability
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    """Dataclasses must be immutable."""

    def test_marketplace_yml_frozen(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        with pytest.raises(AttributeError):
            result.name = "nope"

    def test_owner_frozen(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        with pytest.raises(AttributeError):
            result.owner.name = "nope"

    def test_package_entry_frozen(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        with pytest.raises(AttributeError):
            result.packages[0].name = "nope"

    def test_build_frozen(self):
        b = MarketplaceBuild()
        with pytest.raises(AttributeError):
            b.tag_pattern = "nope"


# ---------------------------------------------------------------------------
# Rejection tests -- required fields
# ---------------------------------------------------------------------------


class TestRequiredFieldRejection:
    """Missing or empty required fields must raise MarketplaceYmlError."""

    @pytest.mark.parametrize(
        "field",
        ["name", "description", "version"],
    )
    def test_missing_required_scalar(self, tmp_path: Path, field: str):
        overrides = {
            "name": "acme-tools",
            "description": "A marketplace",
            "version": "1.0.0",
        }
        del overrides[field]
        # Build YAML without the field
        lines = []
        for k, v in overrides.items():
            lines.append(f'{k}: "{v}"')
        lines.append("owner:\n  name: Acme Corp")
        lines.append("packages:\n  - name: x\n    source: acme/x\n    ref: main")
        yml = _write_yml(tmp_path, "\n".join(lines) + "\n")
        with pytest.raises(MarketplaceYmlError, match=field):
            load_marketplace_yml(yml)

    def test_missing_owner(self, tmp_path: Path):
        content = """\
        name: acme-tools
        description: desc
        version: 1.0.0
        packages:
          - name: x
            source: acme/x
            ref: main
        """
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="owner"):
            load_marketplace_yml(yml)

    def test_missing_owner_name(self, tmp_path: Path):
        content = _minimal_yml(owner="owner:\n  email: x@example.com")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="owner.name"):
            load_marketplace_yml(yml)

    def test_empty_name(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml(name=""))
        with pytest.raises(MarketplaceYmlError, match="name"):
            load_marketplace_yml(yml)


# ---------------------------------------------------------------------------
# Rejection tests -- version validation
# ---------------------------------------------------------------------------


class TestVersionRejection:
    """Invalid semver must be rejected."""

    @pytest.mark.parametrize(
        "bad_version",
        [
            "not-semver",
            "1.0",
            "1",
            "1.0.0.0",
            "abc.def.ghi",
        ],
    )
    def test_invalid_semver(self, tmp_path: Path, bad_version: str):
        yml = _write_yml(tmp_path, _minimal_yml(version=bad_version))
        with pytest.raises(MarketplaceYmlError, match="semver"):
            load_marketplace_yml(yml)


# ---------------------------------------------------------------------------
# Rejection tests -- packages
# ---------------------------------------------------------------------------


class TestPackageEntryRejection:
    """Per-entry validation rules."""

    def test_duplicate_package_names(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    ref: main\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a-v2\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="Duplicate"):
            load_marketplace_yml(yml)

    def test_duplicate_package_names_case_insensitive(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: Tool-A\n"
                "    source: acme/tool-a\n"
                "    ref: main\n"
                "  - name: tool-a\n"
                "    source: acme/tool-b\n"
                "    ref: v1"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="Duplicate"):
            load_marketplace_yml(yml)

    def test_missing_package_name(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - source: acme/tool\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="name"):
            load_marketplace_yml(yml)

    def test_missing_package_source(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_yml(yml)

    def test_invalid_source_shape_no_slash(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: just-a-name\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_yml(yml)

    def test_invalid_source_shape_multiple_slashes(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/repo/extra\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_yml(yml)

    def test_source_path_traversal(self, tmp_path: Path):
        """Source with '..' segments is rejected (regex catches multi-slash first)."""
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: ../evil/repo\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_yml(yml)

    def test_source_dotdot_as_owner(self, tmp_path: Path):
        """Source with '..' as the owner segment triggers path traversal."""
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: \"../repo\"\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            load_marketplace_yml(yml)

    def test_subdir_path_traversal(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool\n"
                "    subdir: ../../etc\n"
                "    ref: main"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            load_marketplace_yml(yml)

    def test_neither_version_nor_ref(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="version.*ref"):
            load_marketplace_yml(yml)

    def test_tag_pattern_no_placeholders(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    ref: main\n"
                "    tag_pattern: static-tag"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="tag_pattern"):
            load_marketplace_yml(yml)


# ---------------------------------------------------------------------------
# Rejection tests -- unknown keys (strict mode)
# ---------------------------------------------------------------------------


class TestUnknownKeyRejection:
    """Unknown keys must be rejected with a clear message."""

    def test_unknown_top_level_key(self, tmp_path: Path):
        content = _minimal_yml() + "unknown_field: value\n"
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="unknown_field"):
            load_marketplace_yml(yml)

    def test_unknown_package_entry_key(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    ref: main\n"
                "    bogus_field: hello"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="bogus_field"):
            load_marketplace_yml(yml)


# ---------------------------------------------------------------------------
# Rejection tests -- build block
# ---------------------------------------------------------------------------


class TestBuildBlockRejection:
    """Build-level validation."""

    def test_build_tag_pattern_no_placeholder(self, tmp_path: Path):
        content = _minimal_yml(
            build="build:\n  tagPattern: static-only"
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="tagPattern"):
            load_marketplace_yml(yml)

    def test_build_unknown_key(self, tmp_path: Path):
        content = _minimal_yml(
            build="build:\n  tagPattern: \"v{version}\"\n  extraKey: oops"
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="extraKey"):
            load_marketplace_yml(yml)

    def test_build_typo_tag_pattern(self, tmp_path: Path):
        """Common typo: snake_case ``tag_pattern`` instead of ``tagPattern``."""
        content = _minimal_yml(
            build="build:\n  tag_pattern: \"v{version}\""
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="tag_pattern"):
            load_marketplace_yml(yml)

    def test_build_permitted_keys_listed(self, tmp_path: Path):
        """Error message must list the permitted set so maintainers can self-correct."""
        content = _minimal_yml(
            build="build:\n  tagPatern: \"v{version}\""
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="tagPattern"):
            load_marketplace_yml(yml)


# ---------------------------------------------------------------------------
# Rejection tests -- YAML errors
# ---------------------------------------------------------------------------


class TestYamlErrorHandling:
    """YAML parse errors are wrapped in MarketplaceYmlError."""

    def test_invalid_yaml(self, tmp_path: Path):
        p = tmp_path / "marketplace.yml"
        p.write_text(":\n  - :\n    bad: [", encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="YAML parse error"):
            load_marketplace_yml(p)

    def test_yaml_not_a_mapping(self, tmp_path: Path):
        p = tmp_path / "marketplace.yml"
        p.write_text("- a list\n- not a mapping\n", encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="mapping"):
            load_marketplace_yml(p)

    def test_file_not_found(self, tmp_path: Path):
        p = tmp_path / "nonexistent.yml"
        with pytest.raises(MarketplaceYmlError, match="Cannot read"):
            load_marketplace_yml(p)


# ---------------------------------------------------------------------------
# Rejection tests -- metadata
# ---------------------------------------------------------------------------


class TestMetadataValidation:
    """Metadata block edge cases."""

    def test_metadata_not_a_mapping(self, tmp_path: Path):
        content = _minimal_yml(metadata="metadata: not-a-dict")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="metadata"):
            load_marketplace_yml(yml)

    def test_metadata_missing_defaults_to_empty(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        assert result.metadata == {}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Assorted edge-case coverage."""

    def test_entry_with_only_version_no_ref(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    version: \">=2.0.0\""
            )
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages[0].version == ">=2.0.0"
        assert result.packages[0].ref is None

    def test_entry_with_only_ref_no_version(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    ref: v3.0.0"
            )
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages[0].ref == "v3.0.0"
        assert result.packages[0].version is None

    def test_entry_with_both_version_and_ref(self, tmp_path: Path):
        """When both are set, both are stored. The builder resolves precedence."""
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    version: \">=1.0.0\"\n"
                "    ref: v1.2.3"
            )
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages[0].version == ">=1.0.0"
        assert result.packages[0].ref == "v1.2.3"

    def test_include_prerelease_not_bool(self, tmp_path: Path):
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: acme/tool-a\n"
                "    ref: main\n"
                "    include_prerelease: yes-please"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="include_prerelease"):
            load_marketplace_yml(yml)

    def test_packages_not_a_list(self, tmp_path: Path):
        content = _minimal_yml(packages="packages: not-a-list")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="packages"):
            load_marketplace_yml(yml)

    def test_build_not_a_mapping(self, tmp_path: Path):
        content = _minimal_yml(build="build: not-a-dict")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="build"):
            load_marketplace_yml(yml)

    def test_owner_not_a_mapping(self, tmp_path: Path):
        content = _minimal_yml(owner="owner: just-a-string")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="owner"):
            load_marketplace_yml(yml)

    def test_local_source_accepted(self, tmp_path: Path):
        """Local-path source './acme' is now valid (no version/ref needed)."""
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: ./acme"
            )
        )
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.packages[0].is_local is True
        assert result.packages[0].source == "./acme"

    def test_source_double_dot_rejected(self, tmp_path: Path):
        """``..`` traversal is still rejected for both remote and local sources."""
        content = _minimal_yml(
            packages=(
                "packages:\n"
                "  - name: tool-a\n"
                "    source: ./../acme"
            )
        )
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError):
            load_marketplace_yml(yml)

    def test_build_default_tag_pattern(self, tmp_path: Path):
        yml = _write_yml(tmp_path, _minimal_yml())
        result = load_marketplace_yml(yml)
        assert result.build.tag_pattern == "v{version}"


# ---------------------------------------------------------------------------
# S1: Output path traversal guard
# ---------------------------------------------------------------------------


class TestOutputPathTraversalGuard:
    """The ``output`` field must be rejected if it contains traversal sequences."""

    def test_output_traversal_rejected(self, tmp_path: Path):
        content = _minimal_yml(output="../../etc/passwd")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            load_marketplace_yml(yml)

    def test_output_safe_path_accepted(self, tmp_path: Path):
        content = _minimal_yml(output="build/marketplace.json")
        yml = _write_yml(tmp_path, content)
        result = load_marketplace_yml(yml)
        assert result.output == "build/marketplace.json"

    def test_output_dotdot_in_middle_rejected(self, tmp_path: Path):
        content = _minimal_yml(output="build/../../../evil.json")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            load_marketplace_yml(yml)

    def test_output_single_dot_rejected(self, tmp_path: Path):
        content = _minimal_yml(output="./marketplace.json")
        yml = _write_yml(tmp_path, content)
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            load_marketplace_yml(yml)
