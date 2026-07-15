"""Tests for normalize-on-write canonicalization and identity-based CLI matching.

Covers:
- DependencyReference.to_canonical() — Docker-style default-host stripping
- DependencyReference.get_identity() — identity without ref/alias
- DependencyReference.canonicalize() — static convenience method
- _validate_and_add_packages_to_apm_yml() — normalize-on-write + dedup
- uninstall identity matching
- only_packages filter in _install_apm_dependencies
"""

from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401
from urllib.parse import urlparse

import pytest

from apm_cli.models.apm_package import DependencyReference

# ── to_canonical() ──────────────────────────────────────────────────────────


class TestToCanonical:
    """Test DependencyReference.to_canonical() method."""

    def test_shorthand_github(self):
        """Shorthand owner/repo stays as-is (default host stripped)."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_shorthand_with_ref(self):
        """Shorthand with ref preserves the ref."""
        dep = DependencyReference.parse("microsoft/apm-sample-package#v1.0")
        assert dep.to_canonical() == "microsoft/apm-sample-package#v1.0"

    def test_shorthand_alias_rejected(self):
        """Shorthand @alias syntax is rejected with a migration error."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("microsoft/apm-sample-package@my-alias")

    def test_shorthand_with_ref_and_alias_rejected(self):
        """Shorthand #ref@alias is rejected with a migration error."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("microsoft/apm-sample-package#main@my-alias")

    def test_shorthand_with_subpath_and_alias_rejected(self):
        """Subpath + @alias rejected loudly (was the silent-miscoercion bug)."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("stablyai/orca/skills/orchestration@orca-stration")

    def test_shorthand_with_deeper_subpath_and_alias_rejected(self):
        """Multi-segment subpath + @alias is also rejected."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo/skills/foo/deeper@my-alias")

    def test_shorthand_with_subpath_ref_and_alias_rejected(self):
        """All four parts (subpath + #ref + @alias) trip the same uniform error."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo/skills/foo#main@my-alias")

    def test_fqdn_shorthand_with_alias_rejected(self):
        """FQDN shorthand + @alias is rejected (covers the non-nested-group FQDN path)."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("github.com/owner/repo@my-alias")

    def test_url_encoded_at_in_alias_shorthand_rejected(self):
        """Percent-encoded ``@`` in shorthand is also rejected."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo%40my-alias")

    def test_trailing_at_with_no_alias_rejected(self):
        """A bare trailing ``@`` is rejected (not silently stripped)."""
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo@")

    def test_https_with_embedded_credentials_parses(self):
        """Guard must not fire on HTTPS userinfo (regression: don't over-reject ``@``)."""
        dep = DependencyReference.parse("https://user@github.com/owner/repo.git")
        assert dep.repo_url == "owner/repo"
        assert dep.alias is None

    def test_fqdn_github(self):
        """FQDN with default host strips the host."""
        dep = DependencyReference.parse("github.com/microsoft/apm-sample-package")
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_fqdn_github_with_ref(self):
        """FQDN with default host + ref strips host, keeps ref."""
        dep = DependencyReference.parse("github.com/microsoft/apm-sample-package#main")
        assert dep.to_canonical() == "microsoft/apm-sample-package#main"

    def test_https_github(self):
        """HTTPS GitHub URL normalizes to shorthand."""
        dep = DependencyReference.parse("https://github.com/microsoft/apm-sample-package.git")
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_https_github_with_ref(self):
        """HTTPS GitHub URL with ref normalizes to shorthand#ref."""
        dep = DependencyReference.parse("https://github.com/microsoft/apm-sample-package.git#v2.0")
        assert dep.to_canonical() == "microsoft/apm-sample-package#v2.0"

    def test_ssh_github(self):
        """SSH GitHub URL normalizes to shorthand."""
        dep = DependencyReference.parse("git@github.com:microsoft/apm-sample-package.git")
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_ssh_protocol_github(self):
        """SSH protocol GitHub URL normalizes to shorthand."""
        dep = DependencyReference.parse("ssh://git@github.com/microsoft/apm-sample-package.git")
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_fqdn_gitlab(self):
        """Non-default host is preserved in canonical form."""
        dep = DependencyReference.parse("gitlab.com/acme/standards")
        assert dep.to_canonical() == "gitlab.com/acme/standards"

    def test_https_gitlab(self):
        """HTTPS GitLab URL normalizes to host/owner/repo."""
        dep = DependencyReference.parse("https://gitlab.com/acme/standards.git")
        assert dep.to_canonical() == "gitlab.com/acme/standards"

    def test_ssh_gitlab(self):
        """SSH GitLab URL normalizes to host/owner/repo."""
        dep = DependencyReference.parse("git@gitlab.com:acme/standards.git")
        assert dep.to_canonical() == "gitlab.com/acme/standards"

    def test_ssh_protocol_gitlab(self):
        """SSH protocol GitLab URL normalizes to host/owner/repo."""
        dep = DependencyReference.parse("ssh://git@gitlab.com/acme/standards.git")
        assert dep.to_canonical() == "gitlab.com/acme/standards"

    def test_gitlab_with_ref(self):
        """Non-default host + ref preserves both."""
        dep = DependencyReference.parse("gitlab.com/acme/standards#v2.0")
        assert dep.to_canonical() == "gitlab.com/acme/standards#v2.0"

    def test_https_gitlab_with_ref(self):
        """HTTPS non-default + ref normalizes correctly."""
        dep = DependencyReference.parse("https://gitlab.com/acme/standards.git#release-1")
        assert dep.to_canonical() == "gitlab.com/acme/standards#release-1"

    def test_bitbucket(self):
        """Bitbucket preserves host."""
        dep = DependencyReference.parse("bitbucket.org/team/rules")
        assert dep.to_canonical() == "bitbucket.org/team/rules"

    def test_ssh_bitbucket(self):
        """SSH Bitbucket normalizes with host."""
        dep = DependencyReference.parse("git@bitbucket.org:team/rules.git")
        assert dep.to_canonical() == "bitbucket.org/team/rules"

    def test_default_github_host_stripped(self):
        """Default GitHub host (github.com) is stripped from canonical form."""
        dep = DependencyReference.parse("github.com/microsoft/apm-sample-package")
        # github.com is the default, so stripped
        assert dep.to_canonical() == "microsoft/apm-sample-package"

    def test_virtual_path_github(self):
        """Virtual path on default host preserves path but strips host."""
        dep = DependencyReference.parse("microsoft/apm-sample-package/prompts/review.prompt.md")
        assert dep.to_canonical() == "microsoft/apm-sample-package/prompts/review.prompt.md"

    def test_virtual_path_non_default_host(self):
        """Virtual path on non-default host preserves both host and path."""
        dep = DependencyReference.parse("gitlab.com/acme/standards/prompts/review.prompt.md")
        assert dep.to_canonical() == "gitlab.com/acme/standards/prompts/review.prompt.md"


