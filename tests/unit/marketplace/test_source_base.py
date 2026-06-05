"""Tests for the marketplace ``sourceBase`` field (issue #1519).

``sourceBase`` declares a git base that host-less relative package sources
compose onto, enabling deeply nested enterprise GitLab group paths that the
3-segment ``host.tld/owner/repo`` shorthand cannot express. Per-entry
host-prefixed and full-URL sources act as overrides (the base is ignored);
local ``./`` sources are untouched; and when no base is declared, behavior is
byte-for-byte identical to before.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.output_mappers import ClaudeMarketplaceMapper
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

# The canonical repro from the issue: a GitLab nested-group base.
_BASE = "https://gitlab.example.com/group/sub-group/team/projects"


def _write_apm(tmp_path: Path, *, source_base: str | None, packages: str) -> Path:
    """Write an apm.yml with a marketplace block and return its path.

    *packages* is a raw YAML fragment of one or more ``- {...}`` entries; each
    line is indented to sit under ``packages:``. *source_base*, when given, is
    emitted as a quoted ``sourceBase:`` line.
    """
    lines = [
        "name: my-project",
        "description: Project description.",
        "version: 1.2.3",
        "marketplace:",
        "  owner:",
        "    name: ACME",
    ]
    if source_base is not None:
        lines.append(f'  sourceBase: "{source_base}"')
    lines.append("  packages:")
    for pkg_line in textwrap.dedent(packages).strip().splitlines():
        lines.append("    " + pkg_line)
    apm = tmp_path / "apm.yml"
    apm.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return apm


def _entry(cfg, name):
    return next(e for e in cfg.packages if e.name == name)


# ---------------------------------------------------------------------------
# sourceBase field validation
# ---------------------------------------------------------------------------


class TestSourceBaseValidation:
    def test_valid_deep_base_accepted_and_stored(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: p, source: p, version: '^1.0.0'}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        assert cfg.source_base == _BASE

    def test_trailing_slash_normalized(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE + "/",
            packages="- {name: p, source: p, version: '^1.0.0'}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        assert cfg.source_base == _BASE  # no trailing slash

    @pytest.mark.parametrize(
        "bad_base",
        [
            "http://gitlab.example.com/group",  # non-https scheme
            "https://user@gitlab.example.com/group",  # userinfo
            "https://gitlab.example.com:443/group",  # port
            "https://gitlab.example.com/group?ref=main",  # query
            "https://gitlab.example.com/group#frag",  # fragment
            "https://gitlab.example.com/group/repo.git",  # .git suffix
            "https://localhost/group",  # non-FQDN host (no dot)
            "https://gitlab.example.com",  # host only, no path
            "git@gitlab.example.com:group/repo",  # SCP/ssh form
        ],
    )
    def test_unsafe_base_forms_rejected(self, tmp_path: Path, bad_base: str) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=bad_base,
            packages="- {name: p, source: owner/repo, version: '^1.0.0'}",
        )
        with pytest.raises(MarketplaceYmlError, match="sourceBase"):
            load_marketplace_from_apm_yml(apm)

    def test_path_traversal_in_base_rejected(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base="https://gitlab.example.com/group/../../etc",
            packages="- {name: p, source: owner/repo, version: '^1.0.0'}",
        )
        with pytest.raises(MarketplaceYmlError, match=r"traversal|sourceBase"):
            load_marketplace_from_apm_yml(apm)

    def test_empty_base_rejected(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base="   ",
            packages="- {name: p, source: owner/repo, version: '^1.0.0'}",
        )
        with pytest.raises(MarketplaceYmlError, match="sourceBase"):
            load_marketplace_from_apm_yml(apm)


# ---------------------------------------------------------------------------
# Relative-source composition onto the base
# ---------------------------------------------------------------------------


class TestRelativeComposition:
    def test_single_segment_relative_composes(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: my-package, source: my-package, version: '^1.0.0'}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        e = _entry(cfg, "my-package")
        assert e.host == "gitlab.example.com"
        assert e.source == "group/sub-group/team/projects/my-package"

    def test_issue_repro_exact(self, tmp_path: Path) -> None:
        """The exact 4+ segment GitLab path from #1519 now resolves."""
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: my-package, source: my-package, ref: v1.2.0}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        e = _entry(cfg, "my-package")
        # Composed shape is identical to a host-prefixed entry pointing at the
        # full nested path -- the URL a real git ls-remote / clone will use.
        assert e.host == "gitlab.example.com"
        assert e.source == "group/sub-group/team/projects/my-package"

    def test_owner_repo_relative_composes(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: t, source: owner/repo, version: '^1.0.0'}",
        )
        e = _entry(load_marketplace_from_apm_yml(apm), "t")
        assert e.host == "gitlab.example.com"
        assert e.source == "group/sub-group/team/projects/owner/repo"

    def test_n_segment_relative_composes(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: t, source: a/b/c, ref: main}",
        )
        e = _entry(load_marketplace_from_apm_yml(apm), "t")
        assert e.host == "gitlab.example.com"
        assert e.source == "group/sub-group/team/projects/a/b/c"


