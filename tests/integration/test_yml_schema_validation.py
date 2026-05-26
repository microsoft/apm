from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS
from apm_cli.marketplace.yml_schema import (
    MarketplaceOutputSpec,
    MarketplaceYmlError,
    _build_config,
    _check_unknown_keys,
    _parse_author,
    _parse_build,
    _parse_claude,
    _parse_codex,
    _parse_outputs,
    _parse_owner,
    _parse_package_entry,
    _parse_versioning,
    _read_yaml_mapping,
    _validate_semver,
    _validate_source,
    _validate_tag_pattern,
    load_marketplace_from_apm_yml,
    load_marketplace_from_legacy_yml,
)


class DuplicateKeyDict(dict):
    def items(self):
        return [("claude", {}), ("claude", {})]


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def _minimal_package_entry() -> dict[str, object]:
    return {
        "name": "pkg",
        "source": "owner/repo",
        "ref": "main",
    }


def _legacy_yaml(body: str = "packages: []\n") -> str:
    return textwrap.dedent(
        f"""
        name: test-marketplace
        description: Test marketplace
        version: 1.2.3
        owner:
          name: Jane Doe
        {body}
        """
    )


def _apm_yaml(marketplace: str, *, top_level: str = "") -> str:
    top_lines = [
        "name: top-level-name",
        "description: Top level description",
        "version: 2.3.4",
    ]
    extra = textwrap.dedent(top_level).strip()
    if extra:
        top_lines.append(extra)
    top_lines.append("marketplace:")
    top_lines.append(textwrap.indent(textwrap.dedent(marketplace).strip(), "  "))
    return "\n".join(top_lines) + "\n"


class TestParseAuthor:
    def test_parse_author_none_returns_none(self) -> None:
        assert _parse_author(None, 0) is None

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_parse_author_rejects_empty_string(self, raw: str) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.author"):
            _parse_author(raw, 0)

    def test_parse_author_accepts_string(self) -> None:
        assert _parse_author("  Jane Doe  ", 1) == {"name": "Jane Doe"}

    def test_parse_author_accepts_mapping_with_name_only(self) -> None:
        assert _parse_author({"name": "Jane"}, 2) == {"name": "Jane"}

    def test_parse_author_accepts_mapping_with_email_and_url(self) -> None:
        result = _parse_author(
            {"name": "Jane", "email": " jane@example.com ", "url": " https://example.com "},
            3,
        )
        assert result == {
            "name": "Jane",
            "email": "jane@example.com",
            "url": "https://example.com",
        }

    def test_parse_author_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="unknown key"):
            _parse_author({"name": "Jane", "extra": "nope"}, 0)

    @pytest.mark.parametrize("raw", [{}, {"name": ""}, {"name": "   "}])
    def test_parse_author_requires_non_empty_name(self, raw: dict[str, str]) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.author\.name"):
            _parse_author(raw, 0)

    @pytest.mark.parametrize("field", ["email", "url"])
    def test_parse_author_rejects_empty_optional_fields(self, field: str) -> None:
        with pytest.raises(MarketplaceYmlError, match=field):
            _parse_author({"name": "Jane", field: "   "}, 0)

    def test_parse_author_rejects_non_string_or_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be a string or object"):
            _parse_author(42, 0)