# ── get_identity() ──────────────────────────────────────────────────────────


class TestGetIdentity:
    """Test DependencyReference.get_identity() — identity without ref/alias."""

    def test_shorthand(self):
        dep = DependencyReference.parse("owner/repo")
        assert dep.get_identity() == "owner/repo"

    def test_shorthand_with_ref(self):
        """Ref is stripped from identity."""
        dep = DependencyReference.parse("owner/repo#v1.0")
        assert dep.get_identity() == "owner/repo"

    # Shorthand @alias rejection is covered in TestToCanonical; parse() raises
    # before get_identity() runs, so duplicating the cases here would prove
    # nothing about identity semantics.

    def test_fqdn_github(self):
        """Default host is stripped from identity."""
        dep = DependencyReference.parse("github.com/owner/repo")
        assert dep.get_identity() == "owner/repo"

    def test_fqdn_gitlab(self):
        """Non-default host is preserved in identity."""
        dep = DependencyReference.parse("gitlab.com/owner/repo")
        assert dep.get_identity() == "gitlab.com/owner/repo"

    def test_https_github(self):
        """HTTPS default host stripped from identity."""
        dep = DependencyReference.parse("https://github.com/owner/repo.git")
        assert dep.get_identity() == "owner/repo"

    def test_https_gitlab(self):
        """HTTPS non-default host preserved in identity."""
        dep = DependencyReference.parse("https://gitlab.com/owner/repo.git")
        assert dep.get_identity() == "gitlab.com/owner/repo"

    def test_ssh_github(self):
        """SSH default host stripped."""
        dep = DependencyReference.parse("git@github.com:owner/repo.git")
        assert dep.get_identity() == "owner/repo"

    def test_ssh_gitlab(self):
        """SSH non-default host preserved."""
        dep = DependencyReference.parse("git@gitlab.com:owner/repo.git")
        assert dep.get_identity() == "gitlab.com/owner/repo"

    def test_virtual_path(self):
        """Virtual path included in identity."""
        dep = DependencyReference.parse("owner/repo/prompts/review.prompt.md")
        assert dep.get_identity() == "owner/repo/prompts/review.prompt.md"

    def test_gitlab_virtual_with_ref(self):
        """Non-default host + virtual path + ref: ref stripped, rest preserved."""
        dep = DependencyReference.parse("gitlab.com/acme/rules/prompts/review.prompt.md#v2")
        assert dep.get_identity() == "gitlab.com/acme/rules/prompts/review.prompt.md"

    def test_same_identity_different_forms(self):
        """All input forms for the same package produce the same identity."""
        forms = [
            "microsoft/apm-sample-package",
            "github.com/microsoft/apm-sample-package",
            "https://github.com/microsoft/apm-sample-package.git",
            "git@github.com:microsoft/apm-sample-package.git",
            "ssh://git@github.com/microsoft/apm-sample-package.git",
            "microsoft/apm-sample-package#main",
        ]
        identities = {DependencyReference.parse(f).get_identity() for f in forms}
        assert len(identities) == 1, f"Expected 1 identity, got {identities}"
        assert identities == {"microsoft/apm-sample-package"}

    def test_different_hosts_different_identities(self):
        """Same owner/repo on different hosts = different identities."""
        gh = DependencyReference.parse("owner/repo")
        gl = DependencyReference.parse("gitlab.com/owner/repo")
        assert gh.get_identity() != gl.get_identity()


