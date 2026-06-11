"""Tests for air-gapped env var support in the apm self-update command.

Covers:
- _get_update_installer_url() using GITHUB_URL / APM_REPO env vars
- Version check respects GITHUB_URL / APM_REPO / VERSION via version_checker
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from click.testing import CliRunner

import apm_cli.commands.self_update as update_module
from apm_cli.cli import cli


class TestInstallerUrlAirGap:
    """_get_update_installer_url honours GITHUB_URL and APM_REPO."""

    def _clean_env(self) -> dict[str, str]:
        mirrored_vars = {
            "GITHUB_URL",
            "APM_REPO",
            "APM_INSTALLER_BASE_URL",
            "APM_RELEASE_BASE_URL",
            "APM_RELEASE_METADATA_URL",
            "APM_PYPI_INDEX_URL",
            "APM_NO_DIRECT_FALLBACK",
            "VERSION",
        }
        return {k: v for k, v in os.environ.items() if k not in mirrored_vars}

    def test_default_unix_url_no_env_vars(self) -> None:
        """Without env vars, returns the public aka.ms shortlink (Unix)."""
        from urllib.parse import urlparse

        env = self._clean_env()
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.hostname == "aka.ms"

    def test_custom_github_url_produces_raw_script_url(self) -> None:
        """With GITHUB_URL=https://gh.corp.com, installer URL targets that host."""
        from urllib.parse import urlparse

        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.hostname == "gh.corp.com"

    def test_custom_github_url_and_repo_in_script_url(self) -> None:
        """With GITHUB_URL and APM_REPO set, both appear in the installer URL."""
        from urllib.parse import urlparse

        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        env["APM_REPO"] = "corp/apm-fork"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.hostname == "gh.corp.com"
        assert "corp/apm-fork" in parsed.path

    def test_custom_github_url_windows_uses_ps1(self) -> None:
        """On Windows with custom GITHUB_URL, installer URL ends in install.ps1."""
        from urllib.parse import urlparse

        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.hostname == "gh.corp.com"
        assert parsed.path.endswith("install.ps1")

    def test_custom_github_url_unix_uses_sh(self) -> None:
        """On Unix with custom GITHUB_URL, installer URL ends in install.sh."""
        from urllib.parse import urlparse

        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.path.endswith("install.sh")

    def test_github_url_with_trailing_slash_is_normalised(self) -> None:
        """GITHUB_URL with a trailing slash must not produce double-slash in the URL."""
        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com/"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        assert "//" not in url.split("://", 1)[1], f"Double slash in URL: {url}"

    def test_default_windows_url_no_env_vars(self) -> None:
        """Without env vars on Windows, returns the public aka.ms shortlink."""
        from urllib.parse import urlparse

        env = self._clean_env()
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()
        parsed = urlparse(url)
        assert parsed.hostname == "aka.ms"

    def test_installer_base_url_unix_uses_mirror_script(self) -> None:
        """APM_INSTALLER_BASE_URL routes Unix self-update installer downloads to the mirror."""
        env = self._clean_env()
        env["APM_INSTALLER_BASE_URL"] = "https://mirror.corp.example/apm-install/"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "mirror.corp.example"
        assert parsed.path == "/apm-install/install.sh"

    def test_installer_base_url_windows_uses_mirror_script(self) -> None:
        """APM_INSTALLER_BASE_URL routes Windows self-update installer downloads to the mirror."""
        env = self._clean_env()
        env["APM_INSTALLER_BASE_URL"] = "https://mirror.corp.example/apm-install"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ):
                from apm_cli.commands.self_update import _get_update_installer_url

                url = _get_update_installer_url()

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "mirror.corp.example"
        assert parsed.path == "/apm-install/install.ps1"

    def test_manual_command_no_direct_fallback_without_base_gives_action_not_pseudo_url(
        self,
    ) -> None:
        """Fail-closed manual guidance should tell users to set the mirror base."""
        env = self._clean_env()
        env["APM_NO_DIRECT_FALLBACK"] = "1"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                command = update_module._get_manual_update_command()

        assert (
            command
            == "Set APM_INSTALLER_BASE_URL to your mirror base URL, then re-run 'apm self-update'."
        )
        assert "APM_INSTALLER_BASE_URL/" not in command

    def test_manual_command_no_direct_fallback_unix_uses_env_reference(self) -> None:
        """Fail-closed manual Unix guidance should reference the mirror env var."""
        env = self._clean_env()
        env["APM_NO_DIRECT_FALLBACK"] = "1"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                command = update_module._get_manual_update_command()

        assert (
            command
            == "Set APM_INSTALLER_BASE_URL to your mirror base URL, then re-run 'apm self-update'."
        )

    def test_manual_command_no_direct_fallback_windows_uses_env_reference(self) -> None:
        """Fail-closed manual Windows guidance should reference the mirror env var."""
        env = self._clean_env()
        env["APM_NO_DIRECT_FALLBACK"] = "1"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ):
                command = update_module._get_manual_update_command()

        assert (
            command
            == "Set APM_INSTALLER_BASE_URL to your mirror base URL, then re-run 'apm self-update'."
        )

    def test_manual_command_mirror_base_does_not_print_credentials(self) -> None:
        """Manual guidance should not echo credentials embedded in mirror URLs."""
        env = self._clean_env()
        env["APM_INSTALLER_BASE_URL"] = "https://user:secret@mirror.corp.example/apm-install"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                command = update_module._get_manual_update_command()

        assert command == 'curl -sSL "$APM_INSTALLER_BASE_URL/install.sh" | sh'
        assert "secret" not in command