class TestSchemaHelpers:
    @pytest.mark.parametrize("version", ["1.0.0", "2.3.4-alpha.1", "3.0.0+build.5"])
    def test_validate_semver_accepts_valid_versions(self, version: str) -> None:
        _validate_semver(version, context="version")

    @pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", "one.two.three"])
    def test_validate_semver_rejects_invalid_versions(self, version: str) -> None:
        with pytest.raises(MarketplaceYmlError, match="semver"):
            _validate_semver(version, context="version")

    @pytest.mark.parametrize("source", ["owner/repo", "./packages/pkg", "./"])
    def test_validate_source_accepts_valid_shapes(self, source: str) -> None:
        _validate_source(source, index=0)

    @pytest.mark.parametrize("source", ["owner", "owner/repo/extra"])
    def test_validate_source_rejects_invalid_shape(self, source: str) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be one of"):
            _validate_source(source, index=0)

    @pytest.mark.parametrize("source", ["../repo", "./../repo"])
    def test_validate_source_rejects_traversal(self, source: str) -> None:
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _validate_source(source, index=0)

    @pytest.mark.parametrize("pattern", ["v{version}", "release-{name}"])
    def test_validate_tag_pattern_accepts_placeholder(self, pattern: str) -> None:
        _validate_tag_pattern(pattern, context="build.tagPattern")

    def test_validate_tag_pattern_rejects_missing_placeholder(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must contain at least one"):
            _validate_tag_pattern("release", context="build.tagPattern")

    def test_check_unknown_keys_allows_clean_mapping(self) -> None:
        _check_unknown_keys({"name": "value"}, frozenset({"name"}), context="owner")

    def test_check_unknown_keys_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in owner: extra"):
            _check_unknown_keys({"name": "x", "extra": 1}, frozenset({"name"}), context="owner")

    def test_parse_owner_rejects_non_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="'owner' must be a mapping"):
            _parse_owner("Jane")

    def test_parse_owner_accepts_mapping_and_normalises_optional_fields(self) -> None:
        owner = _parse_owner({"name": " Jane ", "email": 123, "url": " https://example.com "})
        assert owner.name == "Jane"
        assert owner.email == "123"
        assert owner.url == "https://example.com"

    def test_parse_owner_converts_blank_optional_fields_to_none(self) -> None:
        owner = _parse_owner({"name": "Jane", "email": "   ", "url": ""})
        assert owner.email is None
        assert owner.url is None


class TestBuildAndVersioningParsers:
    def test_parse_build_none_returns_default(self) -> None:
        assert _parse_build(None).tag_pattern == "v{version}"

    def test_parse_build_requires_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="'build' must be a mapping"):
            _parse_build("bad")

    def test_parse_build_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in build"):
            _parse_build({"tagPattern": "v{version}", "extra": True})

    def test_parse_build_requires_non_empty_tag_pattern(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"build\.tagPattern"):
            _parse_build({"tagPattern": "   "})

    def test_parse_build_rejects_pattern_without_placeholder(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must contain at least one"):
            _parse_build({"tagPattern": "release"})

    def test_parse_build_accepts_valid_pattern(self) -> None:
        assert _parse_build({"tagPattern": "release-{version}"}).tag_pattern == "release-{version}"

    def test_parse_versioning_none_returns_default(self) -> None:
        assert _parse_versioning(None).strategy == "lockstep"

    def test_parse_versioning_requires_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="'versioning' must be a mapping"):
            _parse_versioning(["bad"])

    def test_parse_versioning_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in versioning"):
            _parse_versioning({"strategy": "lockstep", "extra": 1})

    def test_parse_versioning_rejects_empty_strategy(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"versioning\.strategy"):
            _parse_versioning({"strategy": "   "})

    def test_parse_versioning_rejects_invalid_strategy(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be one of"):
            _parse_versioning({"strategy": "nope"})

    @pytest.mark.parametrize("strategy", ["lockstep", "tag_pattern", "per_package"])
    def test_parse_versioning_accepts_known_strategies(self, strategy: str) -> None:
        assert _parse_versioning({"strategy": strategy}).strategy == strategy


class TestOutputConfigParsers:
    def test_parse_claude_none_uses_default_output(self) -> None:
        assert _parse_claude(None, default_output="dist/claude.json").output == "dist/claude.json"

    def test_parse_claude_requires_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="'claude' must be a mapping"):
            _parse_claude("bad", default_output="x.json")

    def test_parse_claude_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in claude"):
            _parse_claude({"output": "x.json", "extra": True}, default_output="x.json")

    def test_parse_claude_rejects_empty_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"claude\.output"):
            _parse_claude({"output": "   "}, default_output="x.json")

    def test_parse_claude_rejects_traversal_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _parse_claude({"output": "../escape.json"}, default_output="x.json")

    def test_parse_claude_accepts_valid_output(self) -> None:
        assert (
            _parse_claude({"output": "dist/claude.json"}, default_output="x.json").output
            == "dist/claude.json"
        )

    def test_parse_codex_none_uses_profile_default(self) -> None:
        assert _parse_codex(None).output == MARKETPLACE_OUTPUTS["codex"].default_output

    def test_parse_codex_requires_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="'codex' must be a mapping"):
            _parse_codex("bad")

    def test_parse_codex_rejects_unknown_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in codex"):
            _parse_codex({"output": "x.json", "extra": True})

    def test_parse_codex_rejects_empty_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"codex\.output"):
            _parse_codex({"output": "   "})

    def test_parse_codex_rejects_traversal_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _parse_codex({"output": "../escape.json"})

    def test_parse_codex_accepts_valid_output(self) -> None:
        assert _parse_codex({"output": "dist/codex.json"}).output == "dist/codex.json"


