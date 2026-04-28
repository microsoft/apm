"""APM update command."""

import os
import shutil
import sys

import click

from ..core.command_logger import CommandLogger
from ..update_policy import get_self_update_disabled_message, is_self_update_enabled
from ..utils.subprocess_env import external_process_env
from ..version import get_version


def _is_windows_platform() -> bool:
    """Return True when running on native Windows."""
    return sys.platform == "win32"


def _get_update_installer_url() -> str:
    """Return the official installer URL for the current platform."""
    return "https://aka.ms/apm-windows" if _is_windows_platform() else "https://aka.ms/apm-unix"


def _get_update_installer_suffix() -> str:
    """Return the file suffix for the downloaded installer script."""
    return ".ps1" if _is_windows_platform() else ".sh"


def _get_manual_update_command() -> str:
    """Return the manual update command for the current platform."""
    if _is_windows_platform():
        return (
            'powershell -ExecutionPolicy Bypass -c '
            '"irm https://aka.ms/apm-windows | iex"'
        )
    return "curl -sSL https://aka.ms/apm-unix | sh"


def _get_installer_run_command(script_path: str) -> list[str]:
    """Return the installer execution command for the current platform."""
    if _is_windows_platform():
        powershell_path = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell_path:
            raise FileNotFoundError("PowerShell executable not found in PATH")
        return [powershell_path, "-ExecutionPolicy", "Bypass", "-File", script_path]

    shell_path = "/bin/sh" if os.path.exists("/bin/sh") else "sh"
    return [shell_path, script_path]


@click.command(help="Update APM to the latest version")
@click.option("--check", is_flag=True, help="Only check for updates without installing")
def update(check):
    """Update APM CLI to the latest version (like npm update -g npm).

    This command fetches and installs the latest version of APM using the
    official install script. It will detect your platform and architecture
    automatically.

    Examples:
        apm update         # Update to latest version
        apm update --check # Only check if update is available
    """
    try:
        import subprocess
        import tempfile

        logger = CommandLogger("update")

        if not is_self_update_enabled():
            logger.warning(get_self_update_disabled_message())
            return

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            logger.warning(
                "Cannot determine current version. Running in development mode?"
            )
            if not check:
                logger.progress("To update, reinstall from the repository.")
            return

        logger.progress(f"Current version: {current_version}")
        logger.start("Checking for updates...")

        # Check for latest version
        from ..utils.version_checker import get_latest_version_from_github

        latest_version = get_latest_version_from_github()

        if not latest_version:
            logger.error("Unable to fetch latest version from GitHub")
            logger.progress("Please check your internet connection or try again later")
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
            logger.progress("Run 'apm update' (without --check) to install")
            return

        # Proceed with update
        logger.start("Downloading and installing update...")

        # Download install script to temp file
        try:
            import requests

            install_script_url = _get_update_installer_url()
            response = requests.get(install_script_url, timeout=10)
            response.raise_for_status()

            # Create temporary file for install script
            from ..config import get_apm_temp_dir
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=_get_update_installer_suffix(), delete=False,
                dir=get_apm_temp_dir()
            ) as f:
                temp_script = f.name
                f.write(response.text)

            if not _is_windows_platform():
                os.chmod(temp_script, 0o755)

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
            try:
                os.unlink(temp_script)
            except Exception:
                # Non-fatal: failed to delete temp install script
                pass

            if result.returncode == 0:
                logger.success(
                    f"Successfully updated to version {latest_version}!",
                )
                logger.progress(
                    "Please restart your terminal or run 'apm --version' to verify"
                )
            else:
                logger.error("Installation failed - see output above for details")
                sys.exit(1)

        except ImportError:
            logger.error("'requests' library not available")
            logger.progress("Please update manually using:")
            click.echo(f"  {_get_manual_update_command()}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Update failed: {e}")
            logger.progress("Please update manually using:")
            click.echo(f"  {_get_manual_update_command()}")
            sys.exit(1)

    except Exception as e:
        _logger = CommandLogger("update")
        _logger.error(f"Error during update: {e}")
        sys.exit(1)
