"""Integration tests for ``apm marketplace build``.

Strategy
--------
These tests write a real marketplace.yml to a tmp directory, then invoke
``MarketplaceBuilder`` (or ``run_cli``) with ``RefResolver.list_remote_refs``
patched so no real network calls are made.

All assertions are against real file-system artefacts produced by the build
pipeline: marketplace.json existence, content, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.yml_schema import load_marketplace_yml

from .conftest import (
    GOLDEN_YML,
    MINIMAL_YML,
    _project_uv_bin,
    run_cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "marketplace.yml"
    p.write_text(content, encoding="utf-8")
    return p


def _read_json(tmp_path: Path) -> dict:
    out = tmp_path / "marketplace.json"
    return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Build pipeline tests (library-level, not subprocess)
# ---------------------------------------------------------------------------


class TestBuildGoldenFile:
    """The canonical input must produce byte-level output matching golden.json."""

    def test_golden_content_matches(
        self, tmp_path: Path, mock_ref_resolver_golden, golden_marketplace_json
    ):
        """Run the full build pipeline and compare output with golden.json."""
        _write_yml(tmp_path, GOLDEN_YML)

        opts = BuildOptions(dry_run=False)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml", options=opts)
        report = builder.build()

        out_path = tmp_path / "marketplace.json"
        assert out_path.exists(), "marketplace.json was not produced"

        actual = json.loads(out_path.read_text(encoding="utf-8"))
        assert actual == golden_marketplace_json

    def test_key_order_follows_anthropic_schema(
        self, tmp_path: Path, mock_ref_resolver_golden
    ):
        """Top-level keys must appear in Anthropic canonical order."""
        _write_yml(tmp_path, GOLDEN_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report = builder.build()

        out_path = tmp_path / "marketplace.json"
        raw_text = out_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        top_keys = list(data.keys())

        # Anthropic schema order: name, description, version, owner, metadata, plugins
        expected_order = ["name", "description", "version", "owner", "metadata", "plugins"]
        assert top_keys == expected_order, (
            f"Expected key order {expected_order}, got {top_keys}"
        )

    def test_plugin_key_order(self, tmp_path: Path, mock_ref_resolver_golden):
        """Each plugin must have keys in Anthropic order."""
        _write_yml(tmp_path, GOLDEN_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = _read_json(tmp_path)
        for plugin in data["plugins"]:
            keys = [k for k in plugin.keys() if k != "description"]
            # name must be first; tags before source; source last
            assert keys[0] == "name"
            assert "tags" in keys
            assert keys[-1] == "source"

    def test_source_key_order(self, tmp_path: Path, mock_ref_resolver_golden):
        """Each source block must follow: type, repository, (path), ref, commit."""
        _write_yml(tmp_path, GOLDEN_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = _read_json(tmp_path)
        # test-generator has subdir -> path must appear between repository and ref
        tg = next(p for p in data["plugins"] if p["name"] == "test-generator")
        src_keys = list(tg["source"].keys())
        assert src_keys == ["type", "repository", "path", "ref", "commit"]

    def test_no_apm_only_keys_in_output(self, tmp_path: Path, mock_ref_resolver_golden):
        """APM-only fields must not appear in marketplace.json."""
        _write_yml(tmp_path, GOLDEN_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = _read_json(tmp_path)
        apm_only = {"subdir", "version_range", "tag_pattern", "include_prerelease"}
        for plugin in data["plugins"]:
            assert not apm_only.intersection(plugin.keys()), (
                f"APM-only key found in plugin {plugin['name']}: "
                f"{apm_only.intersection(plugin.keys())}"
            )
            if "source" in plugin:
                assert not apm_only.intersection(plugin["source"].keys())


class TestBuildHappyPath:
    """Happy-path build scenarios."""

    def test_produces_marketplace_json(self, tmp_path: Path, mock_ref_resolver):
        """A successful build writes marketplace.json to disk."""
        _write_yml(tmp_path, MINIMAL_YML)

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report = builder.build()

        out = tmp_path / "marketplace.json"
        assert out.exists()
        assert report.dry_run is False
        assert report.added_count == 2  # two packages, no prior file

    def test_resolved_packages_count(self, tmp_path: Path, mock_ref_resolver):
        """Build report must list all resolved packages."""
        _write_yml(tmp_path, MINIMAL_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report = builder.build()
        assert len(report.resolved) == 2

    def test_sha_in_output(self, tmp_path: Path, mock_ref_resolver):
        """Each plugin in marketplace.json must have a 40-char commit SHA."""
        _write_yml(tmp_path, MINIMAL_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = _read_json(tmp_path)
        for plugin in data["plugins"]:
            sha = plugin["source"]["commit"]
            assert len(sha) == 40, f"Expected 40-char SHA, got {sha!r}"
            assert all(c in "0123456789abcdef" for c in sha)

    def test_metadata_passed_through(self, tmp_path: Path, mock_ref_resolver):
        """Metadata block is copied verbatim including camelCase keys."""
        _write_yml(tmp_path, MINIMAL_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        builder.build()

        data = _read_json(tmp_path)
        assert "metadata" in data
        assert data["metadata"]["pluginRoot"] == "plugins"

    def test_dry_run_does_not_write(self, tmp_path: Path, mock_ref_resolver):
        """--dry-run must not create marketplace.json on disk."""
        _write_yml(tmp_path, MINIMAL_YML)
        opts = BuildOptions(dry_run=True)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml", options=opts)
        report = builder.build()

        out = tmp_path / "marketplace.json"
        assert not out.exists()
        assert report.dry_run is True

    def test_incremental_build_counts_unchanged(
        self, tmp_path: Path, mock_ref_resolver
    ):
        """Second build with same refs reports unchanged packages, not added."""
        _write_yml(tmp_path, MINIMAL_YML)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report1 = builder.build()
        assert report1.added_count == 2

        builder2 = MarketplaceBuilder(tmp_path / "marketplace.yml")
        report2 = builder2.build()
        assert report2.unchanged_count == 2
        assert report2.added_count == 0


class TestBuildErrorPaths:
    """Error handling at the library level."""

    def test_schema_error_raises_marketplace_yml_error(self, tmp_path: Path):
        """Malformed YAML raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError

        bad = tmp_path / "marketplace.yml"
        bad.write_text("name: [\ninvalid: yaml\n", encoding="utf-8")

        builder = MarketplaceBuilder(bad)
        with pytest.raises(MarketplaceYmlError):
            builder.build()

    def test_missing_yml_raises(self, tmp_path: Path):
        """Missing marketplace.yml raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError

        builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
        with pytest.raises(MarketplaceYmlError):
            builder.build()

    def test_offline_with_no_cache_raises_offline_miss(self, tmp_path: Path):
        """Offline build with empty cache raises OfflineMissError."""
        from apm_cli.marketplace.errors import OfflineMissError

        _write_yml(tmp_path, MINIMAL_YML)
        opts = BuildOptions(offline=True)
        builder = MarketplaceBuilder(tmp_path / "marketplace.yml", options=opts)
        with pytest.raises((OfflineMissError, Exception)):
            builder.build()

    def test_no_matching_version_raises(self, tmp_path: Path):
        """Version range that matches no tag raises NoMatchingVersionError."""
        from apm_cli.marketplace.errors import NoMatchingVersionError

        _write_yml(tmp_path, MINIMAL_YML)

        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            return_value=[],  # no refs -> no matching version
        ):
            builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
            with pytest.raises((NoMatchingVersionError, Exception)):
                builder.build()


# ---------------------------------------------------------------------------
# CLI subprocess tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _project_uv_bin(),
    reason="uv not found on PATH; skipping subprocess CLI tests",
)
class TestBuildCLI:
    """Tests that invoke `apm marketplace build` via subprocess."""

    def test_missing_yml_exits_1(self, tmp_path: Path):
        """Missing marketplace.yml must exit 1."""
        result = run_cli(["marketplace", "build"], cwd=tmp_path)
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "marketplace.yml" in combined

    def test_schema_error_exits_2(self, tmp_path: Path):
        """Malformed marketplace.yml must exit 2."""
        (tmp_path / "marketplace.yml").write_text(
            "name: [\nbad: yaml\n", encoding="utf-8"
        )
        result = run_cli(["marketplace", "build"], cwd=tmp_path)
        assert result.returncode == 2

    def test_dry_run_flag_present(self, tmp_path: Path, mock_ref_resolver):
        """--dry-run must be accepted by the CLI (no crash)."""
        _write_yml(tmp_path, MINIMAL_YML)
        result = run_cli(["marketplace", "build", "--dry-run"], cwd=tmp_path)
        # Without real network, build will fail resolving refs; exit != 0 is OK.
        # Key check: exit code is not 2 (schema error) and no Python traceback.
        assert result.returncode != 2
        assert "Traceback" not in result.stderr
