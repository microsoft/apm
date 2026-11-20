"""Tests for normalize-on-write canonicalization and identity-based CLI matching (filesystem, process and CLI boundary cases)."""

from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401
from urllib.parse import urlparse

import pytest

from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.component

# ── to_canonical() ──────────────────────────────────────────────────────────


# ── get_identity() ──────────────────────────────────────────────────────────


# ── canonicalize() static method ────────────────────────────────────────────


# ── backward compat: get_canonical_dependency_string() ──────────────────────


# ── Normalize-on-write in _validate_and_add_packages_to_apm_yml ────────────


class TestNormalizeOnWrite:
    """Test that _validate_and_add_packages_to_apm_yml canonicalizes inputs."""

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_https_url_stored_as_shorthand(
        self, mock_success, mock_validate, tmp_path, monkeypatch
    ):
        """HTTPS GitHub URL is stored as owner/repo in apm.yml."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["https://github.com/microsoft/apm-sample-package.git"]
        )

        assert validated == ["microsoft/apm-sample-package"]
        data = yaml.safe_load(apm_yml.read_text())
        assert "microsoft/apm-sample-package" in data["dependencies"]["apm"]

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_ssh_url_stored_as_shorthand(self, mock_success, mock_validate, tmp_path, monkeypatch):
        """SSH GitHub URL is stored as owner/repo in apm.yml."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["git@github.com:microsoft/apm-sample-package.git"]
        )

        assert validated == ["microsoft/apm-sample-package"]

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_fqdn_github_stored_as_shorthand(
        self, mock_success, mock_validate, tmp_path, monkeypatch
    ):
        """FQDN github.com/owner/repo is stored as owner/repo."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["github.com/microsoft/apm-sample-package"]
        )

        assert validated == ["microsoft/apm-sample-package"]

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_gitlab_url_preserves_host(self, mock_success, mock_validate, tmp_path, monkeypatch):
        """GitLab URL preserves the host in canonical form."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["https://gitlab.com/acme/standards.git"]
        )

        assert validated == ["gitlab.com/acme/standards"]
        data = yaml.safe_load(apm_yml.read_text())
        assert "gitlab.com/acme/standards" in data["dependencies"]["apm"]

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_duplicate_detection_different_forms(self, mock_validate, tmp_path, monkeypatch):
        """Installing the same package in different forms doesn't create duplicates."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "0.1.0",
                    "dependencies": {"apm": ["microsoft/apm-sample-package"]},
                }
            )
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["https://github.com/microsoft/apm-sample-package.git"]
        )

        # Should return empty — package already exists
        assert validated == []
        data = yaml.safe_load(apm_yml.read_text())
        # No duplicate added
        assert data["dependencies"]["apm"].count("microsoft/apm-sample-package") == 1

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_batch_dedup(self, mock_success, mock_validate, tmp_path, monkeypatch):
        """Installing the same package twice in one batch only adds once."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            [
                "microsoft/apm-sample-package",
                "https://github.com/microsoft/apm-sample-package.git",
            ]
        )

        assert len(validated) == 1
        assert validated[0] == "microsoft/apm-sample-package"

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install._rich_success")
    def test_ref_preserved_in_canonical(self, mock_success, mock_validate, tmp_path, monkeypatch):
        """Reference is preserved in the canonical form."""
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": []}})
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            ["https://github.com/microsoft/apm-sample-package.git#v1.0.0"]
        )

        assert validated == ["microsoft/apm-sample-package#v1.0.0"]

    @pytest.mark.parametrize(
        ("raw_url", "expected_port"),
        [
            ("http://mirror.example.com:8080/owner/repo", 8080),
            ("http://mirror.example.com:80/owner/repo", None),
            ("http://mirror.example.com/owner/repo", None),
        ],
        ids=["custom-port", "default-port", "no-port"],
    )
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_http_port_round_trips_through_apm_yml(
        self,
        mock_validate,
        raw_url,
        expected_port,
        tmp_path,
        monkeypatch,
    ):
        """HTTP transport identity survives install write and manifest reload."""
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.utils.yaml_io import dump_yaml, load_yaml

        apm_yml = tmp_path / "apm.yml"
        dump_yaml(
            {"name": "test", "version": "0.1.0", "dependencies": {"apm": []}},
            apm_yml,
        )
        monkeypatch.chdir(tmp_path)
        input_ref = DependencyReference.parse(raw_url)

        validated, _outcome = _validate_and_add_packages_to_apm_yml(
            [raw_url],
            allow_insecure=True,
        )

        assert validated == [input_ref.to_canonical()]
        persisted = load_yaml(apm_yml)
        persisted_url = urlparse(persisted["dependencies"]["apm"][0]["git"])
        reloaded_ref = APMPackage.from_apm_yml(apm_yml).get_apm_dependencies()[0]
        assert (
            persisted_url.scheme,
            persisted_url.hostname,
            persisted_url.port,
            reloaded_ref.port,
            reloaded_ref.get_identity(),
        ) == (
            "http",
            "mirror.example.com",
            expected_port,
            expected_port,
            input_ref.get_identity(),
        )

    @pytest.mark.parametrize(
        ("raw_url", "allow_insecure"),
        [
            ("http://mirror.example.com:8080/owner/repo", False),
            ("http://mirror.example.com:not-a-port/owner/repo", True),
        ],
        ids=["unsafe-without-opt-in", "invalid-port"],
    )
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_rejected_http_input_preserves_last_good_manifest(
        self,
        mock_validate,
        raw_url,
        allow_insecure,
        tmp_path,
        monkeypatch,
    ):
        """Unsafe or invalid HTTP input cannot replace the last-good manifest."""
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.utils.yaml_io import dump_yaml

        apm_yml = tmp_path / "apm.yml"
        dump_yaml(
            {
                "name": "test",
                "version": "0.1.0",
                "dependencies": {"apm": ["owner/last-good"]},
            },
            apm_yml,
        )
        monkeypatch.chdir(tmp_path)
        last_good = apm_yml.read_bytes()

        validated, outcome = _validate_and_add_packages_to_apm_yml(
            [raw_url],
            allow_insecure=allow_insecure,
        )

        assert validated == []
        assert len(outcome.invalid) == 1
        assert apm_yml.read_bytes() == last_good
        reloaded = APMPackage.from_apm_yml(apm_yml).get_apm_dependencies()
        assert [dep.get_identity() for dep in reloaded] == ["owner/last-good"]
        mock_validate.assert_not_called()


