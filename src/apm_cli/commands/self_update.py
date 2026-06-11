"""APM self-update command (renamed from ``update`` in #1203).

This is the platform-aware self-updater for the APM CLI binary itself.
The command is exposed as ``apm self-update`` to free the ``apm update``
verb for dependency-graph refresh (the package-manager convention).

A back-compat shim in :mod:`apm_cli.cli` keeps ``apm update`` working as
a deprecated alias when invoked outside an APM project (no ``apm.yml``
in the current directory).
"""

import os
import shutil
import sys

import click

from ..bootstrap_mirror import (
    append_url_path,
    get_installer_base_url,
    get_release_metadata_url,
    installer_public_download_blocked,
    release_metadata_public_lookup_blocked,
)
from ..core.command_logger import CommandLogger
from ..update_policy import get_self_update_disabled_message, is_self_update_enabled
from ..utils.subprocess_env import external_process_env
from ..version import get_version

_DEFAULT_GITHUB_URL = "https://github.com"
_DEFAULT_APM_REPO = "microsoft/apm"
_INSTALL_SCRIPT_REF = "main"


def _is_windows_platform() -> bool:
    """Return True when running on native Windows."""
    return sys.platform == "win32"


def _get_update_installer_url() -> str:
    """Return the installer URL for the current platform, respecting mirror env vars.

    ``APM_INSTALLER_BASE_URL`` wins when configured, using ``install.sh`` on
    Unix and ``install.ps1`` on Windows under that base URL. Otherwise existing
    behaviour is preserved: public GitHub uses the aka.ms shortlinks, and a
    custom ``GITHUB_URL`` uses the raw repository path.
    """
    github_url = os.environ.get("GITHUB_URL", _DEFAULT_GITHUB_URL).rstrip("/")
    apm_repo = os.environ.get("APM_REPO", _DEFAULT_APM_REPO)
    script_name = "install.ps1" if _is_windows_platform() else "install.sh"

    installer_base_url = get_installer_base_url()
    if installer_base_url is not None:
        return append_url_path(installer_base_url, script_name)

    if installer_public_download_blocked(github_url):
        raise RuntimeError(
            "APM_NO_DIRECT_FALLBACK is set, but APM_INSTALLER_BASE_URL is not configured. "
            "Set APM_INSTALLER_BASE_URL to a mirror containing install.sh/install.ps1."
        )

    if github_url == _DEFAULT_GITHUB_URL:
        return "https://aka.ms/apm-windows" if _is_windows_platform() else "https://aka.ms/apm-unix"

    return f"{github_url}/{apm_repo}/raw/{_INSTALL_SCRIPT_REF}/{script_name}"


def _get_update_installer_suffix() -> str:
    """Return the file suffix for the downloaded installer script."""
    return ".ps1" if _is_windows_platform() else ".sh"


def _get_manual_update_command() -> str:
    """Return the manual update action for the current platform."""
    if _is_windows_platform():
        if get_installer_base_url() is not None:
            installer_url = "$env:APM_INSTALLER_BASE_URL/install.ps1"
        else:
            try:
                installer_url = _get_update_installer_url()
            except RuntimeError:
                return "Set APM_INSTALLER_BASE_URL and re-run 'apm self-update'."
        return f"powershell -ExecutionPolicy Bypass -c 'irm \"{installer_url}\" | iex'"

    if get_installer_base_url() is not None:
        installer_url = "$APM_INSTALLER_BASE_URL/install.sh"
    else:
        try:
            installer_url = _get_update_installer_url()
        except RuntimeError:
            return "Set APM_INSTALLER_BASE_URL and re-run 'apm self-update'."
    return f'curl -sSL "{installer_url}" | sh'


def _log_no_direct_metadata_error(logger: CommandLogger) -> None:
    """Emit the actionable fail-closed metadata configuration error."""
    logger.error("APM_NO_DIRECT_FALLBACK is set, but no release metadata mirror is configured.")
    logger.info(
        "Set APM_RELEASE_METADATA_URL to mirrored latest.json, or set VERSION to a pinned release."
    )


def _get_installer_run_command(script_path: str) -> list[str]:
    """Return the installer execution command for the current platform."""
    if _is_windows_platform():
        powershell_path = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell_path:
            raise FileNotFoundError("PowerShell executable not found in PATH")
        return [powershell_path, "-ExecutionPolicy", "Bypass", "-File", script_path]

    shell_path = "/bin/sh" if os.path.exists("/bin/sh") else "sh"
    return [shell_path, script_path]


