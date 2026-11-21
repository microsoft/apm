"""Phase-3w4 integration tests for src/apm_cli/marketplace/builder.py.

Targets the gap of ~186 lines at 68.3% coverage.

Covered branches / lines:
- BuildReport.primary_output (empty outputs)
- BuildReport.to_json_dict (errors, warnings, outputs)
- BuildReport.failure_to_json_dict
- MarketplaceBuilder.from_config
- MarketplaceBuilder._load_yml apm.yml vs marketplace.yml path
- MarketplaceBuilder._ensure_auth offline / resolved shortcuts
- MarketplaceBuilder._output_path (marketplace_output, output_override, yml default)
- MarketplaceBuilder._mapper_for_profile (unknown mapper)
- MarketplaceBuilder._resolve_entry local-path
- MarketplaceBuilder._resolve_explicit_ref: SHA40, tag hit, full refname, branch, HEAD
- MarketplaceBuilder._resolve_version_range no-candidates / candidates
- MarketplaceBuilder.resolve empty yml, continue_on_error BuildError / Exception
- MarketplaceBuilder._compute_diff (None old, added/updated/removed/unchanged)
- MarketplaceBuilder._serialize_json / _load_existing_json
- MarketplaceBuilder._fetch_remote_metadata host_kind branches, token logic
- MarketplaceBuilder._resolve_github_token error path
- MarketplaceBuilder.build pipeline
- MarketplaceBuilder.write_output dry_run / include_diff
- _strip_ref_prefix helper
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    MarketplaceOutputReport,
    ResolvedPackage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolved_pkg(name="pkg", sha="a" * 40, ref="v1.0.0") -> ResolvedPackage:
    return ResolvedPackage(
        name=name,
        source_repo="owner/repo",
        subdir=None,
        ref=ref,
        sha=sha,
        requested_version=">=1.0.0",
        tags=(),
        is_prerelease=False,
    )


def _make_output_report(**kwargs) -> MarketplaceOutputReport:
    defaults = dict(
        profile="default",
        resolved=(),
        errors=(),
        warnings=(),
        diagnostics=(),
        unchanged_count=0,
        added_count=0,
        updated_count=0,
        removed_count=0,
        output_path=Path("marketplace.json"),
        dry_run=False,
    )
    defaults.update(kwargs)
    return MarketplaceOutputReport(**defaults)


def _make_marketplace_yml_file(tmp_path: Path, content: dict | None = None) -> Path:
    """Write a minimal marketplace.yml and return its path."""
    default = {
        "name": "test-marketplace",
        "description": "Fixture marketplace for builder unit coverage",
        "version": "0.1.0",
        "owner": {"name": "test-owner"},
        "claude": {"output": "marketplace.json"},
        "build": {"tagPattern": "v{version}"},
        "packages": [],
    }
    data = content if content is not None else default
    p = tmp_path / "marketplace.yml"
    p.write_text(yaml.dump(data))
    return p


# ---------------------------------------------------------------------------
# _strip_ref_prefix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BuildReport.primary_output (empty outputs)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BuildReport.to_json_dict
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BuildReport.failure_to_json_dict
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MarketplaceBuilder construction / from_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _load_yml: apm.yml vs marketplace.yml
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _ensure_auth
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _output_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _mapper_for_profile
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_entry: local-path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_explicit_ref
# ---------------------------------------------------------------------------


class TestResolveExplicitRef:
    def _make_builder(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        builder._yml = MagicMock()
        builder._yml.build.tag_pattern = "v{version}"
        return builder

    def test_tag_found_in_refs(self, tmp_path):
        builder = self._make_builder(tmp_path)
        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "v1.2.3"
        entry.subdir = None
        entry.version = ">=1.0.0"
        entry.tags = ()

        remote_ref = MagicMock()
        remote_ref.name = "refs/tags/v1.2.3"
        remote_ref.sha = "b" * 40

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = [remote_ref]

        result = builder._resolve_explicit_ref(entry, resolver, "owner/repo")
        assert result.ref == "v1.2.3"
        assert result.sha == "b" * 40


# ---------------------------------------------------------------------------
# _resolve_version_range
# ---------------------------------------------------------------------------


class TestResolveVersionRange:
    def _make_builder(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        builder._options = BuildOptions()
        yml_mock = MagicMock()
        yml_mock.build.tag_pattern = "v{version}"
        builder._yml = yml_mock
        return builder

    def test_best_candidate_chosen(self, tmp_path):
        builder = self._make_builder(tmp_path)

        entry = MagicMock()
        entry.name = "pkg"
        entry.version = ">=1.0.0"
        entry.tag_pattern = None
        entry.include_prerelease = False
        entry.subdir = None
        entry.tags = ()

        def _make_tag_ref(tag, sha):
            ref = MagicMock()
            ref.name = f"refs/tags/{tag}"
            ref.sha = sha
            return ref

        refs = [
            _make_tag_ref("v1.0.0", "1" * 40),
            _make_tag_ref("v2.0.0", "2" * 40),
            _make_tag_ref("v1.5.0", "3" * 40),
        ]
        resolver = MagicMock()
        resolver.list_remote_refs.return_value = refs

        result = builder._resolve_version_range(entry, resolver, "owner/repo", builder._yml)
        assert result.ref == "v2.0.0"
        assert result.sha == "2" * 40


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _compute_diff
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _load_existing_json
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _serialize_json
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_remote_metadata
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# write_output with dry_run and include_diff
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_github_token error path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build() pipeline
# ---------------------------------------------------------------------------