class TestParseOutputs:
    def test_parse_outputs_none_returns_default(self) -> None:
        outputs, specs = _parse_outputs(None)
        assert outputs == ("claude",)
        assert specs == (
            MarketplaceOutputSpec(
                name="claude",
                path=MARKETPLACE_OUTPUTS["claude"].default_output,
                path_explicit=False,
            ),
        )

    def test_parse_outputs_dict_accepts_null_and_explicit_path(self) -> None:
        outputs, specs = _parse_outputs(
            {"claude": {"path": "dist/claude.json"}, "codex": None},
            warnings_sink=[],
        )
        assert outputs == ("claude", "codex")
        assert specs[0].path == "dist/claude.json"
        assert specs[0].path_explicit is True
        assert specs[1].path == MARKETPLACE_OUTPUTS["codex"].default_output

    def test_parse_outputs_dict_requires_non_empty_keys(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="map keys must be non-empty strings"):
            _parse_outputs({"": {}}, warnings_sink=[])

    def test_parse_outputs_dict_rejects_unknown_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="Unknown marketplace output 'unknown'"):
            _parse_outputs({"unknown": {}}, warnings_sink=[])

    def test_parse_outputs_dict_rejects_duplicate_output_name(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="Duplicate marketplace output 'claude'"):
            _parse_outputs(DuplicateKeyDict(), warnings_sink=[])

    def test_parse_outputs_dict_requires_mapping_or_null_entries(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"outputs\.claude"):
            _parse_outputs({"claude": "dist/claude.json"}, warnings_sink=[])

    def test_parse_outputs_dict_requires_non_empty_path(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"outputs\.claude\.path"):
            _parse_outputs({"claude": {"path": "   "}}, warnings_sink=[])

    def test_parse_outputs_dict_rejects_traversal_path(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _parse_outputs({"claude": {"path": "../escape.json"}}, warnings_sink=[])

    def test_parse_outputs_dict_rejects_unknown_entry_keys(self) -> None:
        with pytest.raises(
            MarketplaceYmlError, match=r"Unknown key\(s\) in 'outputs\.claude': extra"
        ):
            _parse_outputs({"claude": {"path": "dist/out.json", "extra": True}}, warnings_sink=[])

    def test_parse_outputs_dict_rejects_empty_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must contain at least one"):
            _parse_outputs({}, warnings_sink=[])

    def test_parse_outputs_list_emits_deprecation_warning(self) -> None:
        warnings_sink: list[str] = []
        outputs, specs = _parse_outputs(["claude", "codex"], warnings_sink=warnings_sink)
        assert outputs == ("claude", "codex")
        assert len(specs) == 2
        assert len(warnings_sink) == 1
        assert "deprecated" in warnings_sink[0]

    def test_parse_outputs_list_rejects_empty_item(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"outputs\[0\]"):
            _parse_outputs(["   "], warnings_sink=[])

    def test_parse_outputs_list_rejects_unknown_output(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="Unknown marketplace output 'unknown'"):
            _parse_outputs(["unknown"], warnings_sink=[])

    def test_parse_outputs_list_rejects_duplicates(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="Duplicate marketplace output 'claude'"):
            _parse_outputs(["claude", "claude"], warnings_sink=[])

    def test_parse_outputs_list_rejects_empty_list(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must contain at least one"):
            _parse_outputs([], warnings_sink=[])

    def test_parse_outputs_string_form_is_supported(self) -> None:
        warnings_sink: list[str] = []
        outputs, specs = _parse_outputs("codex", warnings_sink=warnings_sink)
        assert outputs == ("codex",)
        assert specs[0].path == MARKETPLACE_OUTPUTS["codex"].default_output
        assert warnings_sink

    def test_parse_outputs_rejects_invalid_root_type(self) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be a string, list, or mapping"):
            _parse_outputs(123, warnings_sink=[])


class TestParsePackageEntry:
    def test_parse_package_entry_requires_mapping(self) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\] must be a mapping"):
            _parse_package_entry("bad", 0)

    def test_parse_package_entry_rejects_unknown_keys(self) -> None:
        entry = _minimal_package_entry() | {"extra": True}
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in packages\[0\]"):
            _parse_package_entry(entry, 0)

    @pytest.mark.parametrize("field", ["name", "source"])
    def test_parse_package_entry_requires_name_and_source(self, field: str) -> None:
        entry = _minimal_package_entry()
        entry.pop(field)
        with pytest.raises(MarketplaceYmlError, match=field):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_invalid_source(self) -> None:
        entry = _minimal_package_entry() | {"source": "invalid-source"}
        with pytest.raises(MarketplaceYmlError, match="must be one of"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_accepts_local_source_without_ref_or_version(self) -> None:
        entry = {"name": "pkg", "source": "./packages/pkg"}
        parsed = _parse_package_entry(entry, 0)
        assert parsed.is_local is True
        assert parsed.ref is None
        assert parsed.version is None

    def test_parse_package_entry_rejects_subdir_with_traversal(self) -> None:
        entry = _minimal_package_entry() | {"subdir": "../escape"}
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_empty_version(self) -> None:
        entry = _minimal_package_entry() | {"version": "   "}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.version"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_empty_ref(self) -> None:
        entry = _minimal_package_entry() | {"ref": "   "}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.ref"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_requires_ref_or_version_for_remote_sources(self) -> None:
        entry = {"name": "pkg", "source": "owner/repo"}
        with pytest.raises(MarketplaceYmlError, match="remote packages require"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_invalid_tag_pattern(self) -> None:
        entry = _minimal_package_entry() | {"tag_pattern": "release"}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.tag_pattern"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_requires_boolean_include_prerelease(self) -> None:
        entry = _minimal_package_entry() | {"include_prerelease": "yes"}
        with pytest.raises(MarketplaceYmlError, match="include_prerelease"):
            _parse_package_entry(entry, 0)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("description", "   "),
            ("homepage", "   "),
            ("license", "   "),
            ("repository", "   "),
            ("category", "   "),
        ],
    )
    def test_parse_package_entry_rejects_empty_string_fields(self, field: str, value: str) -> None:
        entry = _minimal_package_entry() | {field: value}
        with pytest.raises(MarketplaceYmlError, match=field):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_non_list_tags(self) -> None:
        entry = _minimal_package_entry() | {"tags": "tag"}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.tags"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_non_string_tag_member(self) -> None:
        entry = _minimal_package_entry() | {"tags": ["ok", 1]}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.tags\[1\]"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_non_list_keywords(self) -> None:
        entry = _minimal_package_entry() | {"keywords": "tag"}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.keywords"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_rejects_non_string_keyword_member(self) -> None:
        entry = _minimal_package_entry() | {"keywords": ["ok", 1]}
        with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.keywords\[1\]"):
            _parse_package_entry(entry, 0)

    def test_parse_package_entry_parses_author_object(self) -> None:
        entry = _minimal_package_entry() | {"author": {"name": "Jane", "email": "jane@example.com"}}
        parsed = _parse_package_entry(entry, 0)
        assert parsed.author == {"name": "Jane", "email": "jane@example.com"}

    def test_parse_package_entry_merges_keywords_and_tags(self) -> None:
        entry = _minimal_package_entry() | {"tags": ["alpha"], "keywords": ["alpha", "beta"]}
        parsed = _parse_package_entry(entry, 0)
        assert parsed.tags == ("alpha", "beta")

    def test_parse_package_entry_truncates_tags_and_logs_warning(self) -> None:
        logger = MagicMock()
        entry = _minimal_package_entry() | {"tags": [f"tag-{idx}" for idx in range(60)]}
        with patch("logging.getLogger", return_value=logger):
            parsed = _parse_package_entry(entry, 0)
        assert len(parsed.tags) == 50
        logger.warning.assert_called_once()

    def test_parse_package_entry_truncates_overlong_tag_values(self) -> None:
        long_tag = "x" * 150
        entry = _minimal_package_entry() | {"tags": [long_tag]}
        parsed = _parse_package_entry(entry, 0)
        assert len(parsed.tags[0]) == 100


class TestReadYamlMapping:
    def test_read_yaml_mapping_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="Cannot read"):
            _read_yaml_mapping(tmp_path / "missing.yml")

    def test_read_yaml_mapping_rejects_yaml_error_with_mark(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "broken.yml", "name: [unterminated\n")
        with pytest.raises(MarketplaceYmlError, match=r"line 1, column 7"):
            _read_yaml_mapping(path)

    def test_read_yaml_mapping_returns_empty_mapping_for_empty_file(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "empty.yml", "")
        assert _read_yaml_mapping(path) == {}

    def test_read_yaml_mapping_rejects_non_mapping_yaml(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "list.yml", "- one\n- two\n")
        with pytest.raises(MarketplaceYmlError, match="YAML mapping"):
            _read_yaml_mapping(path)


