"""integration tests for src/apm_cli/marketplace/builder.py.

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

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.marketplace.builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    MarketplaceOutputReport,
    ResolvedPackage,
    ResolveResult,
    _strip_ref_prefix,
)
from apm_cli.marketplace.errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    RefNotFoundError,
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
        "claude": {"output": "marketplace.json"},
        "build": {"tag_pattern": "v{version}"},
        "packages": [],
    }
    data = content if content is not None else default
    p = tmp_path / "marketplace.yml"
    p.write_text(yaml.dump(data))
    return p


# ---------------------------------------------------------------------------
# _strip_ref_prefix
# ---------------------------------------------------------------------------


class TestStripRefPrefix:
    def test_strips_tags_prefix(self):
        assert _strip_ref_prefix("refs/tags/v1.2.3") == "v1.2.3"

    def test_strips_heads_prefix(self):
        assert _strip_ref_prefix("refs/heads/main") == "main"

    def test_returns_unchanged_for_other_refs(self):
        assert _strip_ref_prefix("refs/pull/42/head") == "refs/pull/42/head"

    def test_empty_string(self):
        assert _strip_ref_prefix("") == ""


# ---------------------------------------------------------------------------
# BuildReport.primary_output (empty outputs)
# ---------------------------------------------------------------------------


class TestBuildReportEmptyOutputs:
    def test_primary_output_returns_empty_report_when_no_outputs(self):
        report = BuildReport(outputs=())
        primary = report.primary_output
        assert primary.profile == ""
        assert primary.resolved == ()
        assert primary.errors == ()

    def test_dry_run_false_when_no_outputs(self):
        report = BuildReport(outputs=())
        assert report.dry_run is False

    def test_warnings_empty_when_no_outputs(self):
        report = BuildReport(outputs=())
        assert report.warnings == ()

    def test_diagnostics_empty_when_no_outputs(self):
        report = BuildReport(outputs=())
        assert report.diagnostics == ()


# ---------------------------------------------------------------------------
# BuildReport.to_json_dict
# ---------------------------------------------------------------------------


class TestBuildReportToJsonDict:
    def test_ok_true_when_no_errors(self):
        out = _make_output_report()
        report = BuildReport(outputs=(out,))
        d = report.to_json_dict()
        assert d["ok"] is True
        assert d["errors"] == []

    def test_ok_false_when_errors_present(self):
        out = _make_output_report(errors=(("pkg", "not found"),))
        report = BuildReport(outputs=(out,))
        d = report.to_json_dict()
        assert d["ok"] is False
        assert len(d["errors"]) == 1
        assert "pkg: not found" in d["errors"][0]["message"]

    def test_dry_run_propagates(self):
        out = _make_output_report(dry_run=True)
        report = BuildReport(outputs=(out,))
        d = report.to_json_dict()
        assert d["dry_run"] is True

    def test_warnings_from_multiple_outputs(self):
        out1 = _make_output_report(warnings=("warn1",))
        out2 = _make_output_report(warnings=("warn2",))
        report = BuildReport(outputs=(out1, out2))
        d = report.to_json_dict()
        assert "warn1" in d["warnings"]
        assert "warn2" in d["warnings"]

    def test_output_entries_shape(self):
        out = _make_output_report(
            added_count=2, updated_count=1, unchanged_count=3, removed_count=0
        )
        report = BuildReport(outputs=(out,))
        d = report.to_json_dict()
        entry = d["marketplace"]["outputs"][0]
        assert entry["added"] == 2
        assert entry["updated"] == 1
        assert entry["unchanged"] == 3
        assert d["bundle"] is None

    def test_multiple_outputs_multiple_entries(self):
        out1 = _make_output_report(profile="default")
        out2 = _make_output_report(profile="codex", output_path=Path("codex.json"))
        report = BuildReport(outputs=(out1, out2))
        d = report.to_json_dict()
        assert len(d["marketplace"]["outputs"]) == 2


# ---------------------------------------------------------------------------
# BuildReport.failure_to_json_dict
# ---------------------------------------------------------------------------


class TestBuildReportFailureToJsonDict:
    def test_basic_failure_shape(self):
        d = BuildReport.failure_to_json_dict(
            errors=[{"code": "parse_error", "message": "bad yaml"}]
        )
        assert d["ok"] is False
        assert d["dry_run"] is False
        assert d["marketplace"]["outputs"] == []
        assert d["bundle"] is None

    def test_warnings_and_dry_run(self):
        d = BuildReport.failure_to_json_dict(
            errors=[{"code": "x", "message": "y"}],
            warnings=["w1", "w2"],
            dry_run=True,
        )
        assert d["warnings"] == ["w1", "w2"]
        assert d["dry_run"] is True

    def test_warnings_defaults_to_empty_list(self):
        d = BuildReport.failure_to_json_dict(errors=[])
        assert d["warnings"] == []


# ---------------------------------------------------------------------------
# MarketplaceBuilder construction / from_config
# ---------------------------------------------------------------------------


class TestMarketplaceBuilderFromConfig:
    def test_from_config_sets_project_root(self, tmp_path):
        from apm_cli.marketplace.yml_schema import MarketplaceYml

        config = MagicMock(spec=MarketplaceYml)
        config.source_path = None
        builder = MarketplaceBuilder.from_config(config, tmp_path)
        assert builder._project_root == tmp_path

    def test_from_config_stores_yml(self, tmp_path):
        from apm_cli.marketplace.yml_schema import MarketplaceYml

        config = MagicMock(spec=MarketplaceYml)
        config.source_path = Path("marketplace.yml")
        builder = MarketplaceBuilder.from_config(config, tmp_path)
        assert builder._yml is config

    def test_from_config_with_options(self, tmp_path):
        from apm_cli.marketplace.yml_schema import MarketplaceYml

        config = MagicMock(spec=MarketplaceYml)
        config.source_path = None
        opts = BuildOptions(concurrency=2)
        builder = MarketplaceBuilder.from_config(config, tmp_path, options=opts)
        assert builder._options.concurrency == 2


# ---------------------------------------------------------------------------
# _load_yml: apm.yml vs marketplace.yml
# ---------------------------------------------------------------------------


class TestLoadYml:
    def test_loads_apm_yml_when_path_is_apm_yml(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.dump({"name": "test", "version": "1.0.0"}))
        builder = MarketplaceBuilder(apm_yml)

        with (
            patch("apm_cli.marketplace.yml_schema.load_marketplace_from_apm_yml") as mock_load_apm,
            patch("apm_cli.marketplace.builder.load_marketplace_yml") as mock_load_mkt,
        ):
            mock_load_apm.return_value = MagicMock()
            builder._load_yml()
            mock_load_apm.assert_called_once_with(apm_yml)
            mock_load_mkt.assert_not_called()

    def test_loads_marketplace_yml_for_other_names(self, tmp_path):
        mkt_yml = tmp_path / "marketplace.yml"
        mkt_yml.write_text("")
        builder = MarketplaceBuilder(mkt_yml)

        with (
            patch("apm_cli.marketplace.yml_schema.load_marketplace_from_apm_yml") as mock_load_apm,
            patch("apm_cli.marketplace.builder.load_marketplace_yml") as mock_load_mkt,
        ):
            mock_load_mkt.return_value = MagicMock()
            builder._load_yml()
            mock_load_mkt.assert_called_once_with(mkt_yml)
            mock_load_apm.assert_not_called()

    def test_caches_loaded_yml(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("")
        builder = MarketplaceBuilder(apm_yml)
        mock_yml = MagicMock()
        builder._yml = mock_yml
        result = builder._load_yml()
        assert result is mock_yml


# ---------------------------------------------------------------------------
# _ensure_auth
# ---------------------------------------------------------------------------


class TestEnsureAuth:
    def test_skips_when_already_resolved(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        builder._auth_resolved = True
        # Should not call _resolve_github_token
        with patch.object(builder, "_resolve_github_token") as mock_resolve:
            builder._ensure_auth()
            mock_resolve.assert_not_called()

    def test_sets_resolved_in_offline_mode(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml, options=BuildOptions(offline=True))
        builder._ensure_auth()
        assert builder._auth_resolved is True
        assert builder._github_token is None

    def test_resolves_token_when_not_offline(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        with patch.object(builder, "_resolve_github_token", return_value="tok123") as mock_resolve:
            builder._ensure_auth()
            mock_resolve.assert_called_once()
            assert builder._github_token == "tok123"
            assert builder._auth_resolved is True


# ---------------------------------------------------------------------------
# _output_path
# ---------------------------------------------------------------------------


class TestOutputPath:
    def test_output_override_used_when_no_marketplace_output(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        out_path = tmp_path / "override.json"
        builder = MarketplaceBuilder(yml, options=BuildOptions(output_override=out_path))
        assert builder._output_path() == out_path

    def test_falls_back_to_yml_default(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        mock_yml = MagicMock()
        mock_yml.claude.output = "marketplace.json"
        builder._yml = mock_yml
        result = builder._output_path()
        assert result == tmp_path / "marketplace.json"


# ---------------------------------------------------------------------------
# _mapper_for_profile
# ---------------------------------------------------------------------------


class TestMapperForProfile:
    def test_raises_build_error_for_unknown_mapper(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        profile = MagicMock()
        profile.mapper = "nonexistent_mapper_xyz"
        with pytest.raises(BuildError, match="Unknown marketplace output mapper"):
            builder._mapper_for_profile(profile)


# ---------------------------------------------------------------------------
# _resolve_entry: local-path
# ---------------------------------------------------------------------------


class TestResolveEntryLocalPath:
    def test_local_entry_returns_resolved_pkg_with_empty_ref(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)

        entry = MagicMock()
        entry.is_local = True
        entry.name = "local-pkg"
        entry.source = "/some/local/path"
        entry.version = None
        entry.tags = ()

        result = builder._resolve_entry(entry)
        assert result.name == "local-pkg"
        assert result.source_repo == ""
        assert result.ref == ""
        assert result.sha == ""
        assert result.is_prerelease is False


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

    def test_sha40_accepted_directly(self, tmp_path):
        builder = self._make_builder(tmp_path)
        sha = "a" * 40
        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = sha
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        resolver = MagicMock()
        result = builder._resolve_explicit_ref(entry, resolver, "owner/repo")
        assert result.sha == sha
        assert result.ref == sha
        resolver.list_remote_refs.assert_not_called()

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

    def test_full_refname_match_branch_allowed(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder._options = BuildOptions(allow_head=True)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "refs/heads/main"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        remote_ref = MagicMock()
        remote_ref.name = "refs/heads/main"
        remote_ref.sha = "c" * 40

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = [remote_ref]

        result = builder._resolve_explicit_ref(entry, resolver, "owner/repo")
        assert result.sha == "c" * 40

    def test_full_refname_branch_not_allowed_raises(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder._options = BuildOptions(allow_head=False)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "refs/heads/main"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        remote_ref = MagicMock()
        remote_ref.name = "refs/heads/main"
        remote_ref.sha = "d" * 40

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = [remote_ref]

        with pytest.raises(HeadNotAllowedError):
            builder._resolve_explicit_ref(entry, resolver, "owner/repo")

    def test_branch_name_shorthand_allowed(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder._options = BuildOptions(allow_head=True)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "main"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        remote_ref = MagicMock()
        remote_ref.name = "refs/heads/main"
        remote_ref.sha = "e" * 40

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = [remote_ref]

        result = builder._resolve_explicit_ref(entry, resolver, "owner/repo")
        assert result.sha == "e" * 40
        assert result.ref == "main"

    def test_branch_name_shorthand_not_allowed_raises(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder._options = BuildOptions(allow_head=False)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "main"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        remote_ref = MagicMock()
        remote_ref.name = "refs/heads/main"
        remote_ref.sha = "f" * 40

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = [remote_ref]

        with pytest.raises(HeadNotAllowedError):
            builder._resolve_explicit_ref(entry, resolver, "owner/repo")

    def test_head_not_allowed_raises(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder._options = BuildOptions(allow_head=False)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "HEAD"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = []

        with pytest.raises(HeadNotAllowedError):
            builder._resolve_explicit_ref(entry, resolver, "owner/repo")

    def test_ref_not_found_raises(self, tmp_path):
        builder = self._make_builder(tmp_path)

        entry = MagicMock()
        entry.name = "pkg"
        entry.ref = "v999.0.0"
        entry.subdir = None
        entry.version = None
        entry.tags = ()

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = []

        with pytest.raises(RefNotFoundError):
            builder._resolve_explicit_ref(entry, resolver, "owner/repo")


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

    def test_no_candidates_raises(self, tmp_path):
        builder = self._make_builder(tmp_path)

        entry = MagicMock()
        entry.name = "pkg"
        entry.version = ">=1.0.0"
        entry.tag_pattern = None
        entry.include_prerelease = False
        entry.subdir = None
        entry.tags = ()

        resolver = MagicMock()
        resolver.list_remote_refs.return_value = []

        with pytest.raises(NoMatchingVersionError):
            builder._resolve_version_range(entry, resolver, "owner/repo", builder._yml)

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

    def test_prerelease_excluded_by_default(self, tmp_path):
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
            _make_tag_ref("v2.0.0-beta.1", "b" * 40),
        ]
        resolver = MagicMock()
        resolver.list_remote_refs.return_value = refs

        with pytest.raises(NoMatchingVersionError):
            builder._resolve_version_range(entry, resolver, "owner/repo", builder._yml)


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


class TestResolve:
    def test_empty_packages_returns_empty_result(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        mock_yml = MagicMock()
        mock_yml.packages = []
        builder._yml = mock_yml

        with patch.object(builder, "_get_resolver"):
            result = builder.resolve()

        assert result.ok is True
        assert result.entries == ()

    def test_continue_on_error_collects_build_error(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml, options=BuildOptions(continue_on_error=True))

        entry = MagicMock()
        entry.name = "bad-pkg"
        mock_yml = MagicMock()
        mock_yml.packages = [entry]
        builder._yml = mock_yml

        with patch.object(builder, "_get_resolver"):
            with patch.object(
                builder,
                "_resolve_entry",
                side_effect=BuildError("resolution failed", package="bad-pkg"),
            ):
                result = builder.resolve()

        assert not result.ok
        assert len(result.errors) == 1
        assert result.errors[0][0] == "bad-pkg"

    def test_continue_on_error_collects_unexpected_exception(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml, options=BuildOptions(continue_on_error=True))

        entry = MagicMock()
        entry.name = "bad-pkg"
        mock_yml = MagicMock()
        mock_yml.packages = [entry]
        builder._yml = mock_yml

        with patch.object(builder, "_get_resolver"):
            with patch.object(
                builder, "_resolve_entry", side_effect=RuntimeError("network failure")
            ):
                result = builder.resolve()

        assert not result.ok
        assert result.errors[0][0] == "bad-pkg"

    def test_raises_build_error_without_continue_on_error(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml, options=BuildOptions(continue_on_error=False))

        entry = MagicMock()
        entry.name = "bad-pkg"
        mock_yml = MagicMock()
        mock_yml.packages = [entry]
        builder._yml = mock_yml

        with patch.object(builder, "_get_resolver"):
            with patch.object(
                builder,
                "_resolve_entry",
                side_effect=BuildError("resolution failed", package="bad-pkg"),
            ):
                with pytest.raises(BuildError):
                    builder.resolve()


# ---------------------------------------------------------------------------
# _compute_diff
# ---------------------------------------------------------------------------


class TestComputeDiff:
    def test_none_old_json_all_added(self):
        new = {"plugins": [{"name": "a", "source": {"sha": "1" * 40}}]}
        result = MarketplaceBuilder._compute_diff(None, new)
        assert result == (0, 1, 0, 0)

    def test_unchanged_when_same_sha(self):
        sha = "a" * 40
        old = {"plugins": [{"name": "a", "source": {"sha": sha}}]}
        new = {"plugins": [{"name": "a", "source": {"sha": sha}}]}
        result = MarketplaceBuilder._compute_diff(old, new)
        assert result == (1, 0, 0, 0)

    def test_updated_when_sha_changed(self):
        old = {"plugins": [{"name": "a", "source": {"sha": "1" * 40}}]}
        new = {"plugins": [{"name": "a", "source": {"sha": "2" * 40}}]}
        result = MarketplaceBuilder._compute_diff(old, new)
        assert result == (0, 0, 1, 0)

    def test_removed_when_old_not_in_new(self):
        old = {"plugins": [{"name": "a", "source": {"sha": "1" * 40}}]}
        new = {"plugins": [{"name": "b", "source": {"sha": "2" * 40}}]}
        result = MarketplaceBuilder._compute_diff(old, new)
        # a is removed, b is added
        assert result == (0, 1, 0, 1)

    def test_legacy_commit_field_supported(self):
        sha = "c" * 40
        old = {"plugins": [{"name": "x", "source": {"commit": sha}}]}
        new = {"plugins": [{"name": "x", "source": {"commit": sha}}]}
        result = MarketplaceBuilder._compute_diff(old, new)
        assert result == (1, 0, 0, 0)

    def test_string_source_handled(self):
        old = {"plugins": [{"name": "local", "source": "/some/path"}]}
        new = {"plugins": [{"name": "local", "source": "/some/path"}]}
        result = MarketplaceBuilder._compute_diff(old, new)
        assert result == (1, 0, 0, 0)


# ---------------------------------------------------------------------------
# _load_existing_json
# ---------------------------------------------------------------------------


class TestLoadExistingJson:
    def test_returns_none_when_file_missing(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        result = builder._load_existing_json(tmp_path / "nonexistent.json")
        assert result is None

    def test_returns_dict_when_valid_json(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        out = tmp_path / "out.json"
        out.write_text('{"plugins": []}')
        result = builder._load_existing_json(out)
        assert result == {"plugins": []}

    def test_returns_none_on_invalid_json(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        out = tmp_path / "out.json"
        out.write_text("not json {{{")
        result = builder._load_existing_json(out)
        assert result is None


# ---------------------------------------------------------------------------
# _serialize_json
# ---------------------------------------------------------------------------


class TestSerializeJson:
    def test_produces_json_with_trailing_newline(self):
        data = {"plugins": []}
        result = MarketplaceBuilder._serialize_json(data)
        assert result.endswith("\n")
        parsed = json.loads(result)
        assert parsed == data

    def test_uses_2_space_indent(self):
        data = {"key": "value"}
        result = MarketplaceBuilder._serialize_json(data)
        assert '  "key"' in result


# ---------------------------------------------------------------------------
# _fetch_remote_metadata
# ---------------------------------------------------------------------------


class TestFetchRemoteMetadata:
    def _make_builder(self, tmp_path, host="github.com"):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)
        builder._host = host
        return builder

    def test_skips_non_github_host(self, tmp_path):
        builder = self._make_builder(tmp_path, host="gitlab.example.com")
        host_info = MagicMock()
        host_info.kind = "gitlab"
        builder._host_info = host_info

        pkg = _make_resolved_pkg()
        result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_skips_ghe_cloud_without_token(self, tmp_path):
        builder = self._make_builder(tmp_path, host="github.example.com")
        host_info = MagicMock()
        host_info.kind = "ghe_cloud"
        builder._host_info = host_info
        builder._github_token = None

        pkg = _make_resolved_pkg()
        result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_returns_none_on_url_error(self, tmp_path):
        builder = self._make_builder(tmp_path)
        host_info = MagicMock()
        host_info.kind = "github"
        builder._host_info = host_info
        builder._github_token = None

        pkg = _make_resolved_pkg()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("network error")):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_returns_description_and_version_on_success(self, tmp_path):
        builder = self._make_builder(tmp_path)
        host_info = MagicMock()
        host_info.kind = "github"
        builder._host_info = host_info
        builder._github_token = None

        pkg = _make_resolved_pkg()
        raw_yaml = yaml.dump({"description": "A test pkg", "version": "1.2.3"})

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_yaml.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)

        assert result is not None
        assert result["description"] == "A test pkg"
        assert result["version"] == "1.2.3"

    def test_includes_auth_header_when_token_set(self, tmp_path):
        builder = self._make_builder(tmp_path)
        host_info = MagicMock()
        host_info.kind = "github"
        builder._host_info = host_info
        builder._github_token = "mytoken"

        pkg = _make_resolved_pkg()
        captured_req = {}

        def _mock_urlopen(req, timeout=None):
            captured_req["headers"] = dict(req.headers)
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"description: hi\n"
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            builder._fetch_remote_metadata(pkg)

        assert "Authorization" in captured_req["headers"]
        assert "mytoken" in captured_req["headers"]["Authorization"]


# ---------------------------------------------------------------------------
# write_output with dry_run and include_diff
# ---------------------------------------------------------------------------


class TestWriteOutput:
    def _make_builder(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml, options=BuildOptions(dry_run=True))
        builder._yml = MagicMock()
        builder._yml.claude.output = "marketplace.json"
        return builder

    def test_dry_run_does_not_write_file(self, tmp_path):
        builder = self._make_builder(tmp_path)
        out_path = tmp_path / "marketplace.json"

        resolved = (_make_resolved_pkg(),)
        mock_mapper_result = MagicMock()
        mock_mapper_result.document = {"plugins": []}
        mock_mapper_result.warnings = ()
        mock_mapper_result.diagnostics = ()

        with patch.object(builder, "compose_output", return_value=({"plugins": []}, (), ())):
            from apm_cli.marketplace.output_profiles import DEFAULT_MARKETPLACE_OUTPUT

            report = builder.write_output(DEFAULT_MARKETPLACE_OUTPUT, resolved, out_path)

        assert not out_path.exists()
        assert report.primary_output.dry_run is True

    def test_include_diff_computes_stats(self, tmp_path):
        builder = MarketplaceBuilder(
            _make_marketplace_yml_file(tmp_path), options=BuildOptions(dry_run=False)
        )
        builder._yml = MagicMock()

        out_path = tmp_path / "marketplace.json"
        old_data = {"plugins": [{"name": "old", "source": {"sha": "0" * 40}}]}
        out_path.write_text(json.dumps(old_data))

        new_doc = {"plugins": [{"name": "new", "source": {"sha": "1" * 40}}]}

        with patch.object(builder, "compose_output", return_value=(new_doc, (), ())):
            from apm_cli.marketplace.output_profiles import DEFAULT_MARKETPLACE_OUTPUT

            report = builder.write_output(
                DEFAULT_MARKETPLACE_OUTPUT, (), out_path, include_diff=True
            )

        primary = report.primary_output
        assert primary.added_count == 1
        assert primary.removed_count == 1


# ---------------------------------------------------------------------------
# _resolve_github_token error path
# ---------------------------------------------------------------------------


class TestResolveGithubToken:
    def test_returns_none_on_exception(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)

        with patch("apm_cli.core.auth.AuthResolver", side_effect=RuntimeError("no auth")):
            result = builder._resolve_github_token()

        assert result is None


# ---------------------------------------------------------------------------
# build() pipeline
# ---------------------------------------------------------------------------


class TestBuildPipeline:
    def test_build_calls_resolve_and_write_output(self, tmp_path):
        yml = _make_marketplace_yml_file(tmp_path)
        builder = MarketplaceBuilder(yml)

        mock_resolve_result = ResolveResult(entries=(), errors=())
        mock_report = BuildReport(
            outputs=(_make_output_report(output_path=tmp_path / "marketplace.json"),)
        )

        with patch.object(builder, "resolve", return_value=mock_resolve_result):
            with patch.object(builder, "write_output", return_value=mock_report):
                with patch.object(builder, "remote_metadata_for_profile", return_value=None):
                    with patch.object(builder, "_output_path", return_value=tmp_path / "out.json"):
                        report = builder.build()

        assert isinstance(report, BuildReport)