class TestEnterpriseBootstrapSelfUpdate:
    """Self-update honours enterprise bootstrap mirrors and fail-closed mode."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        self.scratch = Path(".test-artifacts") / "self-update-enterprise"
        if self.scratch.exists():
            shutil.rmtree(self.scratch)
        self.scratch.mkdir(parents=True)

    def teardown_method(self) -> None:
        if self.scratch.exists():
            shutil.rmtree(self.scratch)

    def _clean_env(self) -> dict[str, str]:
        mirrored_vars = {
            "GITHUB_URL",
            "APM_REPO",
            "APM_INSTALLER_BASE_URL",
            "APM_RELEASE_BASE_URL",
            "APM_RELEASE_METADATA_URL",
            "APM_PYPI_INDEX_URL",
            "APM_NO_DIRECT_FALLBACK",
            "VERSION",
            "APM_TEMP_DIR",
        }
        return {k: v for k, v in os.environ.items() if k not in mirrored_vars}

    def test_self_update_help_lists_enterprise_mirror_env_vars(self) -> None:
        """Users can discover enterprise mirror env vars from self-update help."""
        result = self.runner.invoke(cli, ["self-update", "--help"])

        assert result.exit_code == 0
        for name in (
            "APM_RELEASE_BASE_URL",
            "APM_RELEASE_METADATA_URL",
            "APM_INSTALLER_BASE_URL",
            "APM_PYPI_INDEX_URL",
            "APM_NO_DIRECT_FALLBACK",
        ):
            assert name in result.output

    def test_no_direct_fallback_blocks_public_metadata_lookup(self) -> None:
        """APM_NO_DIRECT_FALLBACK refuses public latest-release lookup without a mirror."""
        env = self._clean_env()
        env["APM_NO_DIRECT_FALLBACK"] = "1"
        env["APM_TEMP_DIR"] = str(self.scratch)

        with (
            patch.dict("os.environ", env, clear=True),
            patch("requests.get") as mock_get,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
        ):
            result = self.runner.invoke(cli, ["self-update", "--check"])

        assert result.exit_code == 1
        assert "APM_NO_DIRECT_FALLBACK" in result.output
        assert "APM_RELEASE_METADATA_URL" in result.output
        mock_get.assert_not_called()

    def test_self_update_uses_mirrors_without_public_hosts(self) -> None:
        """Mirror env vars keep self-update metadata and installer requests off public hosts."""
        env = self._clean_env()
        env.update(
            {
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_RELEASE_METADATA_URL": "https://mirror.corp.example/apm/latest.json",
                "APM_INSTALLER_BASE_URL": "https://mirror.corp.example/apm/installers",
                "APM_TEMP_DIR": str(self.scratch),
            }
        )
        requested_urls: list[str] = []

        def fake_get(url: str, **_kwargs: object) -> Mock:
            requested_urls.append(url)
            parsed = urlparse(url)
            if parsed.hostname != "mirror.corp.example":
                raise AssertionError(f"unexpected public request: {url}")
            response = Mock()
            response.status_code = 200
            response.raise_for_status.return_value = None
            if parsed.path == "/apm/latest.json":
                response.json.return_value = {"tag_name": "v1.1.0"}
            elif parsed.path == "/apm/installers/install.sh":
                response.text = "echo install"
            else:
                raise AssertionError(f"unexpected mirror path: {parsed.path}")
            return response

        with (
            patch.dict("os.environ", env, clear=True),
            patch("requests.get", side_effect=fake_get),
            patch("subprocess.run", return_value=Mock(returncode=0)) as mock_run,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
            patch("apm_cli.commands.self_update.os.chmod"),
            patch.object(update_module.sys, "platform", "linux"),
            patch("apm_cli.commands.self_update.os.path.exists", return_value=True),
        ):
            result = self.runner.invoke(cli, ["self-update"])

        assert result.exit_code == 0
        assert "Successfully updated to version 1.1.0" in result.output
        assert {urlparse(url).hostname for url in requested_urls} == {"mirror.corp.example"}
        assert [urlparse(url).path for url in requested_urls] == [
            "/apm/latest.json",
            "/apm/installers/install.sh",
        ]
        mock_run.assert_called_once()
