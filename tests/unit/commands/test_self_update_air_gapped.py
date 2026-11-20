"""Tests for air-gapped env var support in the apm self-update command.

Covers:
- _get_update_installer_url() using GITHUB_URL / APM_REPO env vars
- Version check respects GITHUB_URL / APM_REPO / VERSION via version_checker
"""

from __future__ import annotations

import os
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

import apm_cli.commands.self_update as update_module
from apm_cli.core.remediation import CLI_INSTALLER_UNREACHABLE

pytestmark = pytest.mark.unit


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

        assert command == CLI_INSTALLER_UNREACHABLE.sentence()
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

        assert command == CLI_INSTALLER_UNREACHABLE.sentence()

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

        assert command == CLI_INSTALLER_UNREACHABLE.sentence()

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