# ---------------------------------------------------------------------------
# Per-entry overrides (base ignored) and local sources
# ---------------------------------------------------------------------------


class TestOverridePrecedence:
    def test_host_prefixed_source_overrides_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: t, source: github.com/owner/repo, version: '^1.0.0'}",
        )
        e = _entry(load_marketplace_from_apm_yml(apm), "t")
        assert e.host == "github.com"
        assert e.source == "owner/repo"  # base NOT prepended

    def test_full_url_source_overrides_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages='- {name: t, source: "https://other.example.com/x/y.git", ref: v1}',
        )
        e = _entry(load_marketplace_from_apm_yml(apm), "t")
        assert e.host == "other.example.com"
        assert e.source == "x/y"  # base NOT prepended, .git normalized

    def test_local_source_untouched_by_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: t, source: ./local-pkg}",
        )
        e = _entry(load_marketplace_from_apm_yml(apm), "t")
        assert e.is_local is True
        assert e.host is None
        assert e.source == "./local-pkg"  # base ignored for local


# ---------------------------------------------------------------------------
# No base declared -> behavior identical to today (fail-closed)
# ---------------------------------------------------------------------------


class TestNoBaseUnchanged:
    def test_single_segment_rejected_without_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=None,
            packages="- {name: t, source: my-package, version: '^1.0.0'}",
        )
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_from_apm_yml(apm)

    def test_four_segment_rejected_without_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=None,
            packages="- {name: t, source: a/b/c/d, version: '^1.0.0'}",
        )
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_from_apm_yml(apm)

    def test_owner_repo_unchanged_without_base(self, tmp_path: Path) -> None:
        apm = _write_apm(
            tmp_path,
            source_base=None,
            packages="- {name: t, source: owner/repo, version: '^1.0.0'}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        assert cfg.source_base is None
        e = _entry(cfg, "t")
        assert e.host is None
        assert e.source == "owner/repo"  # no composition


# ---------------------------------------------------------------------------
# Phase 3 contract: the composed entry emits the right marketplace.json URL
# (verifies the output mapper needs no change -- a composed relative entry is
# byte-identical to a host-prefixed entry by emit time).
# ---------------------------------------------------------------------------


class TestComposedUrlEmission:
    def test_claude_mapper_emits_composed_gitlab_url(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.builder import ResolvedPackage

        apm = _write_apm(
            tmp_path,
            source_base=_BASE,
            packages="- {name: my-package, source: my-package, ref: v1.2.0}",
        )
        cfg = load_marketplace_from_apm_yml(apm)
        e = _entry(cfg, "my-package")
        # Mirror what builder._resolve_entry produces from the parsed entry.
        resolved = ResolvedPackage(
            name=e.name,
            source_repo=e.source,
            subdir=e.subdir,
            ref="v1.2.0",
            sha="0" * 40,
            requested_version=e.version,
            tags=e.tags,
            is_prerelease=False,
            host=e.host,
        )
        result = ClaudeMarketplaceMapper().compose(config=cfg, resolved=(resolved,))
        source = result.document["plugins"][0]["source"]
        assert source["source"] == "url"
        assert source["url"] == (
            "https://gitlab.example.com/group/sub-group/team/projects/my-package"
        )
        assert source["ref"] == "v1.2.0"