class TestLegacyLoader:
    def test_load_marketplace_from_legacy_yml_read_error(self, tmp_path: Path) -> None:
        path = tmp_path / "marketplace.yml"
        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            with pytest.raises(MarketplaceYmlError, match="Cannot read"):
                load_marketplace_from_legacy_yml(path)

    def test_load_marketplace_from_legacy_yml_yaml_error(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "marketplace.yml", "name: [unterminated\n")
        with pytest.raises(MarketplaceYmlError, match="YAML parse error"):
            load_marketplace_from_legacy_yml(path)

    def test_load_marketplace_from_legacy_yml_rejects_unknown_top_level_keys(
        self, tmp_path: Path
    ) -> None:
        path = _write_yaml(
            tmp_path / "marketplace.yml",
            "name: test-marketplace\ndescription: Test marketplace\nversion: 1.2.3\nowner:\n  name: Jane Doe\npackages: []\nextra: true\n",
        )
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in top level"):
            load_marketplace_from_legacy_yml(path)

    def test_load_marketplace_from_legacy_yml_requires_required_fields(
        self, tmp_path: Path
    ) -> None:
        path = _write_yaml(
            tmp_path / "marketplace.yml",
            "description: desc\nversion: 1.0.0\nowner:\n  name: Jane\npackages: []\n",
        )
        with pytest.raises(MarketplaceYmlError, match="'name' is required"):
            load_marketplace_from_legacy_yml(path)

    def test_load_marketplace_from_legacy_yml_valid_file(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "marketplace.yml", _legacy_yaml())
        cfg = load_marketplace_from_legacy_yml(path)
        assert cfg.name == "test-marketplace"
        assert cfg.description == "Test marketplace"
        assert cfg.version == "1.2.3"
        assert cfg.is_legacy is True
        assert cfg.source_path == path


