"""Tests for apm self-update against enterprise bootstrap mirrors and fail-closed mode."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

import apm_cli.commands.self_update as update_module
from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-self-update-release-resolution"),
]


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
                # Disable the CLI-startup update notifier so its cache-gated
                # metadata fetch (which fires on a cold Windows runner cache but
                # not on a warm Unix one) does not perturb the request list.
                "APM_E2E_TESTS": "1",
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

    def test_self_update_pins_installer_ref_to_latest_version(self) -> None:
        """Self-update downloads installer from the exact latest release ref."""
        env = self._clean_env()
        env.update(
            {
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_RELEASE_METADATA_URL": "https://mirror.corp.example/apm/latest.json",
                "GITHUB_URL": "https://gh.corp.com",
                "APM_TEMP_DIR": str(self.scratch),
                "APM_REPO": "corp/apm",
                "APM_E2E_TESTS": "1",
            }
        )
        requested_urls: list[str] = []
        target_version = "1.1.0"
        expected_installer_path = f"/corp/apm/raw/v{target_version}/install.sh"

        def fake_get(url: str, **_kwargs: object) -> Mock:
            requested_urls.append(url)
            parsed = urlparse(url)
            if parsed.hostname == "mirror.corp.example":
                if parsed.path == "/apm/latest.json":
                    response = Mock()
                    response.status_code = 200
                    response.raise_for_status.return_value = None
                    response.json.return_value = {"tag_name": f"v{target_version}"}
                    return response
                raise AssertionError(f"unexpected mirror request: {url}")

            if parsed.hostname == "gh.corp.com":
                if parsed.path == expected_installer_path:
                    response = Mock()
                    response.status_code = 200
                    response.raise_for_status.return_value = None
                    response.text = "echo install"
                    return response
                raise AssertionError(f"unexpected github request: {url}")

            raise AssertionError(f"unexpected request: {url}")

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
        parsed_paths = [urlparse(url).path for url in requested_urls]
        assert parsed_paths == [
            "/apm/latest.json",
            expected_installer_path,
        ]
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["env"]["VERSION"] == f"v{target_version}"

    def test_explicit_version_controls_ref_and_normalized_installer_env(self) -> None:
        """VERSION skips discovery and controls both installer inputs."""
        env = self._clean_env()
        env.update(
            {
                "VERSION": "1.2.3",
                "GITHUB_URL": "https://gh.corp.com",
                "APM_REPO": "corp/apm",
                "APM_TEMP_DIR": str(self.scratch),
                "APM_E2E_TESTS": "1",
            }
        )

        with (
            patch.dict("os.environ", env, clear=True),
            patch("requests.get") as mock_get,
            patch("subprocess.run", return_value=Mock(returncode=0)) as mock_run,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
            patch("apm_cli.commands.self_update.os.chmod"),
            patch.object(update_module.sys, "platform", "linux"),
            patch("apm_cli.commands.self_update.os.path.exists", return_value=True),
        ):
            response = Mock()
            response.text = "echo install"
            response.raise_for_status.return_value = None
            mock_get.return_value = response
            result = self.runner.invoke(cli, ["self-update"])

        assert result.exit_code == 0
        mock_get.assert_called_once()
        parsed = urlparse(mock_get.call_args.args[0])
        assert parsed.hostname == "gh.corp.com"
        assert parsed.path == "/corp/apm/raw/v1.2.3/install.sh"
        assert mock_run.call_args.kwargs["env"]["VERSION"] == "v1.2.3"

    def test_malformed_explicit_version_fails_without_network_or_install(self) -> None:
        """Malformed VERSION input must fail before download or execution."""
        env = self._clean_env()
        env.update(
            {
                "VERSION": "vv1.2.3",
                "APM_TEMP_DIR": str(self.scratch),
                "APM_E2E_TESTS": "1",
            }
        )

        with (
            patch.dict("os.environ", env, clear=True),
            patch("requests.get") as mock_get,
            patch("subprocess.run") as mock_run,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
        ):
            result = self.runner.invoke(cli, ["self-update"])

        assert result.exit_code == 1
        assert "Invalid VERSION value" in result.output
        mock_get.assert_not_called()
        mock_run.assert_not_called()

    def test_malformed_mirror_release_fails_without_installer_download(self) -> None:
        """Malformed release metadata must not become an executable ref."""
        env = self._clean_env()
        env.update(
            {
                "APM_RELEASE_METADATA_URL": "https://mirror.corp.example/apm/latest.json",
                "APM_TEMP_DIR": str(self.scratch),
                "APM_E2E_TESTS": "1",
            }
        )
        response = Mock(status_code=200)
        response.json.return_value = {"tag_name": "main"}

        with (
            patch.dict("os.environ", env, clear=True),
            patch("requests.get", return_value=response) as mock_get,
            patch("subprocess.run") as mock_run,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
        ):
            result = self.runner.invoke(cli, ["self-update"])

        assert result.exit_code == 1
        assert "Invalid release metadata; expected a valid tag_name." in result.output
        assert mock_get.call_count == 1
        parsed = urlparse(mock_get.call_args.args[0])
        assert parsed.hostname == "mirror.corp.example"
        assert parsed.path == "/apm/latest.json"
        mock_run.assert_not_called()

    def test_installer_network_error_fails_without_subprocess(self) -> None:
        """A failed installer download must not fall through to execution."""
        env = self._clean_env()
        env.update(
            {
                "GITHUB_URL": "https://gh.corp.com",
                "APM_REPO": "corp/apm",
                "APM_TEMP_DIR": str(self.scratch),
                "APM_E2E_TESTS": "1",
            }
        )

        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update.get_latest_version_for_self_update",
                return_value="1.2.3",
            ),
            patch("requests.get", side_effect=OSError("network unavailable")),
            patch("subprocess.run") as mock_run,
            patch("apm_cli.commands.self_update.get_version", return_value="1.0.0"),
            patch.object(update_module.sys, "platform", "linux"),
        ):
            result = self.runner.invoke(cli, ["self-update"])

        assert result.exit_code == 1
        assert "Update failed: network unavailable" in result.output
        mock_run.assert_not_called()