# ── canonicalize() static method ────────────────────────────────────────────


class TestCanonicalize:
    """Test DependencyReference.canonicalize() static convenience method."""

    def test_shorthand(self):
        assert DependencyReference.canonicalize("owner/repo") == "owner/repo"

    def test_https_github(self):
        assert DependencyReference.canonicalize("https://github.com/o/r.git") == "o/r"

    def test_ssh_gitlab(self):
        assert DependencyReference.canonicalize("git@gitlab.com:o/r.git") == "gitlab.com/o/r"

    def test_fqdn_with_ref(self):
        assert DependencyReference.canonicalize("github.com/o/r#v1") == "o/r#v1"

    def test_https_gitlab_with_ref(self):
        assert (
            DependencyReference.canonicalize("https://gitlab.com/o/r.git#main")
            == "gitlab.com/o/r#main"
        )


# ── backward compat: get_canonical_dependency_string() ──────────────────────


class TestGetCanonicalDependencyString:
    """Verify backward compat shim delegates to get_unique_key()."""

    def test_github_package(self):
        dep = DependencyReference.parse("owner/repo#v1.0")
        assert dep.get_canonical_dependency_string() == "owner/repo"

    def test_gitlab_package_still_host_blind(self):
        """get_canonical_dependency_string is host-blind (filesystem matching)."""
        dep = DependencyReference.parse("gitlab.com/owner/repo")
        # Host-blind: returns just owner/repo
        assert dep.get_canonical_dependency_string() == "owner/repo"

    def test_virtual_package(self):
        dep = DependencyReference.parse("owner/repo/prompts/review.prompt.md")
        assert dep.get_canonical_dependency_string() == "owner/repo/prompts/review.prompt.md"


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