class TestApmLoader:
    def test_load_marketplace_from_apm_yml_requires_marketplace_block(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            "name: pkg\ndescription: desc\nversion: 1.0.0\n",
        )
        with pytest.raises(MarketplaceYmlError, match="has no 'marketplace:' block"):
            load_marketplace_from_apm_yml(path)

    def test_load_marketplace_from_apm_yml_requires_mapping_block(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            "name: pkg\ndescription: desc\nversion: 1.0.0\nmarketplace: []\n",
        )
        with pytest.raises(MarketplaceYmlError, match="must be a mapping"):
            load_marketplace_from_apm_yml(path)

    def test_load_marketplace_from_apm_yml_rejects_invalid_marketplace_keys(
        self, tmp_path: Path
    ) -> None:
        path = _write_yaml(tmp_path / "apm.yml", _apm_yaml("owner:\n  name: Jane\nextra: true\n"))
        with pytest.raises(MarketplaceYmlError, match=r"Unknown key\(s\) in marketplace"):
            load_marketplace_from_apm_yml(path)

    def test_load_marketplace_from_apm_yml_inherits_top_level_values(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "apm.yml", _apm_yaml("owner:\n  name: Jane\npackages: []\n"))
        cfg = load_marketplace_from_apm_yml(path)
        assert cfg.name == "top-level-name"
        assert cfg.description == "Top level description"
        assert cfg.version == "2.3.4"
        assert cfg.name_overridden is False
        assert cfg.description_overridden is False
        assert cfg.version_overridden is False

    def test_load_marketplace_from_apm_yml_applies_overrides(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            _apm_yaml(
                """
                name: override-name
                description: Override description
                version: 9.9.9
                owner:
                  name: Jane
                packages: []
                """
            ),
        )
        cfg = load_marketplace_from_apm_yml(path)
        assert cfg.name == "override-name"
        assert cfg.description == "Override description"
        assert cfg.version == "9.9.9"
        assert cfg.name_overridden is True
        assert cfg.description_overridden is True
        assert cfg.version_overridden is True

    def test_load_marketplace_from_apm_yml_allows_missing_version(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            _apm_yaml(
                "owner:\n  name: Jane\npackages: []\n",
                top_level="version:\n",
            ),
        )
        cfg = load_marketplace_from_apm_yml(path)
        assert cfg.version == ""

    def test_load_marketplace_from_apm_yml_requires_name_from_top_level_or_override(
        self, tmp_path: Path
    ) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            "description: desc\nversion: 1.0.0\nmarketplace:\n  owner:\n    name: Jane\n",
        )
        with pytest.raises(MarketplaceYmlError, match="'name' is required"):
            load_marketplace_from_apm_yml(path)


