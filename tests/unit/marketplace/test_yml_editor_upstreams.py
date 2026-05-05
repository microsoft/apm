"""Tests for the upstream editor helpers in ``yml_editor``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.yml_editor import (
    add_plugin_entry,
    add_upstream_entry,
    list_upstream_entries,
    remove_upstream_entry,
)

SHA40 = "b" * 40


def _write_yml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "marketplace.yml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_BASE_YML = """\
name: acme-marketplace
description: ACME curated marketplace
version: 1.0.0
owner:
  name: ACME Corp
packages: []
"""


# ---------------------------------------------------------------------------
# add_upstream_entry
# ---------------------------------------------------------------------------


class TestAddUpstreamHappy:
    def test_add_with_ref_creates_block(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(
            yml,
            alias="gitnexus",
            repo="abhigyanpatwari/GitNexus",
            ref=SHA40,
        )
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        assert "upstreams" in data
        assert len(data["upstreams"]) == 1
        entry = data["upstreams"][0]
        assert entry["alias"] == "gitnexus"
        assert entry["repo"] == "abhigyanpatwari/GitNexus"
        assert entry["ref"] == SHA40

    def test_add_with_branch_and_allow_head(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(
            yml,
            alias="gitnexus",
            repo="abhigyanpatwari/GitNexus",
            branch="main",
            allow_head=True,
        )
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["upstreams"][0]
        assert entry["branch"] == "main"
        assert entry["allow_head"] is True

    def test_add_with_optional_path_and_host(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(
            yml,
            alias="gitlab-mirror",
            repo="example/repo",
            ref=SHA40,
            path=".claude-plugin/marketplace.json",
            host="gitlab.com",
        )
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        entry = data["upstreams"][0]
        assert entry["path"] == ".claude-plugin/marketplace.json"
        assert entry["host"] == "gitlab.com"

    def test_add_appends_when_block_exists(self, tmp_path: Path) -> None:
        existing = textwrap.dedent("""\
            name: acme-marketplace
            description: ACME
            version: 1.0.0
            owner:
              name: ACME Corp
            upstreams:
              - alias: first
                repo: acme/first
                ref: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
            packages: []
            """)
        yml = _write_yml(tmp_path, existing)
        add_upstream_entry(
            yml,
            alias="second",
            repo="acme/second",
            ref=SHA40,
        )
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        aliases = [u["alias"] for u in data["upstreams"]]
        assert aliases == ["first", "second"]


class TestAddUpstreamErrors:
    def test_invalid_alias_rejected(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        # B5: the regex is ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$``.
        # Aliases starting with '-' are still invalid; digit-leading ones are now valid.
        with pytest.raises(MarketplaceYmlError, match="Upstream alias"):
            add_upstream_entry(yml, alias="-bad-alias", repo="a/b", ref=SHA40)

    def test_digit_leading_alias_accepted(self, tmp_path: Path) -> None:
        # B5: digit-leading aliases are now valid (schema and editor aligned).
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(yml, alias="2fa-marketplace", repo="a/b", ref=SHA40)
        data = yaml.safe_load(yml.read_text())
        assert data["upstreams"][0]["alias"] == "2fa-marketplace"

    def test_invalid_repo_rejected(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        with pytest.raises(MarketplaceYmlError, match="repo"):
            add_upstream_entry(yml, alias="ok", repo="not-a-repo", ref=SHA40)

    def test_missing_ref_and_branch_rejected(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        with pytest.raises(MarketplaceYmlError, match=r"ref.*branch"):
            add_upstream_entry(yml, alias="ok", repo="a/b")

    def test_duplicate_alias_rejected(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(yml, alias="dup", repo="a/b", ref=SHA40)
        with pytest.raises(MarketplaceYmlError, match=r"already exists"):
            add_upstream_entry(yml, alias="dup", repo="a/b", ref=SHA40)


# ---------------------------------------------------------------------------
# remove_upstream_entry
# ---------------------------------------------------------------------------


class TestRemoveUpstream:
    def test_remove_existing_alias(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(yml, alias="gitnexus", repo="a/b", ref=SHA40)
        remove_upstream_entry(yml, "gitnexus")
        data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        # Block may exist as empty list or be omitted; both acceptable.
        assert not data.get("upstreams")

    def test_remove_unknown_alias_errors(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        with pytest.raises(MarketplaceYmlError, match="not found"):
            remove_upstream_entry(yml, "ghost")

    def test_remove_blocked_when_referenced(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(yml, alias="gitnexus", repo="a/b", ref=SHA40)

        # Manually inject a package that references the upstream.
        text = yml.read_text(encoding="utf-8")
        text = text.replace(
            "packages: []",
            textwrap.dedent("""\
                packages:
                  - name: acme-gitnexus
                    upstream: gitnexus
                    plugin: gitnexus
                """),
        )
        yml.write_text(text, encoding="utf-8")

        with pytest.raises(MarketplaceYmlError, match=r"still referenced"):
            remove_upstream_entry(yml, "gitnexus")


# ---------------------------------------------------------------------------
# list_upstream_entries
# ---------------------------------------------------------------------------


class TestListUpstreams:
    def test_list_empty_when_no_block(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        assert list_upstream_entries(yml) == []

    def test_list_returns_plain_dicts(self, tmp_path: Path) -> None:
        yml = _write_yml(tmp_path, _BASE_YML)
        add_upstream_entry(yml, alias="a", repo="x/y", ref=SHA40)
        add_upstream_entry(yml, alias="b", repo="x/z", branch="main", allow_head=True)
        entries = list_upstream_entries(yml)
        assert len(entries) == 2
        aliases = [e["alias"] for e in entries]
        assert aliases == ["a", "b"]
        # Plain dicts (not CommentedMap).
        assert isinstance(entries[0], dict)


# ---------------------------------------------------------------------------
# Roundtrip preserves comments
# ---------------------------------------------------------------------------


def test_add_upstream_preserves_existing_comments(tmp_path: Path) -> None:
    """Round-trip mode preserves leading and trailing comments around edits."""
    yml = _write_yml(
        tmp_path,
        textwrap.dedent("""\
            # Top-level marketplace
            name: acme-marketplace
            description: ACME
            version: 1.0.0
            owner:
              name: ACME Corp
            # End of file
            """),
    )
    add_upstream_entry(yml, alias="gitnexus", repo="a/b", ref=SHA40)
    text = yml.read_text(encoding="utf-8")
    assert "# Top-level marketplace" in text
    assert "# End of file" in text
    assert "alias: gitnexus" in text


# ---------------------------------------------------------------------------
# Integration: package add with upstream coexists with direct entries
# ---------------------------------------------------------------------------


def test_upstream_block_does_not_break_direct_package_add(tmp_path: Path) -> None:
    yml = _write_yml(tmp_path, _BASE_YML)
    add_upstream_entry(yml, alias="gitnexus", repo="a/b", ref=SHA40)
    add_plugin_entry(yml, source="acme/direct", version=">=1.0.0")
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert len(data["upstreams"]) == 1
    assert len(data["packages"]) == 1
    assert data["packages"][0]["name"] == "direct"
