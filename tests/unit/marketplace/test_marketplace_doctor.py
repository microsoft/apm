"""Tests for marketplace doctor (issue #847)."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.doctor import (
    DepClassification,
    DepIssue,
    FetchStatus,
    PluginDepReport,
    _collect_apm_dep_strings,
    _normalize_dep_entry,
    _resolve_plugin_github_coords,
    _suggest_replacement,
    check_plugin,
    classify_dependency,
    fetch_plugin_apm_yml,
    run_doctor,
)
from apm_cli.marketplace.errors import MarketplaceError
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Mirror the isolation used by other marketplace tests."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        "apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json")
    )
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


def _gh_plugin(name="p", repo="acme/agent-forge", ref="abc123", path=""):
    src = {"type": "github", "repo": repo, "ref": ref}
    if path:
        src["path"] = path
    return MarketplacePlugin(name=name, source=src)


def _manifest(*plugins, name="test-marketplace"):
    return MarketplaceManifest(name=name, plugins=tuple(plugins))


def _source(name="test-marketplace"):
    return MarketplaceSource(name=name, owner="acme", repo="marketplace")


# ===================================================================
# classify_dependency
# ===================================================================


class TestClassifyDependency:
    """Cover every path of the dep classifier."""

    @pytest.mark.parametrize(
        "dep",
        [
            "conventions@my-marketplace",
            "conventions@my-marketplace#v1.2.3",
            "code-quality@acme#feat/some-branch",
        ],
    )
    def test_marketplace_refs(self, dep):
        assert classify_dependency(dep) == DepClassification.MARKETPLACE

    def test_marketplace_ref_with_semver_range_is_still_marketplace(self):
        # parse_marketplace_ref raises ValueError for semver ranges; doctor
        # should classify as MARKETPLACE so that the install path (not
        # doctor) surfaces the grammar error.
        assert (
            classify_dependency("pkg@mkt#^1.2.3")
            == DepClassification.MARKETPLACE
        )

    @pytest.mark.parametrize(
        "dep",
        [
            "./local/pkg",
            "../sibling-pkg",
            "/abs/path/pkg",
        ],
    )
    def test_local_paths(self, dep):
        assert classify_dependency(dep) == DepClassification.LOCAL

    @pytest.mark.parametrize(
        "dep",
        [
            "acme/agent-forge",
            "acme/agent-forge/general/conventions",
            "acme/agent-forge#abc123",
            "acme/agent-forge/general/conventions#v1.0.0",
            "https://gitlab.com/acme/agent-forge.git",
            "git@gitlab.com:acme/agent-forge.git",
            "ssh://git@gitlab.com/acme/agent-forge.git",
            "github.com/acme/agent-forge",
        ],
    )
    def test_bypass_paths(self, dep):
        assert (
            classify_dependency(dep)
            == DepClassification.BYPASSES_MARKETPLACE
        )

    @pytest.mark.parametrize("dep", ["", "   ", None])
    def test_empty(self, dep):
        # None and empty-ish strings are reported as EMPTY, letting the
        # normal schema validator (not doctor) surface the underlying issue.
        assert classify_dependency(dep) == DepClassification.EMPTY  # type: ignore[arg-type]


# ===================================================================
# _collect_apm_dep_strings
# ===================================================================


class TestCollectApmDepStrings:
    def test_both_sections(self):
        data = {
            "dependencies": {"apm": ["a@mkt", "owner/b"]},
            "devDependencies": {"apm": ["owner/c"]},
        }
        assert _collect_apm_dep_strings(data) == [
            "a@mkt", "owner/b", "owner/c"
        ]

    def test_dict_form_git_entry_is_captured(self):
        """Dict-form deps bypass the marketplace too -- regression for #847
        follow-up review finding (string-only collection was silently
        dropping object-style entries).
        """
        data = {
            "dependencies": {
                "apm": [
                    "name@mkt",
                    {
                        "git": "https://gitlab.com/acme/coding-standards.git",
                        "path": "instructions/security",
                        "ref": "v2.0",
                    },
                ]
            }
        }
        flat = _collect_apm_dep_strings(data)
        assert "name@mkt" in flat
        assert any(
            "gitlab.com/acme/coding-standards.git" in s for s in flat
        )

    def test_dict_form_local_path_entry_is_captured(self):
        data = {
            "dependencies": {
                "apm": [{"path": "./packages/shared"}]
            }
        }
        assert _collect_apm_dep_strings(data) == ["./packages/shared"]

    def test_skip_unrecognised_primitives(self):
        # 42 and None cannot be flattened, but the valid string is kept
        data = {"dependencies": {"apm": ["a@mkt", 42, None]}}
        assert _collect_apm_dep_strings(data) == ["a@mkt"]

    def test_skip_malformed_dict_entries(self):
        # Missing both git and path -> cannot flatten -> dropped
        data = {
            "dependencies": {
                "apm": [{"alias": "orphan"}, {"git": "", "path": "x"}]
            }
        }
        assert _collect_apm_dep_strings(data) == []

    def test_missing_sections(self):
        assert _collect_apm_dep_strings({}) == []
        assert _collect_apm_dep_strings({"dependencies": None}) == []
        assert _collect_apm_dep_strings({"dependencies": {}}) == []
        assert _collect_apm_dep_strings(
            {"dependencies": {"apm": "not-a-list"}}
        ) == []


class TestNormalizeDepEntry:
    def test_string_passthrough(self):
        assert _normalize_dep_entry("a@mkt") == "a@mkt"

    def test_dict_git_entry(self):
        out = _normalize_dep_entry(
            {"git": "https://gitlab.com/acme/x.git", "path": "sub", "ref": "v1"}
        )
        assert out == "https://gitlab.com/acme/x.git"

    def test_dict_path_entry(self):
        assert (
            _normalize_dep_entry({"path": "./local/pkg"}) == "./local/pkg"
        )

    def test_dict_path_whitespace_stripped(self):
        assert _normalize_dep_entry({"path": "  ./x  "}) == "./x"

    def test_dict_missing_fields(self):
        assert _normalize_dep_entry({"alias": "x"}) is None
        assert _normalize_dep_entry({"git": ""}) is None
        assert _normalize_dep_entry({"path": "   "}) is None

    def test_non_string_non_dict(self):
        assert _normalize_dep_entry(42) is None
        assert _normalize_dep_entry(None) is None
        assert _normalize_dep_entry(["nope"]) is None


class TestSuggestReplacement:
    def test_strips_git_suffix(self):
        """Review finding: suggestion should not end with .git."""
        assert (
            "coding-standards@<marketplace>"
            in _suggest_replacement(
                "https://gitlab.com/acme/coding-standards.git"
            )
        )
        assert ".git@" not in _suggest_replacement(
            "https://gitlab.com/acme/x.git"
        )


# ===================================================================
# _resolve_plugin_github_coords
# ===================================================================


class TestResolvePluginGithubCoords:
    def test_basic_github_dict(self):
        plugin = _gh_plugin(repo="acme/forge", ref="abc123")
        coords = _resolve_plugin_github_coords(plugin, "github.com")
        assert coords == ("github.com", "acme", "forge", "abc123", "apm.yml")

    def test_with_subdir_path(self):
        plugin = _gh_plugin(
            repo="acme/forge", ref="abc", path="agents/code-quality"
        )
        coords = _resolve_plugin_github_coords(plugin, "github.com")
        assert coords == (
            "github.com",
            "acme",
            "forge",
            "abc",
            "agents/code-quality/apm.yml",
        )

    def test_string_source_unsupported(self):
        plugin = MarketplacePlugin(name="p", source="acme/forge")
        assert _resolve_plugin_github_coords(plugin, "github.com") is None

    def test_non_github_type(self):
        plugin = MarketplacePlugin(
            name="p", source={"type": "npm", "name": "x"}
        )
        assert _resolve_plugin_github_coords(plugin, "github.com") is None

    def test_repo_without_slash(self):
        plugin = MarketplacePlugin(
            name="p", source={"type": "github", "repo": "no-slash"}
        )
        assert _resolve_plugin_github_coords(plugin, "github.com") is None

    def test_path_traversal_rejected(self):
        plugin = _gh_plugin(path="../../etc")
        assert _resolve_plugin_github_coords(plugin, "github.com") is None

    def test_ref_defaults_to_head(self):
        plugin = MarketplacePlugin(
            name="p", source={"type": "github", "repo": "acme/forge"}
        )
        host, owner, name, ref, path = _resolve_plugin_github_coords(
            plugin, "github.com"
        )
        assert ref == "HEAD"

    def test_source_host_overrides_fallback(self):
        plugin = MarketplacePlugin(
            name="p",
            source={
                "type": "github",
                "repo": "acme/forge",
                "host": "gitlab.com",
            },
        )
        coords = _resolve_plugin_github_coords(plugin, "github.com")
        assert coords[0] == "gitlab.com"


# ===================================================================
# check_plugin (with fake fetcher)
# ===================================================================


def _fake_fetcher(result_by_plugin):
    """Build a fetcher that returns pre-seeded results keyed by plugin name."""

    def _fetcher(plugin, marketplace_source, auth_resolver=None):
        return result_by_plugin[plugin.name]

    return _fetcher


class TestCheckPlugin:
    def test_plugin_with_direct_path_dep_is_flagged(self):
        plugin = _gh_plugin("code-quality")
        fetcher = _fake_fetcher(
            {
                "code-quality": (
                    FetchStatus.OK,
                    {
                        "dependencies": {
                            "apm": [
                                "acme/agent-forge/general/conventions",
                                "standards@acme-mkt",
                            ]
                        }
                    },
                    "",
                )
            }
        )
        report = check_plugin(plugin, _source(), _fetcher=fetcher)
        assert report.fetch_status == FetchStatus.OK
        assert len(report.issues) == 1
        assert (
            report.issues[0].dep
            == "acme/agent-forge/general/conventions"
        )
        assert (
            report.issues[0].classification
            == DepClassification.BYPASSES_MARKETPLACE
        )
        assert "marketplace" in report.issues[0].suggestion

    def test_clean_plugin_has_no_issues(self):
        plugin = _gh_plugin("clean")
        fetcher = _fake_fetcher(
            {
                "clean": (
                    FetchStatus.OK,
                    {
                        "dependencies": {
                            "apm": ["x@mkt", "./local-dev-pkg"]
                        }
                    },
                    "",
                )
            }
        )
        report = check_plugin(plugin, _source(), _fetcher=fetcher)
        assert report.fetch_status == FetchStatus.OK
        assert report.issues == ()

    def test_missing_manifest_is_skipped(self):
        plugin = _gh_plugin("no-manifest")
        fetcher = _fake_fetcher(
            {"no-manifest": (FetchStatus.NO_MANIFEST, None, "not found")}
        )
        report = check_plugin(plugin, _source(), _fetcher=fetcher)
        assert report.fetch_status == FetchStatus.NO_MANIFEST
        assert report.issues == ()

    def test_parse_error_propagates_status(self):
        plugin = _gh_plugin("bad-yaml")
        fetcher = _fake_fetcher(
            {"bad-yaml": (FetchStatus.PARSE_ERROR, None, "bad yaml")}
        )
        report = check_plugin(plugin, _source(), _fetcher=fetcher)
        assert report.fetch_status == FetchStatus.PARSE_ERROR
        assert "bad yaml" in report.detail

    def test_network_error_propagates(self):
        plugin = _gh_plugin("offline")
        fetcher = _fake_fetcher(
            {"offline": (FetchStatus.NETWORK_ERROR, None, "DNS")}
        )
        report = check_plugin(plugin, _source(), _fetcher=fetcher)
        assert report.fetch_status == FetchStatus.NETWORK_ERROR


class TestFetchPluginApmYmlErrorMessage:
    """Regression: plugin fetch errors must not leak the MarketplaceFetchError
    retry hint ('Run apm marketplace update X'), which is invalid for
    plugin-level failures.
    """

    def test_network_error_message_has_no_marketplace_update_hint(self):
        plugin = _gh_plugin("code-quality")
        source = _source("acme-agents")

        # Make fetch_raw raise as the real auth layer would on network error
        with patch(
            "apm_cli.marketplace.doctor.fetch_raw",
            side_effect=MarketplaceError(
                "fetching acme/forge/agents/x/apm.yml@abc: connection reset"
            ),
        ):
            status, data, detail = fetch_plugin_apm_yml(plugin, source)

        assert status == FetchStatus.NETWORK_ERROR
        assert data is None
        # Positive assertion: the underlying reason surfaces
        assert "connection reset" in detail
        # Negative assertion: the misleading retry hint is not present
        assert "apm marketplace update" not in detail
        assert "Failed to fetch marketplace" not in detail


# ===================================================================
# run_doctor aggregates and isolates failures
# ===================================================================


class TestRunDoctor:
    def test_mixed_marketplace(self):
        clean = _gh_plugin("clean")
        bad = _gh_plugin("bad")
        missing = _gh_plugin("no-manifest")
        manifest = _manifest(clean, bad, missing)

        fetcher = _fake_fetcher(
            {
                "clean": (
                    FetchStatus.OK,
                    {"dependencies": {"apm": ["x@mkt"]}},
                    "",
                ),
                "bad": (
                    FetchStatus.OK,
                    {
                        "dependencies": {
                            "apm": ["acme/forge/general/conventions"]
                        }
                    },
                    "",
                ),
                "no-manifest": (FetchStatus.NO_MANIFEST, None, ""),
            }
        )
        reports = run_doctor(manifest, _source(), _fetcher=fetcher)
        assert len(reports) == 3
        status_by_name = {r.plugin_name: r for r in reports}
        assert status_by_name["clean"].issues == ()
        assert len(status_by_name["bad"].issues) == 1
        assert (
            status_by_name["no-manifest"].fetch_status
            == FetchStatus.NO_MANIFEST
        )

    def test_one_plugin_crash_isolated(self):
        good = _gh_plugin("good")
        bomb = _gh_plugin("bomb")
        manifest = _manifest(good, bomb)

        def fetcher(plugin, source, auth_resolver=None):
            if plugin.name == "bomb":
                raise RuntimeError("boom")
            return (
                FetchStatus.OK,
                {"dependencies": {"apm": ["x@mkt"]}},
                "",
            )

        reports = run_doctor(manifest, _source(), _fetcher=fetcher)
        names = [r.plugin_name for r in reports]
        assert names == ["good", "bomb"]
        by_name = {r.plugin_name: r for r in reports}
        assert by_name["good"].fetch_status == FetchStatus.OK
        assert by_name["bomb"].fetch_status == FetchStatus.NETWORK_ERROR
        assert "boom" in by_name["bomb"].detail


# ===================================================================
# CLI integration
# ===================================================================


class TestDoctorCLI:
    """End-to-end through the Click command, fetcher stubbed at the seam."""

    def _invoke(self, runner, extra_args=()):
        from apm_cli.commands.marketplace import marketplace

        return runner.invoke(marketplace, ["doctor", "mymarket", *extra_args])

    def _stub_registry_and_manifest(self, manifest, source):
        patches = [
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=manifest,
            ),
        ]
        for p in patches:
            p.start()
        return patches

    def _stop_patches(self, patches):
        for p in patches:
            p.stop()

    def test_reports_bypass_dep(self, runner):
        source = _source("mymarket")
        manifest = _manifest(_gh_plugin("code-quality"))

        patches = self._stub_registry_and_manifest(manifest, source)
        try:
            with patch(
                "apm_cli.marketplace.doctor.fetch_plugin_apm_yml",
                return_value=(
                    FetchStatus.OK,
                    {
                        "dependencies": {
                            "apm": ["acme/forge/general/conventions"]
                        }
                    },
                    "",
                ),
            ):
                result = self._invoke(runner)
        finally:
            self._stop_patches(patches)

        assert result.exit_code == 0, result.output
        assert "code-quality" in result.output
        assert "acme/forge/general/conventions" in result.output
        assert "1 bypass warning" in result.output

    def test_strict_exits_nonzero_on_bypass(self, runner):
        source = _source("mymarket")
        manifest = _manifest(_gh_plugin("code-quality"))

        patches = self._stub_registry_and_manifest(manifest, source)
        try:
            with patch(
                "apm_cli.marketplace.doctor.fetch_plugin_apm_yml",
                return_value=(
                    FetchStatus.OK,
                    {"dependencies": {"apm": ["acme/forge/general/x"]}},
                    "",
                ),
            ):
                result = self._invoke(runner, extra_args=("--strict",))
        finally:
            self._stop_patches(patches)

        assert result.exit_code == 1, result.output

    def test_clean_marketplace_exits_zero(self, runner):
        source = _source("mymarket")
        manifest = _manifest(_gh_plugin("clean"))

        patches = self._stub_registry_and_manifest(manifest, source)
        try:
            with patch(
                "apm_cli.marketplace.doctor.fetch_plugin_apm_yml",
                return_value=(
                    FetchStatus.OK,
                    {"dependencies": {"apm": ["x@mkt"]}},
                    "",
                ),
            ):
                result = self._invoke(runner, extra_args=("--strict",))
        finally:
            self._stop_patches(patches)

        assert result.exit_code == 0, result.output
        assert "0 bypass warnings" in result.output