@click.command(
    name="self-update",
    help=(
        "Update the APM CLI binary itself to the latest version.\n\n"
        "Environment variables for enterprise bootstrap mirrors:\n"
        "  APM_RELEASE_METADATA_URL  Mirror latest.json release metadata.\n"
        "  APM_RELEASE_BASE_URL      Mirror release assets at {base}/{tag}/{asset}.\n"
        "  APM_INSTALLER_BASE_URL    Mirror install.sh/install.ps1 for self-update.\n"
        "  APM_PYPI_INDEX_URL        PyPI mirror for installer pip fallback.\n"
        "  APM_NO_DIRECT_FALLBACK    Set to 1 to fail closed on public fallback.\n"
    ),
)
@click.option("--check", is_flag=True, help="Only check for updates without installing")
def self_update(check):
    """Update APM CLI to the latest version (like npm update -g npm).

    This command fetches and installs the latest version of APM using the
    official install script. It will detect your platform and architecture
    automatically.

    Examples:
        apm self-update         # Update to latest version
        apm self-update --check # Only check if update is available
    """
    try:
        import subprocess
        import tempfile

        logger = CommandLogger("self-update")

        if not is_self_update_enabled():
            logger.warning(get_self_update_disabled_message())
            return

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            logger.warning("Cannot determine current version. Running in development mode?")
            if not check:
                logger.progress("To update, reinstall from the repository.")
            return

        logger.progress(f"Current version: {current_version}")
        logger.start("Checking for updates...")

        _github_url = os.environ.get("GITHUB_URL", "").rstrip("/")
        if _github_url and _github_url != _DEFAULT_GITHUB_URL:
            logger.progress(f"GITHUB_URL override active -- using host: {_github_url!r}")
        _release_metadata_url = get_release_metadata_url()
        if _release_metadata_url:
            logger.progress("APM_RELEASE_METADATA_URL override active -- using mirrored metadata")
        _pinned = os.environ.get("VERSION", "")
        if _pinned:
            logger.progress(f"VERSION env var set -- API call skipped, using: {_pinned!r}")

        if release_metadata_public_lookup_blocked(_github_url or _DEFAULT_GITHUB_URL):
            _log_no_direct_metadata_error(logger)
            sys.exit(1)

        # Check for latest version
        from ..utils.version_checker import get_latest_version_from_github

        latest_version = get_latest_version_from_github()

        if not latest_version:
            if _release_metadata_url:
                logger.error("Unable to fetch latest version from APM_RELEASE_METADATA_URL mirror")
                logger.info(
                    "Check the mirror URL, publish latest.json, or set VERSION to a pinned release."
                )
            else:
                logger.error("Unable to fetch latest version from remote")
                logger.info("Check your internet connection or try again later.")
            sys.exit(1)

        from ..utils.version_checker import is_newer_version

        if not is_newer_version(current_version, latest_version):
            logger.success(
                f"You're already on the latest version: {current_version}",
                symbol="check",
            )
            return

        logger.progress(f"Latest version available: {latest_version}", symbol="sparkles")

        if check:
            logger.warning(f"Update available: {current_version} -> {latest_version}")
            logger.progress("Run 'apm self-update' (without --check) to install")
            return

        # Proceed with update
        logger.start("Downloading and installing update...")

        # Download install script to temp file
        try:
            import requests

            try:
                install_script_url = _get_update_installer_url()
            except RuntimeError as e:
                logger.error(str(e))
                logger.info(
                    "Unset APM_NO_DIRECT_FALLBACK only if public installer fallback is allowed."
                )
                sys.exit(1)
            response = requests.get(install_script_url, timeout=10)
            response.raise_for_status()

            # Create temporary file for install script
            from ..config import get_apm_temp_dir

            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=_get_update_installer_suffix(),
                delete=False,
                dir=get_apm_temp_dir(),
            ) as f:
                temp_script = f.name
                f.write(response.text)

            if not _is_windows_platform():
                os.chmod(temp_script, 0o755)  # noqa: S103

            # Run install script
            logger.progress("Running installer...", symbol="gear")

            # Note: We don't capture output so the installer can prompt when needed.
            # Sanitise the environment so the installer (and the system binaries
            # it spawns -- curl, tar, sudo) do not inherit the PyInstaller
            # bootloader's LD_LIBRARY_PATH / DYLD_* overrides, which would
            # otherwise redirect system linkers at this binary's bundled
            # _internal directory.  See issue #894.
            result = subprocess.run(
                _get_installer_run_command(temp_script),
                check=False,
                env=external_process_env(),
            )

            # Clean up temp file
            try:  # noqa: SIM105
                os.unlink(temp_script)
            except Exception:
                # Non-fatal: failed to delete temp install script
                pass

            if result.returncode == 0:
                logger.success(
                    f"Successfully updated to version {latest_version}!",
                )
                logger.progress("Please restart your terminal or run 'apm --version' to verify")
            else:
                logger.error("Installation failed - see output above for details")
                sys.exit(1)

        except ImportError:
            logger.error("'requests' library not available")
            logger.info("Update manually using:")
            click.echo(f"  {_get_manual_update_command()}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Update failed: {e}")
            logger.info("Update manually using:")
            click.echo(f"  {_get_manual_update_command()}")
            sys.exit(1)

    except Exception as e:
        _logger = CommandLogger("self-update")
        _logger.error(f"Error during update: {e}")
        sys.exit(1)
