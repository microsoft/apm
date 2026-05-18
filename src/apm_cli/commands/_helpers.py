"""Shared CLI helpers for APM command modules.

This module must NOT import from any command module.
"""

import builtins
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import click
from colorama import Fore, Style
from colorama import init as colorama_init

from ..constants import (
    APM_DIR,
    APM_MODULES_DIR,
    APM_MODULES_GITIGNORE_PATTERN,
    APM_YML_FILENAME,
    GITIGNORE_FILENAME,
)
from ..update_policy import get_update_hint_message, is_self_update_enabled
from ..utils.atomic_io import atomic_write_text as _atomic_write
from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..utils.path_security import PathTraversalError, validate_path_segments
from ..utils.version_checker import check_for_updates
from ..version import get_build_sha, get_version
from ._helpers_init import (
    _auto_detect_author,
    _auto_detect_description,
    _create_minimal_apm_yml,
    _create_plugin_json,
    _get_default_config,
    _get_default_script,
    _list_available_scripts,
    _load_apm_config,
    _validate_plugin_name,
    _validate_project_name,
)

# CRITICAL: Shadow Click commands at module level to prevent namespace collision
# When Click commands like 'config set' are defined, calling set() can invoke the command
# instead of the Python built-in. This affects ALL functions in this module.
set = builtins.set
list = builtins.list
dict = builtins.dict

# Initialize colorama for fallback
colorama_init(autoreset=True)

# Legacy colorama constants for compatibility
TITLE = f"{Fore.CYAN}{Style.BRIGHT}"
SUCCESS = f"{Fore.GREEN}{Style.BRIGHT}"
ERROR = f"{Fore.RED}{Style.BRIGHT}"
INFO = f"{Fore.BLUE}"
WARNING = f"{Fore.YELLOW}"
HIGHLIGHT = f"{Fore.MAGENTA}{Style.BRIGHT}"
RESET = Style.RESET_ALL


# -------------------------------------------------------------------
# TTY detection
# -------------------------------------------------------------------


def _is_interactive():
    """Return True when both stdin and stdout are attached to a TTY."""
    return sys.stdin.isatty() and sys.stdout.isatty()


# Lazy loading for Rich components to improve startup performance
_console = None


def _get_console():
    """Get Rich console instance with lazy loading."""
    global _console
    if _console is None:
        from rich.console import Console
        from rich.theme import Theme

        custom_theme = Theme(
            {
                "info": "cyan",
                "warning": "yellow",
                "error": "bold red",
                "success": "bold green",
                "highlight": "bold magenta",
                "muted": "dim white",
                "accent": "bold blue",
                "title": "bold cyan",
            }
        )

        _console = Console(theme=custom_theme)
    return _console


def _rich_blank_line():
    """Print a blank line with Rich if available, otherwise use click."""
    console = _get_console()
    if console:
        console.print()
    else:
        click.echo()


def _lazy_yaml():
    """Lazy import for yaml module to improve startup performance."""
    try:
        import yaml

        return yaml
    except ImportError:
        raise ImportError("PyYAML is required but not installed")  # noqa: B904


def _lazy_prompt():
    """Lazy import for Rich Prompt to improve startup performance."""
    try:
        from rich.prompt import Prompt

        return Prompt
    except ImportError:
        return None


def _lazy_confirm():
    """Lazy import for Rich Confirm to improve startup performance."""
    try:
        from rich.prompt import Confirm

        return Confirm
    except ImportError:
        return None


# ------------------------------------------------------------------
# Shared orphan-detection helpers
# ------------------------------------------------------------------
from ._helpers_orphans import (
    _build_expected_install_paths,
    _check_orphaned_packages,
    _scan_installed_packages,
    _standalone_installed_packages,
)
from ._helpers_orphans import (
    _expand_with_ancestors as _expand_with_ancestors_impl,
)


def _expand_with_ancestors(
    paths: Iterable[str], installed: Iterable[str] | None = None
) -> set[str]:
    """Expand paths while routing validation through this module's guard."""
    return _expand_with_ancestors_impl(
        paths,
        installed=installed,
        validate_segments=validate_path_segments,
    )


def print_version(ctx, param, value):
    """Print version and exit."""
    if not value or ctx.resilient_parsing:
        return

    version_str = get_version()
    sha = get_build_sha()
    if sha:
        version_str += f" ({sha})"

    console = _get_console()
    if console:
        try:
            console.print(
                f"[bold cyan]Agent Package Manager (APM) CLI[/bold cyan] version {version_str}"
            )
        except Exception:
            click.echo(f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}")
    else:
        # Graceful fallback when Rich isn't available (e.g., stripped automation environment)
        click.echo(f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}")

    # Gated verbose-version output (experimental flag)
    try:
        from ..core.experimental import is_enabled

        if is_enabled("verbose_version"):
            import platform
            import sys

            python_ver = platform.python_version()
            plat = f"{sys.platform}-{platform.machine()}"
            install_path = str(Path(__file__).resolve().parent.parent)

            _rich_echo(f"  {'Python:':<14}{python_ver}", color="dim")
            _rich_echo(f"  {'Platform:':<14}{plat}", color="dim")
            _rich_echo(f"  {'Install path:':<14}{install_path}", color="dim")
    except Exception:
        # Never let experimental flag logic break --version
        pass

    ctx.exit()


def _check_and_notify_updates():
    """Check for updates and notify user non-blockingly."""
    try:
        # Skip notifications when self-update is disabled by distribution policy.
        if not is_self_update_enabled():
            return

        # Skip version check in E2E test mode to avoid interfering with tests
        if os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes"):
            return

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            return

        latest_version = check_for_updates(current_version)

        if latest_version:
            # Display yellow warning with update command
            _rich_warning(
                f"A new version of APM is available: {latest_version} (current: {current_version})",
                symbol="warning",
            )

            # Show update command using helper for consistency
            _rich_echo(get_update_hint_message(), color="yellow", bold=True)

            # Add a blank line for visual separation
            click.echo()
    except Exception:
        # Silently fail - version checking should never block CLI usage
        pass


def _update_gitignore_for_apm_modules(logger=None):
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(GITIGNORE_FILENAME)
    apm_modules_pattern = APM_MODULES_GITIGNORE_PATTERN

    # Read current .gitignore content
    current_content = []
    if gitignore_path.exists():
        try:
            with open(gitignore_path, encoding="utf-8") as f:
                current_content = [line.rstrip("\n\r") for line in f.readlines()]
        except Exception as e:
            if logger:
                logger.warning(f"Could not read .gitignore: {e}")
            else:
                _rich_warning(f"Could not read .gitignore: {e}")
            return

    # Check if apm_modules/ is already in .gitignore
    if any(line.strip() == apm_modules_pattern for line in current_content):
        return  # Already present

    # Add apm_modules/ to .gitignore
    try:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            # Add a blank line before our entry if file isn't empty
            if current_content and current_content[-1].strip():
                f.write("\n")
            f.write(f"\n# APM dependencies\n{apm_modules_pattern}\n")

        if logger:
            logger.progress(f"Added {apm_modules_pattern} to .gitignore")
        else:
            _rich_info(f"Added {apm_modules_pattern} to .gitignore")
    except Exception as e:
        if logger:
            logger.warning(f"Could not update .gitignore: {e}")
        else:
            _rich_warning(f"Could not update .gitignore: {e}")
