"""Tests for air-gapped env var support in the apm self-update command.

Covers:
- _get_update_installer_url() using GITHUB_URL / APM_REPO env vars
- Version check respects GITHUB_URL / APM_REPO / VERSION via version_checker
"""

from __future__ import annotations

import os
from unittest.mock import patch


class TestInstallerUrlAirGap:
    """_get_update_installer_url honours GITHUB_URL and APM_REPO."""

    def _clean_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in ("GITHUB_URL", "APM_REPO")}

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
