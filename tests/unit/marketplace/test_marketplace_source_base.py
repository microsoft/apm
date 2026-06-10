from __future__ import annotations

import textwrap
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.marketplace.yml_editor import add_plugin_entry
from apm_cli.marketplace.yml_schema import MarketplaceConfig

_SHA = "a" * 40


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def _apm_yml(source_base: str | None, packages: str) -> str:
    lines = [
        "name: source-base-marketplace",
        "description: Source base marketplace",
        "version: 1.0.0",
        "marketplace:",
        "  owner:",
        "    name: ACME",
    ]
    if source_base is not None:
        lines.append(f"  sourceBase: {source_base}")
    lines.append("  packages:")
    lines.append(textwrap.indent(textwrap.dedent(packages).strip(), "    "))
    return "\n".join(lines) + "\n"


def _load_config(tmp_path: Path, source_base: str | None, packages: str) -> MarketplaceConfig:
    _write(tmp_path / "apm.yml", _apm_yml(source_base, packages))
    return load_marketplace_config(tmp_path)


class TestSourceBaseSchema:
    def test_accepts_single_and_nested_relative_sources_when_source_base_is_set(
        self, tmp_path: Path
    ) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces/",
            f"""
            - name: single
              source: single-tool
              ref: {_SHA}
            - name: nested
              source: team/tools/nested-tool
              ref: {_SHA}
            """,
        )

        assert config.source_base == "https://gitlab.example.com/platform/marketplaces"
        assert config.packages[0].source == "single-tool"
        assert config.packages[0].host is None
        assert config.packages[1].source == "team/tools/nested-tool"
        assert config.packages[1].host is None

    def test_absent_source_base_keeps_owner_repo_source_unchanged(self, tmp_path: Path) -> None:
        config = _load_config(
            tmp_path,
            None,
            f"""
            - name: existing
              source: owner/repo
              ref: {_SHA}
            """,
        )

        assert config.source_base is None
        assert config.packages[0].source == "owner/repo"
        assert config.packages[0].host is None

    @pytest.mark.parametrize(
        ("source_base", "message"),
        [
            ("http://gitlab.example.com/group", "https"),
            ("https://user@gitlab.example.com/group", "userinfo"),
            ("https://gitlab.example.com:443/group", "port"),
            ("https://gitlab.example.com/group?token=x", "query"),
            ("https://gitlab.example.com/group#frag", "fragment"),
            ("https://gitlab.example.com/group.git", r"\.git"),
            ("https://localhost/group", "FQDN"),
            ("https://gitlab.example.com/group//repo", "empty"),
            ("https://gitlab.example.com/group//", "empty"),
            ("https://gitlab.example.com/group/../repo", "traversal"),
        ],
    )
    def test_rejects_source_base_security_guard_violations(
        self, tmp_path: Path, source_base: str, message: str
    ) -> None:
        with pytest.raises(MarketplaceYmlError, match=message):
            _load_config(
                tmp_path,
                source_base,
                f"""
                - name: tool
                  source: tool
                  ref: {_SHA}
                """,
            )

    def test_rejects_single_segment_source_without_source_base(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be one of"):
            _load_config(
                tmp_path,
                None,
                f"""
                - name: tool
                  source: tool
                  ref: {_SHA}
                """,
            )


class TestSourceBaseBuildComposition:
    def test_composes_relative_source_onto_base_for_resolution_and_output(
        self, tmp_path: Path
    ) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            f"""
            - name: tool
              source: team/tool
              ref: {_SHA}
            """,
        )
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))

        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "platform/marketplaces/team/tool"
        assert resolved.host == "gitlab.example.com"
        parsed = urlparse(resolved.source_url or "")
        assert parsed.scheme == "https"
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

        doc = builder.compose_marketplace_json([resolved])
        source = doc["plugins"][0]["source"]
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

    def test_host_prefixed_source_overrides_source_base(self, tmp_path: Path) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            f"""
            - name: override
              source: ghe.example.com/acme/tool
              ref: {_SHA}
            """,
        )
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))

        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "acme/tool"
        assert resolved.host == "ghe.example.com"
        assert resolved.source_url is None

        doc = builder.compose_marketplace_json([resolved])
        source = doc["plugins"][0]["source"]
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "ghe.example.com"
        assert parsed.path == "/acme/tool"


class TestSourceBaseEditor:
    def test_add_plugin_entry_accepts_relative_source_when_source_base_is_set(
        self, tmp_path: Path
    ) -> None:
        yml_path = _write(
            tmp_path / "apm.yml",
            _apm_yml(
                "https://gitlab.example.com/platform/marketplaces",
                f"""
                - name: existing
                  source: existing-tool
                  ref: {_SHA}
                """,
            ),
        )

        name = add_plugin_entry(yml_path, source="new-tool", ref=_SHA)

        assert name == "new-tool"
        config = load_marketplace_config(tmp_path)
        added = next(pkg for pkg in config.packages if pkg.name == "new-tool")
        assert added.source == "new-tool"
        assert added.host is None