class TestBuildConfig:
    def test_build_config_requires_owner(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="'owner' is required"):
            _build_config(
                marketplace_dict={},
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_requires_non_empty_output(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="'output' must be a non-empty string"):
            _build_config(
                marketplace_dict={"owner": {"name": "Jane"}, "output": "   "},
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_rejects_output_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="traversal"):
            _build_config(
                marketplace_dict={"owner": {"name": "Jane"}, "output": "../escape.json"},
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_requires_metadata_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="'metadata' must be a mapping"):
            _build_config(
                marketplace_dict={"owner": {"name": "Jane"}, "metadata": "bad"},
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_rejects_plugin_root_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match=r"metadata\.pluginRoot"):
            _build_config(
                marketplace_dict={
                    "owner": {"name": "Jane"},
                    "metadata": {"pluginRoot": "../outside"},
                },
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_requires_packages_list(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="'packages' must be a list"):
            _build_config(
                marketplace_dict={"owner": {"name": "Jane"}, "packages": "bad"},
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_rejects_duplicate_package_names(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="Duplicate package name 'pkg'"):
            _build_config(
                marketplace_dict={
                    "owner": {"name": "Jane"},
                    "packages": [
                        {"name": "Pkg", "source": "owner/one", "ref": "main"},
                        {"name": "pkg", "source": "owner/two", "ref": "main"},
                    ],
                },
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_requires_category_for_codex_output(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="packages must define 'category'"):
            _build_config(
                marketplace_dict={
                    "owner": {"name": "Jane"},
                    "outputs": {"codex": None},
                    "packages": [{"name": "pkg", "source": "owner/repo", "ref": "main"}],
                },
                name="name",
                description="desc",
                version="1.0.0",
                source_path=tmp_path / "apm.yml",
                is_legacy=False,
                name_overridden=False,
                description_overridden=False,
                version_overridden=False,
            )

    def test_build_config_prefers_sibling_output_over_outputs_map(self, tmp_path: Path) -> None:
        cfg = _build_config(
            marketplace_dict={
                "owner": {"name": "Jane"},
                "outputs": {"claude": {"path": "dist/from-map.json"}},
                "claude": {"output": "dist/from-sibling.json"},
            },
            name="name",
            description="desc",
            version="1.0.0",
            source_path=tmp_path / "apm.yml",
            is_legacy=False,
            name_overridden=False,
            description_overridden=False,
            version_overridden=False,
        )
        assert cfg.output == "dist/from-sibling.json"
        assert cfg.output_specs[0].path == "dist/from-sibling.json"
        assert cfg.warnings
        assert "conflicts with marketplace.claude.output" in cfg.warnings[0]
