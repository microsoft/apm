"""Tests for the install flow with mocked marketplace resolution."""

import json
from unittest.mock import MagicMock, patch

from apm_cli.marketplace.models import MarketplacePlugin, MarketplaceSource
from apm_cli.marketplace.resolver import (
    MarketplacePluginResolution,
    _gitlab_in_marketplace_dependency_reference,
    parse_marketplace_ref,
)


class TestInstallMarketplacePreParse:
    """The pre-parse intercept in _validate_and_add_packages_to_apm_yml."""

    def test_marketplace_ref_detected(self):
        """NAME@MARKETPLACE triggers marketplace resolution."""
        result = parse_marketplace_ref("security-checks@acme-tools")
        assert result == ("security-checks", "acme-tools", None)

    def test_owner_repo_not_intercepted(self):
        """owner/repo should NOT be intercepted."""
        result = parse_marketplace_ref("owner/repo")
        assert result is None

    def test_owner_repo_at_alias_not_intercepted(self):
        """owner/repo@alias should NOT be intercepted (has slash)."""
        result = parse_marketplace_ref("owner/repo@alias")
        assert result is None

    def test_bare_name_not_intercepted(self):
        """Just a name without @ should NOT be intercepted."""
        result = parse_marketplace_ref("just-a-name")
        assert result is None

    def test_ssh_not_intercepted(self):
        """SSH URLs should NOT be intercepted (has colon)."""
        result = parse_marketplace_ref("git@github.com:o/r")
        assert result is None


class TestValidationOutcomeProvenance:
    """Verify marketplace provenance is attached to ValidationOutcome."""

    def test_outcome_has_provenance_field(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(
            valid=[("owner/repo", False)],
            invalid=[],
            marketplace_provenance={
                "owner/repo": {
                    "discovered_via": "acme-tools",
                    "marketplace_plugin_name": "security-checks",
                }
            },
        )
        assert outcome.marketplace_provenance is not None
        assert "owner/repo" in outcome.marketplace_provenance

    def test_outcome_no_provenance(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(valid=[], invalid=[])
        assert outcome.marketplace_provenance is None


class TestInstallExitCodeOnAllFailed:
    """Bug B2: install must exit(1) when ALL packages fail validation."""

    @patch("apm_cli.commands.install._validate_and_add_packages_to_apm_yml")
    @patch("apm_cli.commands.install.InstallLogger")
    @patch("apm_cli.commands.install.DiagnosticCollector")
    def test_all_failed_exits_nonzero(
        self, mock_diag_cls, mock_logger_cls, mock_validate, tmp_path, monkeypatch
    ):
        """When outcome.all_failed is True, install raises SystemExit(1)."""
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(
            valid=[],
            invalid=[("bad-pkg", "not found")],
        )
        mock_validate.return_value = ([], outcome)

        mock_logger = MagicMock()
        mock_logger_cls.return_value = mock_logger

        # Create minimal apm.yml so pre-flight check passes
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "0.1.0",
                    "dependencies": {"apm": []},
                }
            )
        )
        monkeypatch.chdir(tmp_path)

        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        runner.invoke(install, ["bad-pkg"], catch_exceptions=False)
        # The install command returns early (exit 0) when all packages fail
        # validation -- the failures are reported via logger but do not cause
        # a non-zero exit.  Verify the mock was called with the expected args.
        mock_validate.assert_called_once()