class TestOnlyPackagesFilter:
    """Test identity-based filtering in _install_apm_dependencies."""

    def test_filter_matches_shorthand(self):
        """Shorthand filter matches a parsed dep with default host."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        filter_ref = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.get_identity() == filter_ref.get_identity()

    def test_filter_https_matches_shorthand_dep(self):
        """HTTPS URL filter matches shorthand-parsed dep."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        filter_ref = DependencyReference.parse(
            "https://github.com/microsoft/apm-sample-package.git"
        )
        assert dep.get_identity() == filter_ref.get_identity()

    def test_filter_shorthand_matches_https_dep(self):
        """Shorthand filter matches HTTPS-parsed dep."""
        dep = DependencyReference.parse("https://github.com/microsoft/apm-sample-package.git")
        filter_ref = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.get_identity() == filter_ref.get_identity()

    def test_filter_no_cross_host_match(self):
        """Filter for GitHub package does NOT match GitLab dep."""
        dep = DependencyReference.parse("gitlab.com/microsoft/apm-sample-package")
        filter_ref = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.get_identity() != filter_ref.get_identity()


# ── HTTP (allow_insecure) ────────────────────────────────────────────────────


class TestHttpInsecureDeps:
    """Tests for HTTP (insecure) dependency parsing and serialization."""

    def test_http_scheme_detection_is_case_insensitive(self):
        """Parsing an uppercase HTTP scheme still marks the ref as insecure."""
        dep = DependencyReference.parse("HTTP://my-server.example.com/owner/repo")
        assert dep.is_insecure is True

    def test_http_url_sets_insecure_flag(self):
        """Parsing an http:// URL marks the ref as insecure."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        assert dep.is_insecure is True
        assert dep.host == "my-server.example.com"
        assert dep.repo_url == "owner/repo"

    def test_https_url_is_not_insecure(self):
        """Parsing an https:// URL does not mark the ref as insecure."""
        dep = DependencyReference.parse("https://gitlab.com/owner/repo.git")
        assert dep.is_insecure is False

    def test_shorthand_is_not_insecure(self):
        """Parsing shorthand owner/repo does not mark the ref as insecure."""
        dep = DependencyReference.parse("owner/repo")
        assert dep.is_insecure is False

    def test_http_allow_insecure_default_false(self):
        """Freshly parsed HTTP dep has allow_insecure=False by default."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        assert dep.allow_insecure is False

    def test_http_to_canonical_is_scheme_free(self):
        """to_canonical() for HTTP dep keeps the canonical identifier scheme-free."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        canonical = dep.to_canonical()
        assert canonical == "my-server.example.com/owner/repo"

    def test_http_to_canonical_with_ref(self):
        """to_canonical() for HTTP dep with ref stays scheme-free."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo#main")
        canonical = dep.to_canonical()
        assert canonical == "my-server.example.com/owner/repo#main"

    def test_http_to_apm_yml_entry_returns_dict(self):
        """to_apm_yml_entry() for HTTP dep returns a dict with git key."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        dep.allow_insecure = True
        entry = dep.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert entry["git"] == "http://my-server.example.com/owner/repo"
        assert entry["allow_insecure"] is True

    def test_http_to_apm_yml_entry_preserves_allow_insecure_false(self):
        """to_apm_yml_entry() preserves an explicit False opt-in state."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        entry = dep.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert entry["allow_insecure"] is False

    def test_http_to_apm_yml_entry_includes_ref(self):
        """to_apm_yml_entry() includes ref when present."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo#v1.0")
        dep.allow_insecure = True
        entry = dep.to_apm_yml_entry()
        assert entry.get("ref") == "v1.0"
        assert "http://my-server.example.com/owner/repo" in entry["git"]

    def test_http_to_apm_yml_entry_preserves_custom_port(self):
        """Regression (#2202): a custom port must round-trip into apm.yml.

        The HTTP branch of to_apm_yml_entry() previously built the git URL
        from host alone, silently dropping the port so subsequent commands
        connected to the default port and failed.
        """
        dep = DependencyReference.parse("http://192.168.1.10:8080/owner/repo")
        dep.allow_insecure = True
        assert dep.port == 8080  # parsed correctly
        entry = dep.to_apm_yml_entry()
        persisted_url = urlparse(entry["git"])
        assert (persisted_url.scheme, persisted_url.hostname, persisted_url.port) == (
            "http",
            "192.168.1.10",
            8080,
        )

    def test_http_to_apm_yml_entry_custom_port_round_trips(self):
        """The persisted entry re-parses back to the same host and port (#2202)."""
        original = "http://192.168.1.10:8080/owner/repo"
        dep = DependencyReference.parse(original)
        dep.allow_insecure = True
        reparsed = DependencyReference.parse_from_dict(dep.to_apm_yml_entry())
        assert reparsed.host == "192.168.1.10"
        assert reparsed.port == 8080
        input_url = urlparse(original)
        reloaded_url = urlparse(reparsed.to_github_url())
        assert (
            reloaded_url.scheme,
            reloaded_url.hostname,
            reloaded_url.port,
            reloaded_url.path,
        ) == (input_url.scheme, input_url.hostname, input_url.port, input_url.path)

    def test_https_to_apm_yml_entry_returns_string(self):
        """to_apm_yml_entry() for HTTPS dep returns canonical string (not dict)."""
        dep = DependencyReference.parse("owner/repo")
        entry = dep.to_apm_yml_entry()
        assert isinstance(entry, str)
        assert entry == "owner/repo"

    def test_parse_from_dict_git_http(self):
        """parse_from_dict() supports git: http://... for HTTP deps."""
        entry = {"git": "http://my-server.example.com/owner/repo", "allow_insecure": True}
        dep = DependencyReference.parse_from_dict(entry)
        assert dep.is_insecure is True
        assert dep.allow_insecure is True
        assert dep.repo_url == "owner/repo"
        assert dep.host == "my-server.example.com"

    def test_parse_from_dict_git_http_with_ref(self):
        """parse_from_dict() reads ref from dict with git key."""
        entry = {
            "git": "http://my-server.example.com/owner/repo",
            "ref": "main",
            "allow_insecure": True,
        }
        dep = DependencyReference.parse_from_dict(entry)
        assert dep.reference == "main"

    def test_parse_from_dict_git_http_allow_insecure_default_false(self):
        """parse_from_dict() with git http URL defaults allow_insecure to False."""
        entry = {"git": "http://my-server.example.com/owner/repo"}
        dep = DependencyReference.parse_from_dict(entry)
        assert dep.allow_insecure is False

    def test_parse_from_dict_rejects_non_boolean_allow_insecure(self):
        """parse_from_dict() rejects non-boolean allow_insecure values."""
        entry = {
            "git": "http://my-server.example.com/owner/repo",
            "allow_insecure": "false",
        }
        with pytest.raises(ValueError, match="'allow_insecure' field must be a boolean"):
            DependencyReference.parse_from_dict(entry)

    def test_http_to_github_url_uses_http_scheme(self):
        """to_github_url() uses http:// for HTTP deps."""
        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        url = dep.to_github_url()
        assert url.startswith("http://")
        assert "my-server.example.com/owner/repo" in url

    def test_https_to_github_url_uses_https_scheme(self):
        """to_github_url() still uses https:// for HTTPS deps."""
        dep = DependencyReference.parse("https://gitlab.com/owner/repo.git")
        url = dep.to_github_url()
        assert url.startswith("https://")

    def test_http_identity_scheme_agnostic(self):
        """HTTP and HTTPS deps to the same host/repo have the same identity."""
        http_dep = DependencyReference.parse("http://gitlab.com/owner/repo")
        https_dep = DependencyReference.parse("https://gitlab.com/owner/repo.git")
        # Identity includes host but not scheme, so they are the same package
        assert http_dep.get_identity() == https_dep.get_identity()