# ── Uninstall identity matching ─────────────────────────────────────────────


class TestUninstallIdentityMatching:
    """Test that uninstall matches packages by identity regardless of input form."""

    def _make_apm_yml(self, tmp_path, deps):
        import yaml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump({"name": "test", "version": "0.1.0", "dependencies": {"apm": deps}})
        )
        return apm_yml

    def test_uninstall_shorthand_matches_canonical(self):
        """Uninstalling 'owner/repo' matches canonical 'owner/repo' in apm.yml."""
        pkg_ref = DependencyReference.parse("owner/repo")
        dep_ref = DependencyReference.parse("owner/repo")
        assert pkg_ref.get_identity() == dep_ref.get_identity()

    def test_uninstall_https_matches_shorthand(self):
        """Uninstalling via HTTPS URL matches shorthand in apm.yml."""
        pkg_ref = DependencyReference.parse("https://github.com/owner/repo.git")
        dep_ref = DependencyReference.parse("owner/repo")
        assert pkg_ref.get_identity() == dep_ref.get_identity()

    def test_uninstall_ssh_matches_shorthand(self):
        """Uninstalling via SSH URL matches shorthand in apm.yml."""
        pkg_ref = DependencyReference.parse("git@github.com:owner/repo.git")
        dep_ref = DependencyReference.parse("owner/repo")
        assert pkg_ref.get_identity() == dep_ref.get_identity()

    def test_uninstall_fqdn_matches_shorthand(self):
        """Uninstalling via FQDN matches shorthand in apm.yml."""
        pkg_ref = DependencyReference.parse("github.com/owner/repo")
        dep_ref = DependencyReference.parse("owner/repo")
        assert pkg_ref.get_identity() == dep_ref.get_identity()

    def test_uninstall_gitlab_matches_gitlab(self):
        """Uninstalling gitlab package matches gitlab canonical entry."""
        pkg_ref = DependencyReference.parse("https://gitlab.com/acme/rules.git")
        dep_ref = DependencyReference.parse("gitlab.com/acme/rules")
        assert pkg_ref.get_identity() == dep_ref.get_identity()

    def test_uninstall_gitlab_no_match_github(self):
        """GitLab package does NOT match GitHub package with same owner/repo."""
        pkg_ref = DependencyReference.parse("gitlab.com/owner/repo")
        dep_ref = DependencyReference.parse("owner/repo")
        assert pkg_ref.get_identity() != dep_ref.get_identity()


# ── only_packages filter ────────────────────────────────────────────────────


# ── HTTP (allow_insecure) ────────────────────────────────────────────────────


# HTTPS custom-port shorthand round-trip (#2203)