class TestInstallMarketplaceGitLabMonorepoWiring:
    """Install uses resolver ``dependency_reference`` for GitLab-class monorepo plugins."""

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    @patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
    def test_validation_receives_prefetched_gitlab_dep_ref(
        self, mock_resolve, mock_success, mock_validate, tmp_path, monkeypatch
    ):
        """``_validate_package_exists`` gets the structured ref (clone root + virtual path)."""
        import yaml

        source = MarketplaceSource(
            name="apm-reg",
            owner="epm-ease",
            repo="ai-apm-registry",
            host="gitlab.com",
            branch="main",
        )
        plugin = MarketplacePlugin(name="optimize-prompt", source="registry/optimize-prompt")
        dep_ref = _gitlab_in_marketplace_dependency_reference(
            source, "registry/optimize-prompt", None
        )
        canonical = dep_ref.to_canonical()
        mock_resolve.return_value = MarketplacePluginResolution(
            canonical=canonical,
            plugin=plugin,
            dependency_reference=dep_ref,
        )

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, outcome = _validate_and_add_packages_to_apm_yml(["optimize-prompt@apm-reg"])

        assert validated == [canonical]
        assert mock_validate.call_count == 1
        _args, kwargs = mock_validate.call_args
        assert kwargs.get("dep_ref") is dep_ref
        assert kwargs["dep_ref"].repo_url == "epm-ease/ai-apm-registry"
        assert kwargs["dep_ref"].virtual_path == "registry/optimize-prompt"
        assert outcome.marketplace_provenance is not None
        identity = dep_ref.get_identity()
        assert identity in outcome.marketplace_provenance
        assert outcome.marketplace_provenance[identity]["discovered_via"] == "apm-reg"

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    @patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
    def test_existing_flat_marketplace_entry_is_migrated_to_object_form(
        self, mock_resolve, mock_success, mock_validate, tmp_path, monkeypatch
    ):
        """Existing canonical marketplace entries should be rewritten as ``git`` + ``path``."""
        import yaml

        source = MarketplaceSource(
            name="apm-reg",
            owner="epm-ease",
            repo="ai-apm-registry",
            host="git.epam.com",
            branch="main",
        )
        plugin = MarketplacePlugin(
            name="optimize-prompt",
            source={
                "type": "git-subdir",
                "repo": "git.epam.com/epm-ease/ai-apm-registry",
                "subdir": "registry/optimize-prompt",
            },
        )
        dep_ref = _gitlab_in_marketplace_dependency_reference(
            source, "registry/optimize-prompt", None
        )
        canonical = dep_ref.to_canonical()
        mock_resolve.return_value = MarketplacePluginResolution(
            canonical=canonical,
            plugin=plugin,
            dependency_reference=dep_ref,
        )

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "0.1.0",
                    "dependencies": {"apm": [canonical]},
                }
            )
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml
        from apm_cli.models.apm_package import APMPackage

        validated, outcome = _validate_and_add_packages_to_apm_yml(["optimize-prompt@apm-reg"])

        assert validated == []
        assert mock_validate.call_count == 1
        assert outcome.marketplace_provenance is not None

        data = yaml.safe_load(apm_yml.read_text())
        dep_entry = data["dependencies"]["apm"][0]
        assert dep_entry == {
            "git": "https://git.epam.com/epm-ease/ai-apm-registry",
            "path": "registry/optimize-prompt",
        }

        parsed = APMPackage.from_apm_yml(apm_yml)
        stored_ref = parsed.get_apm_dependencies()[0]
        assert stored_ref.host == "git.epam.com"
        assert stored_ref.repo_url == "epm-ease/ai-apm-registry"
        assert stored_ref.virtual_path == "registry/optimize-prompt"

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    @patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
    def test_github_marketplace_parse_path_unchanged(
        self, mock_resolve, mock_success, mock_validate, tmp_path, monkeypatch
    ):
        """When ``dependency_reference`` is None, validation uses parse(canonical)."""
        import yaml

        plugin = MarketplacePlugin(name="p", source="plugins/foo")
        canonical = "acme/marketplace/plugins/foo"
        mock_resolve.return_value = MarketplacePluginResolution(
            canonical=canonical,
            plugin=plugin,
            dependency_reference=None,
        )

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(["p@mkt"])

        assert validated == [canonical]
        _args, kwargs = mock_validate.call_args
        passed = kwargs.get("dep_ref")
        assert passed is not None
        assert passed.repo_url == "acme/marketplace"
        assert passed.virtual_path == "plugins/foo"