# HTTPS custom-port shorthand round-trip (#2203)


class TestHttpsCustomPortShorthand:
    """The apm.yml entry APM writes for a custom-port HTTPS dep must re-parse.

    to_canonical()/get_identity() emit the scheme-free ``host:port/owner/repo``
    form for a non-default-port HTTPS dependency. Before the fix the shorthand
    parser rejected that form, so any command re-reading apm.yml failed on an
    entry APM itself wrote.
    """

    def test_shorthand_with_custom_port_parses(self):
        """Bare ``host:port/owner/repo`` restores host and port."""
        dep = DependencyReference.parse("git.example.com:8443/owner/repo")
        assert dep.host == "git.example.com"
        assert dep.port == 8443
        assert dep.repo_url == "owner/repo"

    def test_apm_yml_entry_round_trips(self):
        """Manifest write/reparse preserves the backend HTTPS URL (#2203)."""
        dep = DependencyReference.parse("https://git.example.com:8443/owner/repo")
        entry = dep.to_apm_yml_entry()
        assert entry == "git.example.com:8443/owner/repo"
        reparsed = DependencyReference.parse(entry)
        assert reparsed.host == "git.example.com"
        assert reparsed.port == 8443
        backend_url = urlparse(reparsed.to_clone_url())
        assert (
            backend_url.scheme,
            backend_url.hostname,
            backend_url.port,
            backend_url.path,
        ) == ("https", "git.example.com", 8443, "/owner/repo")
        # Serialization is idempotent: what APM writes, it can read and rewrite.
        assert reparsed.to_canonical() == entry

    def test_identity_round_trips(self):
        """get_identity() (uninstall/dedup key) also re-parses with its port."""
        dep = DependencyReference.parse("https://git.example.com:8443/owner/repo")
        reparsed = DependencyReference.parse(dep.get_identity())
        assert reparsed.port == 8443
        assert reparsed.get_identity() == dep.get_identity()

    def test_shorthand_with_ref_and_custom_port(self):
        """A trailing ``#ref`` still parses alongside a custom port."""
        dep = DependencyReference.parse("git.example.com:8443/owner/repo#v1.0")
        assert dep.host == "git.example.com"
        assert dep.port == 8443
        assert dep.reference == "v1.0"

    def test_redundant_https_default_port_is_normalized(self):
        """``host:443`` normalizes to no port, matching the URL-form parser."""
        dep = DependencyReference.parse("git.example.com:443/owner/repo")
        assert dep.port is None
        assert dep.to_canonical() == "git.example.com/owner/repo"
        backend_url = urlparse(dep.to_clone_url())
        assert (backend_url.scheme, backend_url.hostname, backend_url.port) == (
            "https",
            "git.example.com",
            None,
        )

    @pytest.mark.parametrize("port", ["", "0", "65536", "not-a-port"])
    def test_malformed_shorthand_port_is_rejected(self, port):
        """Malformed ports cannot be reinterpreted as a different dependency."""
        with pytest.raises(ValueError, match=r"Invalid shorthand port"):
            DependencyReference.parse(f"git.example.com:{port}/owner/repo")

    def test_non_ascii_shorthand_port_error_is_printable_ascii(self):
        """Untrusted port text cannot escape into the rendered parser error."""
        with pytest.raises(ValueError) as exc_info:
            DependencyReference.parse("git.example.com:\U0001f680/owner/repo")

        message = str(exc_info.value)
        assert (message, message.isascii(), message.isprintable()) == (
            "Invalid shorthand port. Expected an integer from 1 to 65535",
            True,
            True,
        )

    def test_plain_shorthand_has_no_port(self):
        """Portless shorthand is unaffected by the port-splitting branch."""
        assert DependencyReference.parse("owner/repo").port is None
        assert DependencyReference.parse("github.com/owner/repo").port is None
