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

import pytest
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

    def test_custom_github_url_uses_supplied_script_ref(self) -> None:
        """Supplying a script ref points installer downloads to that tag path."""
        from urllib.parse import urlparse

        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        env["APM_REPO"] = "corp/apm"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ):
                from apm_cli.commands.self_update import (
                    _get_update_installer_url,
                    _resolve_self_update_release,
                )

                url = _get_update_installer_url(_resolve_self_update_release("v1.2.3"))
        parsed = urlparse(url)
        assert parsed.path == "/corp/apm/raw/v1.2.3/install.sh"

    def test_public_github_uses_supplied_release_on_unix(self) -> None:
        """A resolved public release must bypass the moving aka.ms installer."""
        env = self._clean_env()
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ),
        ):
            release = update_module._resolve_self_update_release("1.2.3")
            url = update_module._get_update_installer_url(release)

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "github.com"
        assert parsed.path == "/microsoft/apm/raw/v1.2.3/install.sh"

    def test_public_github_uses_supplied_release_on_windows(self) -> None:
        """Windows must download the PowerShell installer from the same tag."""
        env = self._clean_env()
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ),
        ):
            release = update_module._resolve_self_update_release("v1.2.3")
            url = update_module._get_update_installer_url(release)

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "github.com"
        assert parsed.path == "/microsoft/apm/raw/v1.2.3/install.ps1"

    def test_custom_github_without_release_preserves_main_fallback(self) -> None:
        """The documented current-ref fallback remains available before discovery."""
        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        env["APM_REPO"] = "corp/apm"
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ),
        ):
            url = update_module._get_update_installer_url()

        parsed = urlparse(url)
        assert parsed.hostname == "gh.corp.com"
        assert parsed.path == "/corp/apm/raw/main/install.sh"

    @pytest.mark.parametrize(
        ("raw_version", "expected_version", "expected_tag"),
        (
            ("1.2.3", "1.2.3", "v1.2.3"),
            ("v1.2.3", "1.2.3", "v1.2.3"),
            ("2.0.0rc1", "2.0.0rc1", "v2.0.0rc1"),
        ),
    )
    def test_release_normalization_adds_v_exactly_once(
        self,
        raw_version: str,
        expected_version: str,
        expected_tag: str,
    ) -> None:
        """Installer URL and environment share one normalized release object."""
        release = update_module._resolve_self_update_release(raw_version)

        assert release.version == expected_version
        assert release.tag == expected_tag

    @pytest.mark.parametrize(
        "raw_version",
        ("", "main", "vv1.2.3", "v1.2", "v1.2.3/../../main", " v1.2.3"),
    )
    def test_release_normalization_rejects_malformed_refs(self, raw_version: str) -> None:
        """Only release-version tags may reach installer URL construction."""
        with pytest.raises(ValueError, match="Invalid self-update release version"):
            update_module._resolve_self_update_release(raw_version)

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

    def test_installer_base_url_ignores_resolved_github_ref(self) -> None:
        """An operator mirror remains authoritative after release discovery."""
        env = self._clean_env()
        env["APM_INSTALLER_BASE_URL"] = "https://mirror.corp.example/apm-install"
        env["GITHUB_URL"] = "https://gh.corp.example"
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ),
        ):
            release = update_module._resolve_self_update_release("v1.2.3")
            url = update_module._get_update_installer_url(release)

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "mirror.corp.example"
        assert parsed.path == "/apm-install/install.sh"

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

        assert command == "Set APM_INSTALLER_BASE_URL=<mirror> and re-run: apm self-update"
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

        assert command == "Set APM_INSTALLER_BASE_URL=<mirror> and re-run: apm self-update"

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

        assert command == "Set APM_INSTALLER_BASE_URL=<mirror> and re-run: apm self-update"

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

    def test_manual_command_unix_keeps_resolved_release(self) -> None:
        """Unix recovery guidance must not fall back to main after discovery."""
        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        env["APM_REPO"] = "corp/apm"
        release = update_module._resolve_self_update_release("1.2.3")
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=False,
            ),
        ):
            command = update_module._get_manual_update_command(release)

        assert command == (
            'curl -sSL "https://gh.corp.com/corp/apm/raw/v1.2.3/install.sh" '
            "| env VERSION='v1.2.3' sh"
        )

    def test_manual_command_windows_keeps_resolved_release(self) -> None:
        """Windows recovery guidance must pass the exact selected VERSION."""
        env = self._clean_env()
        env["GITHUB_URL"] = "https://gh.corp.com"
        env["APM_REPO"] = "corp/apm"
        release = update_module._resolve_self_update_release("v1.2.3")
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "apm_cli.commands.self_update._is_windows_platform",
                return_value=True,
            ),
        ):
            command = update_module._get_manual_update_command(release)

        assert command == (
            "$env:VERSION='v1.2.3'; powershell -ExecutionPolicy Bypass "
            "-c 'irm \"https://gh.corp.com/corp/apm/raw/v1.2.3/install.ps1\" | iex'"
        )


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
        assert (
            "Unable to fetch latest version from APM_RELEASE_METADATA_URL mirror" in result.output
        )
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
