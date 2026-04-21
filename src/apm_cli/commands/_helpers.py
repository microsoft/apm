"""Shared CLI helpers for APM command modules.

This module must NOT import from any command module.
"""

import builtins
import os
import tempfile
from pathlib import Path

import click
from colorama import Fore, Style
from colorama import init as colorama_init

from ..constants import (
    APM_DIR,
    APM_LOCK_FILENAME,
    APM_MODULES_DIR,
    APM_MODULES_GITIGNORE_PATTERN,
    APM_YML_FILENAME,
    GITIGNORE_FILENAME,
)
from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..update_policy import get_update_hint_message, is_self_update_enabled
from ..version import get_build_sha, get_version
from ..utils.version_checker import check_for_updates

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
        raise ImportError("PyYAML is required but not installed")


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

def _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir: Path) -> set:
    """Build expected package paths under *apm_modules_dir*.

    Combines direct deps (from ``apm.yml``) with transitive deps
    (depth > 1 from ``apm.lock``), using ``get_install_path()`` for
    consistency with how packages are actually installed.
    """
    from ..models.apm_package import DependencyReference

    expected = builtins.set()
    for dep in declared_deps:
        install_path = dep.get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(relative_path.as_posix())
        except ValueError:
            expected.add(str(install_path))

    if lockfile:
        for dep in lockfile.get_all_dependencies():
            if dep.depth is not None and dep.depth > 1:
                dep_ref = DependencyReference(
                    repo_url=dep.repo_url,
                    host=dep.host,
                    virtual_path=dep.virtual_path,
                    is_virtual=dep.is_virtual,
                )
                install_path = dep_ref.get_install_path(apm_modules_dir)
                try:
                    relative_path = install_path.relative_to(apm_modules_dir)
                    expected.add(relative_path.as_posix())
                except ValueError:
                    pass
    return expected


def _scan_installed_packages(apm_modules_dir: Path) -> list:
    """Scan *apm_modules_dir* for installed package paths.

    Walks the tree to find directories containing ``apm.yml`` or ``.apm``,
    supporting GitHub (2-level), ADO (3-level), and subdirectory packages.

    Returns:
        List of ``"owner/repo"`` or ``"org/project/repo"`` path keys.
    """
    installed: list = []
    if not apm_modules_dir.exists():
        return installed
    for candidate in apm_modules_dir.rglob("*"):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        if not ((candidate / APM_YML_FILENAME).exists() or (candidate / APM_DIR).exists()):
            continue
        rel_parts = candidate.relative_to(apm_modules_dir).parts
        if len(rel_parts) >= 2:
            installed.append("/".join(rel_parts))
    return installed


def _check_orphaned_packages():
    """Check for packages in apm_modules/ that are not declared in apm.yml or apm.lock.

    Considers both direct dependencies (from apm.yml) and transitive dependencies
    (from apm.lock) as expected packages, so transitive deps are not falsely
    flagged as orphaned.

    Returns:
        List[str]: List of orphaned package names in org/repo or org/project/repo format
    """
    try:
        if not Path(APM_YML_FILENAME).exists():
            return []

        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            return []

        try:
            from ..models.apm_package import APMPackage
            from ..deps.lockfile import LockFile, get_lockfile_path

            apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
            expected = _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir)
        except Exception:
            return []

        installed = _scan_installed_packages(apm_modules_dir)
        return [p for p in installed if p not in expected]
    except Exception:
        return []


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
            click.echo(
                f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}"
            )
    else:
        # Graceful fallback when Rich isn't available (e.g., stripped automation environment)
        click.echo(
            f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}"
        )

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


def _atomic_write(path: Path, data: str) -> None:
    """Atomically write text data to path (best-effort)."""
    fd, tmp_name = tempfile.mkstemp(prefix="apm-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _update_gitignore_for_apm_modules(logger=None):
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(GITIGNORE_FILENAME)
    apm_modules_pattern = APM_MODULES_GITIGNORE_PATTERN

    # Read current .gitignore content
    current_content = []
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
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


# ------------------------------------------------------------------
# Script / config helpers (shared by run, list, config commands)
# ------------------------------------------------------------------

def _load_apm_config():
    """Load configuration from apm.yml."""
    if Path(APM_YML_FILENAME).exists():
        from ..utils.yaml_io import load_yaml
        return load_yaml(APM_YML_FILENAME)
    return None


def _get_default_script():
    """Get the default script (start) from apm.yml scripts."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config and "start" in apm_config["scripts"]:
        return "start"
    return None


def _list_available_scripts():
    """List all available scripts from apm.yml."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config:
        return apm_config["scripts"]
    return {}


# ------------------------------------------------------------------
# Init helpers (shared by init and install commands)
# ------------------------------------------------------------------

def _auto_detect_author():
    """Auto-detect author from git config."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, encoding="utf-8", timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "Developer"


def _auto_detect_description(project_name):
    """Auto-detect description from git repository or use default."""
    import subprocess

    try:
        # Try to get git repository description
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # We have a git repo, but description is typically not set
            # Just use a sensible default
            pass
    except Exception:
        pass
    return f"APM project for {project_name}"


def _get_default_config(project_name):
    """Get default configuration for new projects with auto-detection."""
    return {
        "name": project_name,
        "version": "1.0.0",
        "description": _auto_detect_description(project_name),
        "author": _auto_detect_author(),
    }


def _validate_plugin_name(name):
    """Validate plugin name is kebab-case (lowercase, numbers, hyphens).

    Returns True if valid, False otherwise.
    """
    import re

    return bool(re.match(r"^[a-z][a-z0-9-]{0,63}$", name))


def _validate_project_name(name):
    """Validate that a project name is safe to use as a directory name.

    Project names are used directly as directory names and must not contain
    '/' or '\' so the name is not interpreted as a filesystem path,
    and must not be '..' to prevent directory traversal.

    Returns True if valid, False otherwise.
    """
    if "/" in name or "\\" in name:
        return False
    if name == "..":
        return False
    return True

def _create_plugin_json(config):
    """Create plugin.json file with package metadata.

    Args:
        config: dict with name, version, description, author keys.
    """
    import json

    plugin_data = {
        "name": config["name"],
        "version": config.get("version", "0.1.0"),
        "description": config.get("description", ""),
        "author": {"name": config.get("author", "")},
        "license": "MIT",
    }

    with open("plugin.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(plugin_data, indent=2) + "\n")


def _create_minimal_apm_yml(config, plugin=False, target_path=None):
    """Create minimal apm.yml file with auto-detected metadata.

    Args:
        config: dict with name, version, description, author keys.
        plugin: if True, include a devDependencies section.
        target_path: explicit file path to write (defaults to cwd/apm.yml).
    """
    # Create minimal apm.yml structure
    apm_yml_data = {
        "name": config["name"],
        "version": config["version"],
        "description": config["description"],
        "author": config["author"],
        "dependencies": {"apm": [], "mcp": []},
    }

    if plugin:
        apm_yml_data["devDependencies"] = {"apm": []}

    apm_yml_data["scripts"] = {}

    # Write apm.yml
    from ..utils.yaml_io import dump_yaml
    out_path = target_path or APM_YML_FILENAME
    dump_yaml(apm_yml_data, out_path)
