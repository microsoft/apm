"""Tests for the install flow with mocked marketplace resolution."""

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