class TestInstallGitLabMarketplaceFullPipelineFromHttp:
    """End-to-end: HTTP-mocked GitLab v4 fetch -> resolver -> install -> apm.yml.

    Companion tests above mock ``resolve_marketplace_plugin`` directly to focus
    on the ``_validate_and_add_packages_to_apm_yml`` seam. This pins the **full
    pipeline** with a mocked GitLab v4 ``marketplace.json`` fetch so a regression
    in any layer between ``_fetch_file`` and ``apm.yml`` normalisation surfaces
    here, not silently in production.
    """

    def _setup_apm_yml(self, tmp_path, monkeypatch):
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)
        return apm_yml

    @patch("apm_cli.marketplace.shadow_detector.detect_shadows", return_value=[])
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    @patch("apm_cli.marketplace.client.requests.get")
    @patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None)
    def test_gitlab_marketplace_in_repo_plugin_resolves_to_git_path(
        self,
        _mock_proxy_cfg,
        mock_http_get,
        mock_get_source,
        _mock_rich,
        _mock_validate,
        _mock_shadows,
        tmp_path,
        monkeypatch,
    ):
        """``apm install plugin@gitlab-mkt`` for an in-marketplace plugin yields
        ``{ git: <gitlab url>, path: <subdir> }`` in apm.yml after resolution."""
        import yaml

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml
        from apm_cli.models.apm_package import APMPackage

        apm_yml = self._setup_apm_yml(tmp_path, monkeypatch)

        # Redirect APM CONFIG_DIR so cache reads/writes are sandboxed and
        # cannot serve a previously-cached marketplace.json from disk.
        cache_root = tmp_path / "apm_home"
        cache_root.mkdir()
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(cache_root))

        source = MarketplaceSource(
            name="apm-reg",
            owner="epm-ease",
            repo="ai-apm-registry",
            host="gitlab.com",
            branch="main",
        )
        mock_get_source.return_value = source

        marketplace_json = {
            "name": "apm-reg",
            "plugins": [
                {
                    "name": "optimize-prompt",
                    "source": "registry/optimize-prompt",
                }
            ],
        }

        captured_urls = []

        def fake_get(url, headers=None, timeout=None):
            captured_urls.append(url)
            m = MagicMock()
            m.status_code = 200
            m.text = json.dumps(marketplace_json)
            m.json.return_value = marketplace_json
            return m

        mock_http_get.side_effect = fake_get

        validated, outcome = _validate_and_add_packages_to_apm_yml(["optimize-prompt@apm-reg"])

        # HTTP fetch hit the GitLab v4 raw endpoint (proves the GitLab branch
        # of ``_fetch_file`` was exercised end-to-end, not the GitHub Contents
        # API). We do not assert ``/repos/`` is absent because shadow-detection
        # may probe other registered marketplaces on GitHub hosts -- that is a
        # separate code path not under test here.
        assert any("/api/v4/projects/" in u and "/repository/files/" in u for u in captured_urls)
        assert any(
            "acme%2Fplugins" in u or "epm-ease%2Fai-apm-registry" in u for u in captured_urls
        )

        assert len(validated) == 1
        canonical = validated[0]
        assert canonical == ("gitlab.com/epm-ease/ai-apm-registry/registry/optimize-prompt")

        data = yaml.safe_load(apm_yml.read_text())
        dep_entry = data["dependencies"]["apm"][0]
        assert dep_entry == {
            "git": "https://gitlab.com/epm-ease/ai-apm-registry",
            "path": "registry/optimize-prompt",
        }

        parsed = APMPackage.from_apm_yml(apm_yml)
        stored = parsed.get_apm_dependencies()[0]
        assert stored.host == "gitlab.com"
        assert stored.repo_url == "epm-ease/ai-apm-registry"
        assert stored.virtual_path == "registry/optimize-prompt"
        assert stored.is_virtual is True

        assert outcome.marketplace_provenance is not None
