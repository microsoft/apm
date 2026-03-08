"""Command-line interface for Agent Package Manager (APM)."""

import builtins
import os
import shutil
import sys
import warnings
from pathlib import Path
from typing import List

import click
from colorama import Fore, Style, init

# CRITICAL: Shadow Click commands at module level to prevent namespace collision
# When Click commands like 'config set' are defined, calling set() can invoke the command
# instead of the Python built-in. This affects ALL functions in this module.
set = builtins.set
list = builtins.list
dict = builtins.dict

from apm_cli.commands.deps import deps
from apm_cli.compilation import AgentsCompiler, CompilationConfig
from apm_cli.primitives.discovery import discover_primitives
from apm_cli.utils.console import (
    STATUS_SYMBOLS,
    _create_files_table,
    _get_console,
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_panel,
    _rich_success,
    _rich_warning,
    show_download_spinner,
)
from apm_cli.utils.github_host import is_valid_fqdn, default_host

# APM imports - use absolute imports everywhere for consistency
from apm_cli.version import get_build_sha, get_version
from apm_cli.utils.version_checker import check_for_updates

# APM Dependencies - Import for Task 5 integration
try:
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import APMPackage, DependencyReference
    from apm_cli.integration import PromptIntegrator, AgentIntegrator

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    # Graceful fallback if APM dependencies are not available
    APM_DEPS_AVAILABLE = False
    _APM_IMPORT_ERROR = str(e)

# Initialize colorama for fallback
init(autoreset=True)

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
    expected = builtins.set()
    for dep in declared_deps:
        install_path = dep.get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(str(relative_path))
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
                    expected.add(str(relative_path))
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
        if not ((candidate / "apm.yml").exists() or (candidate / ".apm").exists()):
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
        if not Path("apm.yml").exists():
            return []

        apm_modules_dir = Path("apm_modules")
        if not apm_modules_dir.exists():
            return []

        try:
            apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(Path.cwd() / "apm.lock")
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
        from rich.panel import Panel  # type: ignore
        from rich.text import Text  # type: ignore

        version_text = Text()
        version_text.append("Agent Package Manager (APM) CLI", style="bold cyan")
        version_text.append(f" version {version_str}", style="white")
        console.print(Panel(version_text, border_style="cyan", padding=(0, 1)))
    else:
        # Graceful fallback when Rich isn't available (e.g., stripped automation environment)
        click.echo(
            f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}"
        )

    ctx.exit()


def _check_and_notify_updates():
    """Check for updates and notify user non-blockingly."""
    try:
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
            _rich_echo("Run apm update to upgrade", color="yellow", bold=True)

            # Add a blank line for visual separation
            click.echo()
    except Exception:
        # Silently fail - version checking should never block CLI usage
        pass


@click.group(
    help="Agent Package Manager (APM): The package manager for AI-Native Development"
)
@click.option(
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
    help="Show version and exit.",
)
@click.pass_context
def cli(ctx):
    """Main entry point for the APM CLI."""
    ctx.ensure_object(dict)

    # Check for updates non-blockingly (only if not already showing version)
    if not ctx.resilient_parsing:
        _check_and_notify_updates()


# Register command groups
cli.add_command(deps)


@cli.command(help="🚀 Initialize a new APM project")
@click.argument("project_name", required=False)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts and use auto-detected defaults"
)
@click.pass_context
def init(ctx, project_name, yes):
    """Initialize a new APM project (like npm init).

    Creates a minimal apm.yml with auto-detected metadata.
    """
    try:
        # Handle explicit current directory
        if project_name == ".":
            project_name = None

        # Determine project directory and name
        if project_name:
            project_dir = Path(project_name)
            project_dir.mkdir(exist_ok=True)
            os.chdir(project_dir)
            _rich_info(f"Created project directory: {project_name}", symbol="folder")
            final_project_name = project_name
        else:
            project_dir = Path.cwd()
            final_project_name = project_dir.name

        # Check for existing apm.yml
        apm_yml_exists = Path("apm.yml").exists()

        # Handle existing apm.yml in brownfield projects
        if apm_yml_exists:
            _rich_warning("apm.yml already exists")

            if not yes:
                Confirm = _lazy_confirm()
                if Confirm:
                    try:
                        confirm = Confirm.ask("Continue and overwrite?")
                    except Exception:
                        confirm = click.confirm("Continue and overwrite?")
                else:
                    confirm = click.confirm("Continue and overwrite?")

                if not confirm:
                    _rich_info("Initialization cancelled.")
                    return
            else:
                _rich_info("--yes specified, overwriting apm.yml...")

        # Get project configuration (interactive mode or defaults)
        if not yes:
            config = _interactive_project_setup(final_project_name)
        else:
            # Use auto-detected defaults
            config = _get_default_config(final_project_name)

        _rich_success(f"Initializing APM project: {config['name']}", symbol="rocket")

        # Create minimal apm.yml
        _create_minimal_apm_yml(config)

        _rich_success("APM project initialized successfully!", symbol="sparkles")

        # Display created file info
        try:
            console = _get_console()
            if console:
                files_data = [
                    ("✨", "apm.yml", "Project configuration"),
                ]
                table = _create_files_table(files_data, title="Created Files")
                console.print(table)
        except (ImportError, NameError):
            _rich_info("Created:")
            _rich_echo("  ✨ apm.yml - Project configuration", style="muted")

        _rich_blank_line()

        # Next steps - actionable commands matching README workflow
        next_steps = [
            "Install a runtime:       apm runtime setup copilot",
            "Add APM dependencies:    apm install <owner>/<repo>",
            "Compile agent context:   apm compile",
            "Run your first workflow: apm run start",
        ]

        try:
            _rich_panel(
                "\n".join(f"• {step}" for step in next_steps),
                title="💡 Next Steps",
                style="cyan",
            )
        except (ImportError, NameError):
            _rich_info("Next steps:")
            for step in next_steps:
                click.echo(f"  • {step}")

    except Exception as e:
        _rich_error(f"Error initializing project: {e}")
        sys.exit(1)


def _validate_and_add_packages_to_apm_yml(packages, dry_run=False):
    """Validate packages exist and can be accessed, then add to apm.yml dependencies section.
    
    Implements normalize-on-write: any input form (HTTPS URL, SSH URL, FQDN, shorthand)
    is canonicalized before storage. Default host (github.com) is stripped;
    non-default hosts are preserved. Duplicates are detected by identity.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    import yaml

    apm_yml_path = Path("apm.yml")

    # Read current apm.yml
    try:
        with open(apm_yml_path, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        _rich_error(f"Failed to read apm.yml: {e}")
        sys.exit(1)

    # Ensure dependencies structure exists
    if "dependencies" not in data:
        data["dependencies"] = {}
    if "apm" not in data["dependencies"]:
        data["dependencies"]["apm"] = []

    current_deps = data["dependencies"]["apm"] or []
    validated_packages = []

    # Build identity set from existing deps for duplicate detection
    existing_identities = builtins.set()
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, str):
                ref = DependencyReference.parse(dep_entry)
            elif isinstance(dep_entry, dict):
                ref = DependencyReference.parse_from_dict(dep_entry)
            else:
                continue
            existing_identities.add(ref.get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    # First, validate all packages
    _rich_info(f"Validating {len(packages)} package(s)...")

    for package in packages:
        # Validate package format (should be owner/repo or a git URL)
        if "/" not in package:
            _rich_error(f"Invalid package format: {package}. Use 'owner/repo' format.")
            continue

        # Canonicalize input
        try:
            dep_ref = DependencyReference.parse(package)
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
        except ValueError as e:
            _rich_error(f"Invalid package: {package} — {e}")
            continue

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        if _validate_package_exists(package):
            if already_in_deps:
                _rich_info(
                    f"✓ {canonical} - already in apm.yml, ensuring installation..."
                )
            else:
                validated_packages.append(canonical)
                existing_identities.add(identity)  # prevent duplicates within batch
                _rich_info(f"✓ {canonical} - accessible")
        else:
            _rich_error(f"✗ {package} - not accessible or doesn't exist")

    if not validated_packages:
        if dry_run:
            _rich_warning("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them
        return []

    if dry_run:
        _rich_info(
            f"Dry run: Would add {len(validated_packages)} package(s) to apm.yml:"
        )
        for pkg in validated_packages:
            _rich_info(f"  + {pkg}")
        return validated_packages

    # Add validated packages to dependencies (already canonical)
    for package in validated_packages:
        current_deps.append(package)
        _rich_info(f"Added {package} to apm.yml")

    # Update dependencies
    data["dependencies"]["apm"] = current_deps

    # Write back to apm.yml
    try:
        with open(apm_yml_path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        _rich_success(f"Updated apm.yml with {len(validated_packages)} new package(s)")
    except Exception as e:
        _rich_error(f"Failed to write apm.yml: {e}")
        sys.exit(1)

    return validated_packages


def _validate_package_exists(package):
    """Validate that a package exists and is accessible on GitHub or Azure DevOps."""
    import os
    import subprocess
    import tempfile

    try:
        # Parse the package to check if it's a virtual package or ADO
        from apm_cli.models.apm_package import DependencyReference
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = DependencyReference.parse(package)

        # For virtual packages, use the downloader's validation method
        if dep_ref.is_virtual:
            downloader = GitHubPackageDownloader()
            return downloader.validate_virtual_package_exists(dep_ref)

        # For Azure DevOps or GitHub Enterprise (non-github.com hosts),
        # use the downloader which handles authentication properly
        if dep_ref.is_azure_devops() or (dep_ref.host and dep_ref.host != "github.com"):
            from apm_cli.utils.github_host import is_github_hostname, is_azure_devops_hostname

            downloader = GitHubPackageDownloader()
            # Set the host
            if dep_ref.host:
                downloader.github_host = dep_ref.host

            # Build authenticated URL using downloader's auth
            package_url = downloader._build_repo_url(
                dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref
            )

            # For generic hosts (not GitHub, not ADO), relax the env so native
            # credential helpers (SSH keys, macOS Keychain, etc.) can work.
            # This mirrors _clone_with_fallback() which does the same relaxation.
            is_generic = not is_github_hostname(dep_ref.host) and not is_azure_devops_hostname(dep_ref.host)
            if is_generic:
                validate_env = {k: v for k, v in downloader.git_env.items()
                                if k not in ('GIT_ASKPASS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
                validate_env['GIT_TERMINAL_PROMPT'] = '0'
            else:
                validate_env = {**os.environ, **downloader.git_env}

            cmd = ["git", "ls-remote", "--heads", "--exit-code", package_url]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=validate_env,
            )
            return result.returncode == 0

        # For GitHub.com, use standard approach (public repos don't need auth)
        package_url = f"{dep_ref.to_github_url()}.git"

        # For regular packages, use git ls-remote
        with tempfile.TemporaryDirectory() as temp_dir:
            try:

                # Try cloning with minimal fetch
                cmd = [
                    "git",
                    "ls-remote",
                    "--heads",
                    "--exit-code",
                    package_url,
                ]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30  # 30 second timeout
                )

                return result.returncode == 0

            except subprocess.TimeoutExpired:
                return False
            except Exception:
                return False

    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        package_url = (
            f"https://{package}.git"
            if is_valid_fqdn(package)
            else f"https://{default_host()}/{package}.git"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                cmd = [
                    "git",
                    "ls-remote",
                    "--heads",
                    "--exit-code",
                    package_url,
                ]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                return result.returncode == 0

            except subprocess.TimeoutExpired:
                return False
            except Exception:
                return False


@cli.command(
    help="📦 Install APM and MCP dependencies (auto-creates apm.yml when installing packages)"
)
@click.argument("packages", nargs=-1)
@click.option("--runtime", help="Target specific runtime only (copilot, codex, vscode)")
@click.option("--exclude", help="Exclude specific runtime from installation")
@click.option(
    "--only",
    type=click.Choice(["apm", "mcp"]),
    help="Install only specific dependency type",
)
@click.option(
    "--update", is_flag=True, help="Update dependencies to latest Git references"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be installed without installing"
)
@click.option("--force", is_flag=True, help="Overwrite locally-authored files on collision")
@click.option("--verbose", is_flag=True, help="Show detailed installation information")
@click.option(
    "--trust-transitive-mcp",
    is_flag=True,
    help="Trust self-defined MCP servers from transitive packages (skip re-declaration requirement)",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.pass_context
def install(ctx, packages, runtime, exclude, only, update, dry_run, force, verbose, trust_transitive_mcp, parallel_downloads):
    """Install APM and MCP dependencies from apm.yml (like npm install).

    This command automatically detects AI runtimes from your apm.yml scripts and installs
    MCP servers for all detected and available runtimes. It also installs APM package
    dependencies from GitHub repositories.

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1                    # Add package to apm.yml and install
        apm install org/pkg1 org/pkg2           # Add multiple packages and install
        apm install --exclude codex             # Install for all except Codex CLI
        apm install --only=apm                  # Install only APM dependencies
        apm install --only=mcp                  # Install only MCP dependencies
        apm install --update                    # Update dependencies to latest Git refs
        apm install --dry-run                   # Show what would be installed
    """
    try:
        # Check if apm.yml exists
        apm_yml_exists = Path("apm.yml").exists()

        # Auto-bootstrap: create minimal apm.yml when packages specified but no apm.yml
        if not apm_yml_exists and packages:
            # Get current directory name as project name
            project_name = Path.cwd().name
            config = _get_default_config(project_name)
            _create_minimal_apm_yml(config)
            _rich_success("Created apm.yml", symbol="sparkles")

        # Error when NO apm.yml AND NO packages
        if not apm_yml_exists and not packages:
            _rich_error("No apm.yml found")
            _rich_info("💡 Run 'apm init' to create one, or:")
            _rich_info("   apm install <org/repo> to auto-create + install")
            sys.exit(1)

        # If packages are specified, validate and add them to apm.yml first
        if packages:
            validated_packages = _validate_and_add_packages_to_apm_yml(
                packages, dry_run
            )
            # Note: Empty validated_packages is OK if packages are already in apm.yml
            # We'll proceed with installation from apm.yml to ensure everything is synced

        _rich_info("Installing dependencies from apm.yml...")

        # Parse apm.yml to get both APM and MCP dependencies
        try:
            apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
        except Exception as e:
            _rich_error(f"Failed to parse apm.yml: {e}")
            sys.exit(1)

        # Get APM and MCP dependencies
        apm_deps = apm_package.get_apm_dependencies()
        mcp_deps = apm_package.get_mcp_dependencies()

        # Determine what to install based on --only flag
        should_install_apm = only != "mcp"
        should_install_mcp = only != "apm"

        # Show what will be installed if dry run
        if dry_run:
            _rich_info("Dry run mode - showing what would be installed:")

            if should_install_apm and apm_deps:
                _rich_info(f"APM dependencies ({len(apm_deps)}):")
                for dep in apm_deps:
                    action = "update" if update else "install"
                    _rich_info(
                        f"  - {dep.repo_url}#{dep.reference or 'main'} → {action}"
                    )

            if should_install_mcp and mcp_deps:
                _rich_info(f"MCP dependencies ({len(mcp_deps)}):")
                for dep in mcp_deps:
                    _rich_info(f"  - {dep}")

            if not apm_deps and not mcp_deps:
                _rich_warning("No dependencies found in apm.yml")

            _rich_success("Dry run complete - no changes made")
            return

        # Install APM dependencies first (if requested)
        apm_count = 0
        prompt_count = 0
        agent_count = 0
        if should_install_apm and apm_deps:
            if not APM_DEPS_AVAILABLE:
                _rich_error("APM dependency system not available")
                _rich_info(f"Import error: {_APM_IMPORT_ERROR}")
                sys.exit(1)

            try:
                # If specific packages were requested, only install those
                # Otherwise install all from apm.yml
                only_pkgs = builtins.list(packages) if packages else None
                apm_count, prompt_count, agent_count = _install_apm_dependencies(
                    apm_package, update, verbose, only_pkgs, force=force,
                    parallel_downloads=parallel_downloads,
                )
            except Exception as e:
                _rich_error(f"Failed to install APM dependencies: {e}")
                sys.exit(1)
        elif should_install_apm and not apm_deps:
            _rich_info("No APM dependencies found in apm.yml")

        # Collect transitive MCP dependencies from resolved APM packages
        apm_modules_path = Path.cwd() / "apm_modules"
        if should_install_mcp and apm_modules_path.exists():
            lock_path = Path.cwd() / "apm.lock"
            transitive_mcp = _collect_transitive_mcp_deps(apm_modules_path, lock_path, trust_transitive_mcp)
            if transitive_mcp:
                _rich_info(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
                mcp_deps = _deduplicate_mcp_deps(mcp_deps + transitive_mcp)

        # Continue with MCP installation (existing logic)
        mcp_count = 0
        if should_install_mcp and mcp_deps:
            mcp_count = _install_mcp_dependencies(mcp_deps, runtime, exclude, verbose)
        elif should_install_mcp and not mcp_deps:
            _rich_warning("No MCP dependencies found in apm.yml")

        # Show beautiful post-install summary
        _rich_blank_line()
        if not only:
            # Load apm.yml config for summary
            apm_config = _load_apm_config()
            _show_install_summary(
                apm_count, prompt_count, agent_count, mcp_count, apm_config
            )
        elif only == "apm":
            _rich_success(f"Installed {apm_count} APM dependencies")
        elif only == "mcp":
            _rich_success(f"Configured {mcp_count} MCP servers")

    except Exception as e:
        _rich_error(f"Error installing dependencies: {e}")
        sys.exit(1)


@cli.command(help="Remove APM packages not listed in apm.yml")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be removed without removing"
)
@click.pass_context
def prune(ctx, dry_run):
    """Remove installed APM packages that are not listed in apm.yml (like npm prune).

    This command cleans up the apm_modules/ directory by removing packages that
    were previously installed but are no longer declared as dependencies in apm.yml.

    Examples:
        apm prune           # Remove orphaned packages
        apm prune --dry-run # Show what would be removed
    """
    try:
        # Check if apm.yml exists
        if not Path("apm.yml").exists():
            _rich_error("No apm.yml found. Run 'apm init' first.")
            sys.exit(1)

        # Check if apm_modules exists
        apm_modules_dir = Path("apm_modules")
        if not apm_modules_dir.exists():
            _rich_info("No apm_modules/ directory found. Nothing to prune.")
            return

        _rich_info("Analyzing installed packages vs apm.yml...")

        # Build expected vs installed using shared helpers
        try:
            apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(Path.cwd() / "apm.lock")
            expected_installed = _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir)
        except Exception as e:
            _rich_error(f"Failed to parse apm.yml: {e}")
            sys.exit(1)

        installed_packages = _scan_installed_packages(apm_modules_dir)
        orphaned_packages = [p for p in installed_packages if p not in expected_installed]

        if not orphaned_packages:
            _rich_success("No orphaned packages found. apm_modules/ is clean.")
            return

        # Show what will be removed
        _rich_info(f"Found {len(orphaned_packages)} orphaned package(s):")
        for pkg_name in orphaned_packages:
            if dry_run:
                _rich_info(f"  - {pkg_name} (would be removed)")
            else:
                _rich_info(f"  - {pkg_name}")

        if dry_run:
            _rich_success("Dry run complete - no changes made")
            return

        # Remove orphaned packages
        removed_count = 0
        pruned_keys = []
        deleted_pkg_paths: list = []
        for org_repo_name in orphaned_packages:
            path_parts = org_repo_name.split("/")
            pkg_path = apm_modules_dir.joinpath(*path_parts)
            try:
                shutil.rmtree(pkg_path)
                _rich_info(f"✓ Removed {org_repo_name}")
                removed_count += 1
                pruned_keys.append(org_repo_name)
                deleted_pkg_paths.append(pkg_path)
            except Exception as e:
                _rich_error(f"✗ Failed to remove {org_repo_name}: {e}")

        # Batch parent cleanup — single bottom-up pass
        from apm_cli.integration.base_integrator import BaseIntegrator
        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        # Clean deployed files for pruned packages and update lockfile
        if pruned_keys:
            from apm_cli.deps.lockfile import get_lockfile_path
            lockfile_path = get_lockfile_path(Path("."))
            lockfile = LockFile.read(lockfile_path)
            project_root = Path(".")
            if lockfile:
                deployed_cleaned = 0
                deleted_targets: list = []
                for dep_key in pruned_keys:
                    dep = lockfile.get_dependency(dep_key)
                    if dep and dep.deployed_files:
                        for rel_path in dep.deployed_files:
                            if not BaseIntegrator.validate_deploy_path(rel_path, project_root):
                                continue
                            target = project_root / rel_path
                            if target.is_file():
                                target.unlink()
                                deployed_cleaned += 1
                                deleted_targets.append(target)
                            elif target.is_dir():
                                shutil.rmtree(target)
                                deployed_cleaned += 1
                                deleted_targets.append(target)
                    # Remove from lockfile
                    if dep_key in lockfile.dependencies:
                        del lockfile.dependencies[dep_key]
                # Batch parent cleanup — single bottom-up pass
                BaseIntegrator.cleanup_empty_parents(deleted_targets, stop_at=project_root)
                if deployed_cleaned > 0:
                    _rich_info(f"✓ Cleaned {deployed_cleaned} deployed integration file(s)")
                # Write updated lockfile (or remove if empty)
                try:
                    if lockfile.dependencies:
                        lockfile.write(lockfile_path)
                    else:
                        lockfile_path.unlink(missing_ok=True)
                except Exception:
                    pass

        # Final summary
        if removed_count > 0:
            _rich_success(f"Pruned {removed_count} orphaned package(s)")
        else:
            _rich_warning("No packages were removed")

    except Exception as e:
        _rich_error(f"Error pruning packages: {e}")
        sys.exit(1)


@cli.command(help="⬆️  Update APM to the latest version")
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

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            _rich_warning(
                "Cannot determine current version. Running in development mode?"
            )
            if not check:
                _rich_info("To update, reinstall from the repository.")
            return

        _rich_info(f"Current version: {current_version}", symbol="info")
        _rich_info("Checking for updates...", symbol="running")

        # Check for latest version
        from apm_cli.utils.version_checker import get_latest_version_from_github

        latest_version = get_latest_version_from_github()

        if not latest_version:
            _rich_error("Unable to fetch latest version from GitHub")
            _rich_info("Please check your internet connection or try again later")
            sys.exit(1)

        from apm_cli.utils.version_checker import is_newer_version

        if not is_newer_version(current_version, latest_version):
            _rich_success(
                f"You're already on the latest version: {current_version}",
                symbol="check",
            )
            return

        _rich_info(f"Latest version available: {latest_version}", symbol="sparkles")

        if check:
            _rich_warning(f"Update available: {current_version} → {latest_version}")
            _rich_info("Run 'apm update' (without --check) to install", symbol="info")
            return

        # Proceed with update
        _rich_info("Downloading and installing update...", symbol="running")

        # Download install script to temp file
        try:
            import requests

            install_script_url = (
                "https://raw.githubusercontent.com/microsoft/apm/main/install.sh"
            )
            response = requests.get(install_script_url, timeout=10)
            response.raise_for_status()

            # Create temporary file for install script
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
                temp_script = f.name
                f.write(response.text)

            # Make script executable
            os.chmod(temp_script, 0o755)

            # Run install script
            _rich_info("Running installer...", symbol="gear")

            # Use /bin/sh for better cross-platform compatibility
            # Note: We don't capture output so the installer can prompt for sudo
            shell_path = "/bin/sh" if os.path.exists("/bin/sh") else "sh"
            result = subprocess.run(
                [shell_path, temp_script], check=False
            )

            # Clean up temp file
            try:
                os.unlink(temp_script)
            except Exception:
                # Non-fatal: failed to delete temp install script
                pass

            if result.returncode == 0:
                _rich_success(
                    f"Successfully updated to version {latest_version}!",
                    symbol="sparkles",
                )
                _rich_info(
                    "Please restart your terminal or run 'apm --version' to verify"
                )
            else:
                _rich_error("Installation failed - see output above for details")
                sys.exit(1)

        except ImportError:
            _rich_error("'requests' library not available")
            _rich_info("Please update manually using:")
            click.echo(
                "  curl -sSL https://raw.githubusercontent.com/microsoft/apm/main/install.sh | sh"
            )
            sys.exit(1)
        except Exception as e:
            _rich_error(f"Update failed: {e}")
            _rich_info("Please update manually using:")
            click.echo(
                "  curl -sSL https://raw.githubusercontent.com/microsoft/apm/main/install.sh | sh"
            )
            sys.exit(1)

    except Exception as e:
        _rich_error(f"Error during update: {e}")
        sys.exit(1)


@cli.command(help="Remove APM packages from apm.yml and apm_modules")
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be removed without removing"
)
@click.pass_context
def uninstall(ctx, packages, dry_run):
    """Remove APM packages from apm.yml and apm_modules (like npm uninstall).

    This command removes packages from both the apm.yml dependencies list
    and the apm_modules/ directory. It's the opposite of 'apm install <package>'.

    Examples:
        apm uninstall acme/my-package                # Remove one package
        apm uninstall org/pkg1 org/pkg2              # Remove multiple packages
        apm uninstall acme/my-package --dry-run      # Show what would be removed
    """
    try:
        # Check if apm.yml exists
        if not Path("apm.yml").exists():
            _rich_error("No apm.yml found. Run 'apm init' first.")
            sys.exit(1)

        if not packages:
            _rich_error("No packages specified. Specify packages to uninstall.")
            sys.exit(1)

        _rich_info(f"Uninstalling {len(packages)} package(s)...")

        # Read current apm.yml
        import yaml

        apm_yml_path = Path("apm.yml")
        try:
            with open(apm_yml_path, "r") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            _rich_error(f"Failed to read apm.yml: {e}")
            sys.exit(1)

        # Ensure dependencies structure exists
        if "dependencies" not in data:
            data["dependencies"] = {}
        if "apm" not in data["dependencies"]:
            data["dependencies"]["apm"] = []

        current_deps = data["dependencies"]["apm"] or []
        packages_to_remove = []
        packages_not_found = []

        # Validate which packages can be removed
        for package in packages:
            # Validate package format (should be owner/repo or a git URL)
            if "/" not in package:
                _rich_error(
                    f"Invalid package format: {package}. Use 'owner/repo' format."
                )
                continue

            # Match by identity: parse the user input and each apm.yml entry,
            # compare using get_identity() which normalizes host differences.
            matched_dep = None
            try:
                pkg_ref = DependencyReference.parse(package)
                pkg_identity = pkg_ref.get_identity()
            except Exception:
                pkg_identity = package

            for dep_entry in current_deps:
                try:
                    if isinstance(dep_entry, str):
                        dep_ref = DependencyReference.parse(dep_entry)
                    elif isinstance(dep_entry, dict):
                        dep_ref = DependencyReference.parse_from_dict(dep_entry)
                    else:
                        continue
                    if dep_ref.get_identity() == pkg_identity:
                        matched_dep = dep_entry  # preserve original entry for removal
                        break
                except (ValueError, TypeError, AttributeError, KeyError):
                    # Fallback: exact string match
                    dep_str = dep_entry if isinstance(dep_entry, str) else str(dep_entry)
                    if dep_str == package:
                        matched_dep = dep_entry
                        break
                    pass

            if matched_dep is not None:
                packages_to_remove.append(matched_dep)
                _rich_info(f"✓ {package} - found in apm.yml")
            else:
                packages_not_found.append(package)
                _rich_warning(f"✗ {package} - not found in apm.yml")

        if not packages_to_remove:
            _rich_warning("No packages found in apm.yml to remove")
            return

        if dry_run:
            _rich_info(f"Dry run: Would remove {len(packages_to_remove)} package(s):")
            apm_modules_dir = Path("apm_modules")
            for pkg in packages_to_remove:
                _rich_info(f"  - {pkg} from apm.yml")
                # Check if package exists in apm_modules
                try:
                    dep_ref = DependencyReference.parse(pkg)
                    package_path = dep_ref.get_install_path(apm_modules_dir)
                except ValueError:
                    package_path = apm_modules_dir / pkg.split("/")[-1]
                if apm_modules_dir.exists() and package_path.exists():
                    _rich_info(f"  - {pkg} from apm_modules/")

            # Show transitive deps that would be removed
            from apm_cli.deps.lockfile import LockFile, get_lockfile_path
            lockfile_path = get_lockfile_path(Path("."))
            lockfile = LockFile.read(lockfile_path)
            if lockfile:
                removed_repo_urls = builtins.set()
                for pkg in packages_to_remove:
                    try:
                        ref = DependencyReference.parse(pkg)
                        removed_repo_urls.add(ref.repo_url)
                    except ValueError:
                        removed_repo_urls.add(pkg)
                # Find transitive orphans
                queue = builtins.list(removed_repo_urls)
                potential_orphans = builtins.set()
                while queue:
                    parent_url = queue.pop()
                    for dep in lockfile.get_all_dependencies():
                        key = dep.get_unique_key()
                        if key in potential_orphans:
                            continue
                        if dep.resolved_by and dep.resolved_by == parent_url:
                            potential_orphans.add(key)
                            queue.append(dep.repo_url)
                if potential_orphans:
                    _rich_info(f"  Transitive dependencies that would be removed:")
                    for orphan_key in sorted(potential_orphans):
                        _rich_info(f"    - {orphan_key}")

            _rich_success("Dry run complete - no changes made")
            return

        # Remove packages from apm.yml
        for package in packages_to_remove:
            current_deps.remove(package)
            _rich_info(f"Removed {package} from apm.yml")

        # Update dependencies in apm.yml
        data["dependencies"]["apm"] = current_deps

        # Write back to apm.yml
        try:
            with open(apm_yml_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            _rich_success(
                f"Updated apm.yml (removed {len(packages_to_remove)} package(s))"
            )
        except Exception as e:
            _rich_error(f"Failed to write apm.yml: {e}")
            sys.exit(1)

        # Remove packages from apm_modules/
        apm_modules_dir = Path("apm_modules")
        removed_from_modules = 0

        # npm-style transitive dep cleanup: use lockfile to find orphaned transitive deps
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path
        lockfile_path = get_lockfile_path(Path("."))
        lockfile = LockFile.read(lockfile_path)

        if apm_modules_dir.exists():
            deleted_pkg_paths: list = []
            for package in packages_to_remove:
                # Parse package into DependencyReference to get canonical install path
                # This correctly handles virtual packages (owner/repo-packagename) vs
                # regular packages (owner/repo) and ADO paths (org/project/repo)
                try:
                    dep_ref = DependencyReference.parse(package)
                    package_path = dep_ref.get_install_path(apm_modules_dir)
                except ValueError:
                    # Fallback for invalid format: use raw path segments
                    repo_parts = package.split("/")
                    if len(repo_parts) >= 2:
                        package_path = apm_modules_dir.joinpath(*repo_parts)
                    else:
                        package_path = apm_modules_dir / package

                if package_path.exists():
                    try:
                        shutil.rmtree(package_path)
                        _rich_info(f"✓ Removed {package} from apm_modules/")
                        removed_from_modules += 1
                        deleted_pkg_paths.append(package_path)
                    except Exception as e:
                        _rich_error(
                            f"✗ Failed to remove {package} from apm_modules/: {e}"
                        )
                else:
                    _rich_warning(f"Package {package} not found in apm_modules/")

            # Batch parent cleanup — single bottom-up pass
            from apm_cli.integration.base_integrator import BaseIntegrator as _BI2
            _BI2.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        # npm-style transitive dependency cleanup: remove orphaned transitive deps
        # After removing the direct packages, check if they had transitive deps that
        # are no longer needed by any remaining package.
        if lockfile and apm_modules_dir.exists():
            # Collect the repo_urls of removed packages
            removed_repo_urls = builtins.set()
            for pkg in packages_to_remove:
                try:
                    ref = DependencyReference.parse(pkg)
                    removed_repo_urls.add(ref.repo_url)
                except ValueError:
                    removed_repo_urls.add(pkg)

            # Find all transitive deps resolved_by any removed package (recursive)
            def _find_transitive_orphans(lockfile, removed_urls):
                """Recursively find all transitive deps that are no longer needed."""
                orphans = builtins.set()
                queue = builtins.list(removed_urls)
                while queue:
                    parent_url = queue.pop()
                    for dep in lockfile.get_all_dependencies():
                        key = dep.get_unique_key()
                        if key in orphans:
                            continue
                        if dep.resolved_by and dep.resolved_by == parent_url:
                            orphans.add(key)
                            # This orphan's own transitives are also orphaned
                            queue.append(dep.repo_url)
                return orphans

            potential_orphans = _find_transitive_orphans(lockfile, removed_repo_urls)

            if potential_orphans:
                # Check which orphans are still needed by remaining packages
                # Re-read updated apm.yml to get remaining deps
                remaining_deps = builtins.set()
                try:
                    with open(apm_yml_path, "r") as f:
                        updated_data = yaml.safe_load(f) or {}
                    for dep_str in updated_data.get("dependencies", {}).get("apm", []) or []:
                        try:
                            ref = DependencyReference.parse(dep_str)
                            remaining_deps.add(ref.get_unique_key())
                        except ValueError:
                            remaining_deps.add(dep_str)
                except Exception:
                    pass

                # Also check remaining lockfile deps that are NOT orphaned
                for dep in lockfile.get_all_dependencies():
                    key = dep.get_unique_key()
                    if key not in potential_orphans and dep.repo_url not in removed_repo_urls:
                        remaining_deps.add(key)

                # Remove only true orphans (not needed by remaining deps)
                actual_orphans = potential_orphans - remaining_deps
                deleted_orphan_paths: list = []
                for orphan_key in actual_orphans:
                    orphan_dep = lockfile.get_dependency(orphan_key)
                    if not orphan_dep:
                        continue
                    try:
                        orphan_ref = DependencyReference.parse(orphan_key)
                        orphan_path = orphan_ref.get_install_path(apm_modules_dir)
                    except ValueError:
                        parts = orphan_key.split("/")
                        orphan_path = apm_modules_dir.joinpath(*parts) if len(parts) >= 2 else apm_modules_dir / orphan_key

                    if orphan_path.exists():
                        try:
                            shutil.rmtree(orphan_path)
                            _rich_info(f"✓ Removed transitive dependency {orphan_key} from apm_modules/")
                            removed_from_modules += 1
                            deleted_orphan_paths.append(orphan_path)
                        except Exception as e:
                            _rich_error(f"✗ Failed to remove transitive dep {orphan_key}: {e}")

                # Batch parent cleanup — single bottom-up pass
                from apm_cli.integration.base_integrator import BaseIntegrator as _BI
                _BI.cleanup_empty_parents(deleted_orphan_paths, stop_at=apm_modules_dir)

        # Collect deployed_files only for REMOVED packages (direct + transitive)
        # so sync_integration doesn't iterate paths from packages still installed.
        from apm_cli.integration.base_integrator import BaseIntegrator
        removed_keys = builtins.set()
        for pkg in packages_to_remove:
            try:
                ref = DependencyReference.parse(pkg)
                removed_keys.add(ref.get_unique_key())
            except ValueError:
                removed_keys.add(pkg)
        if 'actual_orphans' in locals():
            removed_keys.update(actual_orphans)
        all_deployed_files = builtins.set()
        if lockfile:
            for dep_key, dep in lockfile.dependencies.items():
                if dep_key in removed_keys:
                    all_deployed_files.update(dep.deployed_files)
        # Normalize path separators once
        all_deployed_files = BaseIntegrator.normalize_managed_files(all_deployed_files) or builtins.set()

        # Update lockfile: remove entries for all removed packages (direct + transitive)
        removed_orphan_keys = builtins.set()
        if lockfile and apm_modules_dir.exists() and 'actual_orphans' in locals():
            removed_orphan_keys = actual_orphans
        if lockfile:
            lockfile_updated = False
            for pkg in packages_to_remove:
                try:
                    ref = DependencyReference.parse(pkg)
                    key = ref.get_unique_key()
                except ValueError:
                    key = pkg
                if key in lockfile.dependencies:
                    del lockfile.dependencies[key]
                    lockfile_updated = True
            # Also remove orphaned transitive deps from lockfile
            for orphan_key in removed_orphan_keys:
                if orphan_key in lockfile.dependencies:
                    del lockfile.dependencies[orphan_key]
                    lockfile_updated = True
            if lockfile_updated:
                try:
                    if lockfile.dependencies:
                        lockfile.write(lockfile_path)
                    else:
                        # No deps left — remove lockfile
                        lockfile_path.unlink(missing_ok=True)
                except Exception:
                    pass

        # Sync integrations: remove all deployed files and re-integrate from remaining packages
        prompts_cleaned = 0
        agents_cleaned = 0
        commands_cleaned = 0
        skills_cleaned = 0
        hooks_cleaned = 0
        instructions_cleaned = 0

        try:
            from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType, validate_apm_package
            from apm_cli.integration.prompt_integrator import PromptIntegrator
            from apm_cli.integration.agent_integrator import AgentIntegrator
            from apm_cli.integration.skill_integrator import SkillIntegrator
            from apm_cli.integration.command_integrator import CommandIntegrator
            from apm_cli.integration.hook_integrator import HookIntegrator
            from apm_cli.integration.instruction_integrator import InstructionIntegrator

            apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
            project_root = Path(".")

            # Use pre-collected deployed_files (captured before lockfile entries were deleted)
            sync_managed = all_deployed_files if all_deployed_files else None

            # Pre-partition managed files by integration type — single O(M)
            # pass instead of 6× O(M) prefix scans inside each integrator.
            if sync_managed is not None:
                _buckets = BaseIntegrator.partition_managed_files(sync_managed)
            else:
                _buckets = None

            # Phase 1: Remove all APM-deployed files
            if Path(".github/prompts").exists():
                integrator = PromptIntegrator()
                result = integrator.sync_integration(apm_package, project_root,
                                                     managed_files=_buckets["prompts"] if _buckets else None)
                prompts_cleaned = result.get("files_removed", 0)

            if Path(".github/agents").exists():
                integrator = AgentIntegrator()
                result = integrator.sync_integration(apm_package, project_root,
                                                     managed_files=_buckets["agents_github"] if _buckets else None)
                agents_cleaned = result.get("files_removed", 0)

            if Path(".claude/agents").exists():
                integrator = AgentIntegrator()
                result = integrator.sync_integration_claude(apm_package, project_root,
                                                            managed_files=_buckets["agents_claude"] if _buckets else None)
                agents_cleaned += result.get("files_removed", 0)

            if Path(".github/skills").exists() or Path(".claude/skills").exists():
                integrator = SkillIntegrator()
                result = integrator.sync_integration(apm_package, project_root,
                                                     managed_files=_buckets["skills"] if _buckets else None)
                skills_cleaned = result.get("files_removed", 0)

            if Path(".claude/commands").exists():
                integrator = CommandIntegrator()
                result = integrator.sync_integration(apm_package, project_root,
                                                     managed_files=_buckets["commands"] if _buckets else None)
                commands_cleaned = result.get("files_removed", 0)

            # Clean hooks (.github/hooks/ and .claude/settings.json)
            hook_integrator_cleanup = HookIntegrator()
            result = hook_integrator_cleanup.sync_integration(apm_package, project_root,
                                                              managed_files=_buckets["hooks"] if _buckets else None)
            hooks_cleaned = result.get("files_removed", 0)

            # Clean instructions (.github/instructions/)
            if Path(".github/instructions").exists():
                integrator = InstructionIntegrator()
                result = integrator.sync_integration(apm_package, project_root,
                                                     managed_files=_buckets["instructions"] if _buckets else None)
                instructions_cleaned = result.get("files_removed", 0)

            # Phase 2: Re-integrate from remaining installed packages in apm_modules/
            prompt_integrator = PromptIntegrator()
            agent_integrator = AgentIntegrator()
            skill_integrator = SkillIntegrator()
            command_integrator = CommandIntegrator()
            hook_integrator_reint = HookIntegrator()
            instruction_integrator = InstructionIntegrator()

            for dep in apm_package.get_apm_dependencies():
                dep_ref = dep if hasattr(dep, 'repo_url') else None
                if not dep_ref:
                    continue
                # Build install path
                install_path = Path("apm_modules") / dep_ref.repo_url
                if dep_ref.is_virtual and dep_ref.virtual_path:
                    install_path = Path("apm_modules") / dep_ref.repo_url / dep_ref.virtual_path
                if not install_path.exists():
                    continue

                # Build minimal PackageInfo for re-integration
                result = validate_apm_package(install_path)
                pkg = result.package if result and result.package else None
                if not pkg:
                    continue
                pkg_info = PackageInfo(
                    package=pkg,
                    install_path=install_path,
                    dependency_ref=dep_ref,
                    package_type=result.package_type if result else None,
                )

                try:
                    if prompt_integrator.should_integrate(project_root):
                        prompt_integrator.integrate_package_prompts(pkg_info, project_root)
                    if agent_integrator.should_integrate(project_root):
                        agent_integrator.integrate_package_agents(pkg_info, project_root)
                        if Path(".claude").exists():
                            agent_integrator.integrate_package_agents_claude(pkg_info, project_root)
                    skill_integrator.integrate_package_skill(pkg_info, project_root)
                    if command_integrator.should_integrate(project_root):
                        command_integrator.integrate_package_commands(pkg_info, project_root)
                    hook_integrator_reint.integrate_package_hooks(pkg_info, project_root)
                    hook_integrator_reint.integrate_package_hooks_claude(pkg_info, project_root)
                    instruction_integrator.integrate_package_instructions(pkg_info, project_root)
                except Exception:
                    pass  # Best effort re-integration

        except Exception:
            pass  # Best effort cleanup — don't report false failures

        # Show cleanup feedback
        if prompts_cleaned > 0:
            _rich_info(f"✓ Cleaned up {prompts_cleaned} integrated prompt(s)")
        if agents_cleaned > 0:
            _rich_info(f"✓ Cleaned up {agents_cleaned} integrated agent(s)")
        if skills_cleaned > 0:
            _rich_info(f"✓ Cleaned up {skills_cleaned} skill(s)")
        if commands_cleaned > 0:
            _rich_info(f"✓ Cleaned up {commands_cleaned} command(s)")
        if hooks_cleaned > 0:
            _rich_info(f"✓ Cleaned up {hooks_cleaned} hook(s)")
        if instructions_cleaned > 0:
            _rich_info(f"✓ Cleaned up {instructions_cleaned} instruction(s)")

        # Final summary
        summary_lines = []
        summary_lines.append(
            f"Removed {len(packages_to_remove)} package(s) from apm.yml"
        )
        if removed_from_modules > 0:
            summary_lines.append(
                f"Removed {removed_from_modules} package(s) from apm_modules/"
            )

        _rich_success("Uninstall complete: " + ", ".join(summary_lines))

        if packages_not_found:
            _rich_warning(
                f"Note: {len(packages_not_found)} package(s) were not found in apm.yml"
            )

    except Exception as e:
        _rich_error(f"Error uninstalling packages: {e}")
        sys.exit(1)


def _install_apm_dependencies(
    apm_package: "APMPackage",
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: "builtins.list" = None,
    force: bool = False,
    parallel_downloads: int = 4,
):
    """Install APM package dependencies.

    Args:
        apm_package: Parsed APM package with dependencies
        update_refs: Whether to update existing packages to latest refs
        verbose: Show detailed installation information
        only_packages: If provided, only install these specific packages (not all from apm.yml)
        force: Whether to overwrite locally-authored files on collision
        parallel_downloads: Max concurrent downloads (0 disables parallelism)
    """
    if not APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    apm_deps = apm_package.get_apm_dependencies()
    if not apm_deps:
        return 0, 0, 0

    _rich_info(f"Installing APM dependencies ({len(apm_deps)})...")

    project_root = Path.cwd()
    
    # T5: Check for existing lockfile - use locked versions for reproducible installs
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    lockfile_path = get_lockfile_path(project_root)
    existing_lockfile = None
    if lockfile_path.exists() and not update_refs:
        existing_lockfile = LockFile.read(lockfile_path)
        if existing_lockfile and existing_lockfile.dependencies:
            _rich_info(f"Using apm.lock ({len(existing_lockfile.dependencies)} locked dependencies)")
    
    apm_modules_dir = project_root / "apm_modules"
    apm_modules_dir.mkdir(exist_ok=True)
    
    # Create downloader early so it can be used for transitive dependency resolution
    downloader = GitHubPackageDownloader()
    
    # Track direct dependency keys so the download callback can distinguish them from transitive
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in apm_deps)

    # Track paths already downloaded by the resolver callback to avoid re-downloading
    # Maps dep_key -> resolved_commit (SHA or None) so the cached path can use it
    callback_downloaded = {}

    # Create a download callback for transitive dependency resolution
    # This allows the resolver to fetch packages on-demand during tree building
    def download_callback(dep_ref, modules_dir):
        """Download a package during dependency resolution."""
        install_path = dep_ref.get_install_path(modules_dir)
        if install_path.exists():
            return install_path
        try:
            # Build repo_ref string - include host for GHE/ADO, plus reference if specified
            repo_ref = dep_ref.repo_url
            if dep_ref.host and dep_ref.host not in ("github.com", None):
                repo_ref = f"{dep_ref.host}/{dep_ref.repo_url}"
            if dep_ref.virtual_path:
                repo_ref = f"{repo_ref}/{dep_ref.virtual_path}"
            
            # T5: Use locked commit if available (reproducible installs)
            locked_ref = None
            if existing_lockfile:
                locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                    locked_ref = locked_dep.resolved_commit
            
            # Priority: locked commit > explicit reference > default branch
            if locked_ref:
                repo_ref = f"{repo_ref}#{locked_ref}"
            elif dep_ref.reference:
                repo_ref = f"{repo_ref}#{dep_ref.reference}"
            
            # Silent download - no progress display for transitive deps
            result = downloader.download_package(repo_ref, install_path)
            # Capture resolved commit SHA for lockfile
            resolved_sha = None
            if result and hasattr(result, 'resolved_reference') and result.resolved_reference:
                resolved_sha = result.resolved_reference.resolved_commit
            callback_downloaded[dep_ref.get_unique_key()] = resolved_sha
            return install_path
        except Exception as e:
            # Log but don't fail - allow resolution to continue
            if verbose:
                _rich_error(f"  └─ Failed to resolve transitive dep {dep_ref.repo_url}: {e}")
            return None
    
    # Resolve dependencies with transitive download support
    resolver = APMDependencyResolver(
        apm_modules_dir=apm_modules_dir,
        download_callback=download_callback
    )

    try:
        dependency_graph = resolver.resolve_dependencies(project_root)

        # Check for circular dependencies
        if dependency_graph.circular_dependencies:
            _rich_error("Circular dependencies detected:")
            for circular in dependency_graph.circular_dependencies:
                cycle_path = " → ".join(circular.cycle_path)
                _rich_error(f"  {cycle_path}")
            raise RuntimeError("Cannot install packages with circular dependencies")

        # Get flattened dependencies for installation
        flat_deps = dependency_graph.flattened_dependencies
        deps_to_install = flat_deps.get_installation_list()

        # If specific packages were requested, filter to only those
        if only_packages:
            # Build identity set from user-supplied package specs.
            # Accepts any input form: git URLs, FQDN, shorthand.
            only_identities = builtins.set()
            for p in only_packages:
                try:
                    ref = DependencyReference.parse(p)
                    only_identities.add(ref.get_identity())
                except Exception:
                    only_identities.add(p)

            deps_to_install = [
                dep for dep in deps_to_install
                if dep.get_identity() in only_identities
            ]

        if not deps_to_install:
            _rich_info("No APM dependencies to install", symbol="check")
            return 0, 0, 0

        # apm_modules directory already created above

        # Auto-detect target for integration (same logic as compile)
        from apm_cli.core.target_detection import (
            detect_target,
            should_integrate_vscode,
            should_integrate_claude,
            get_target_description,
        )

        # Get config target from apm.yml if available
        config_target = apm_package.target

        # Auto-create .github/ if neither .github/ nor .claude/ exists.
        # Per skill-strategy Decision 1, .github/skills/ is the standard skills location;
        # creating .github/ here ensures a consistent skills root and also enables
        # VSCode/Copilot integration by default (quick path to value), even for
        # projects that don't yet use .claude/.
        github_dir = project_root / ".github"
        claude_dir = project_root / ".claude"
        if not github_dir.exists() and not claude_dir.exists():
            github_dir.mkdir(parents=True, exist_ok=True)
            _rich_info(
                "Created .github/ as standard skills root (.github/skills/) and to enable VSCode/Copilot integration"
            )

        detected_target, detection_reason = detect_target(
            project_root=project_root,
            explicit_target=None,  # No explicit flag for install
            config_target=config_target,
        )

        # Determine which integrations to run based on detected target
        integrate_vscode = should_integrate_vscode(detected_target)
        integrate_claude = should_integrate_claude(detected_target)

        # Initialize integrators
        prompt_integrator = PromptIntegrator()
        agent_integrator = AgentIntegrator()
        from apm_cli.integration.skill_integrator import SkillIntegrator, should_install_skill
        from apm_cli.integration.command_integrator import CommandIntegrator
        from apm_cli.integration.hook_integrator import HookIntegrator
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        skill_integrator = SkillIntegrator()
        command_integrator = CommandIntegrator()
        hook_integrator = HookIntegrator()
        instruction_integrator = InstructionIntegrator()
        total_prompts_integrated = 0
        total_agents_integrated = 0
        total_skills_integrated = 0
        total_sub_skills_promoted = 0
        total_instructions_integrated = 0
        total_commands_integrated = 0
        total_hooks_integrated = 0
        total_links_resolved = 0

        # Collect installed packages for lockfile generation
        from apm_cli.deps.lockfile import LockFile, LockedDependency, get_lockfile_path
        installed_packages: List[tuple] = []  # List of (dep_ref, resolved_commit, depth, resolved_by)
        package_deployed_files: dict = {}  # dep_key → list of relative deployed paths

        # Build managed_files from existing lockfile for collision detection
        managed_files = builtins.set()
        existing_lockfile = LockFile.read(get_lockfile_path(project_root)) if project_root else None
        if existing_lockfile:
            for dep in existing_lockfile.dependencies.values():
                managed_files.update(dep.deployed_files)
        # Normalize path separators once for O(1) lookups in check_collision
        from apm_cli.integration.base_integrator import BaseIntegrator
        managed_files = BaseIntegrator.normalize_managed_files(managed_files)

        # Install each dependency with Rich progress display
        from rich.progress import (
            Progress,
            SpinnerColumn,
            TextColumn,
            BarColumn,
            TaskProgressColumn,
        )

        # downloader already created above for transitive resolution
        installed_count = 0

        # Phase 4 (#171): Parallel package downloads using ThreadPoolExecutor
        # Pre-download all non-cached packages in parallel for wall-clock speedup.
        # Results are stored and consumed by the sequential integration loop below.
        from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_completed

        _pre_download_results = {}   # dep_key -> PackageInfo
        _need_download = []
        for _pd_ref in deps_to_install:
            _pd_key = _pd_ref.get_unique_key()
            _pd_path = (apm_modules_dir / _pd_ref.alias) if _pd_ref.alias else _pd_ref.get_install_path(apm_modules_dir)
            # Skip if already downloaded during BFS resolution
            if _pd_key in callback_downloaded:
                continue
            # Skip if lockfile SHA matches local HEAD (Phase 5 check)
            if _pd_path.exists() and existing_lockfile and not update_refs:
                _pd_locked = existing_lockfile.get_dependency(_pd_key)
                if _pd_locked and _pd_locked.resolved_commit and _pd_locked.resolved_commit != "cached":
                    try:
                        from git import Repo as _PDGitRepo
                        if _PDGitRepo(_pd_path).head.commit.hexsha == _pd_locked.resolved_commit:
                            continue
                    except Exception:
                        pass
            # Build download ref (use locked commit for reproducibility)
            _pd_dlref = str(_pd_ref)
            if existing_lockfile and not update_refs:
                _pd_locked = existing_lockfile.get_dependency(_pd_key)
                if _pd_locked and _pd_locked.resolved_commit and _pd_locked.resolved_commit != "cached":
                    _pd_base = _pd_ref.repo_url
                    if _pd_ref.virtual_path:
                        _pd_base = f"{_pd_base}/{_pd_ref.virtual_path}"
                    _pd_dlref = f"{_pd_base}#{_pd_locked.resolved_commit}"
            _need_download.append((_pd_ref, _pd_path, _pd_dlref))

        if _need_download and parallel_downloads > 0:
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}[/cyan]"),
                BarColumn(),
                TaskProgressColumn(),
                transient=True,
            ) as _dl_progress:
                _max_workers = min(parallel_downloads, len(_need_download))
                with ThreadPoolExecutor(max_workers=_max_workers) as _executor:
                    _futures = {}
                    for _pd_ref, _pd_path, _pd_dlref in _need_download:
                        _pd_disp = str(_pd_ref) if _pd_ref.is_virtual else _pd_ref.repo_url
                        _pd_short = _pd_disp.split("/")[-1] if "/" in _pd_disp else _pd_disp
                        _pd_tid = _dl_progress.add_task(description=f"Fetching {_pd_short}", total=None)
                        _pd_fut = _executor.submit(
                            downloader.download_package, _pd_dlref, _pd_path,
                            progress_task_id=_pd_tid, progress_obj=_dl_progress,
                        )
                        _futures[_pd_fut] = (_pd_ref, _pd_tid, _pd_disp)
                    for _pd_fut in _futures_completed(_futures):
                        _pd_ref, _pd_tid, _pd_disp = _futures[_pd_fut]
                        _pd_key = _pd_ref.get_unique_key()
                        try:
                            _pd_info = _pd_fut.result()
                            _pre_download_results[_pd_key] = _pd_info
                            _dl_progress.update(_pd_tid, visible=False)
                            _dl_progress.refresh()
                        except Exception:
                            _dl_progress.remove_task(_pd_tid)
                            # Silent: sequential loop below will retry and report errors

        _pre_downloaded_keys = builtins.set(_pre_download_results.keys())

        # Create progress display for sequential integration
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,  # Progress bar disappears when done
        ) as progress:
            for dep_ref in deps_to_install:
                # Determine installation directory using namespaced structure
                # e.g., microsoft/apm-sample-package -> apm_modules/microsoft/apm-sample-package/
                # For virtual packages: owner/repo/prompts/file.prompt.md -> apm_modules/owner/repo-file/
                # For subdirectory packages: owner/repo/subdir -> apm_modules/owner/repo/subdir/
                if dep_ref.alias:
                    # If alias is provided, use it directly (assume user handles namespacing)
                    install_name = dep_ref.alias
                    install_path = apm_modules_dir / install_name
                else:
                    # Use the canonical install path from DependencyReference
                    install_path = dep_ref.get_install_path(apm_modules_dir)

                # npm-like behavior: Branches always fetch latest, only tags/commits use cache
                # Resolve git reference to determine type
                from apm_cli.models.apm_package import GitReferenceType

                resolved_ref = None
                if dep_ref.reference and dep_ref.get_unique_key() not in _pre_downloaded_keys:
                    try:
                        resolved_ref = downloader.resolve_git_reference(
                            f"{dep_ref.repo_url}@{dep_ref.reference}"
                        )
                    except Exception:
                        pass  # If resolution fails, skip cache (fetch latest)

                # Use cache only for tags and commits (not branches)
                is_cacheable = resolved_ref and resolved_ref.ref_type in [
                    GitReferenceType.TAG,
                    GitReferenceType.COMMIT,
                ]
                # Skip download if: already fetched by resolver callback, or cached tag/commit
                already_resolved = dep_ref.get_unique_key() in callback_downloaded
                # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
                lockfile_match = False
                if install_path.exists() and existing_lockfile and not update_refs:
                    locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                    if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                        try:
                            from git import Repo as GitRepo
                            local_repo = GitRepo(install_path)
                            if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                                lockfile_match = True
                        except Exception:
                            pass  # Not a git repo or invalid — fall through to download
                skip_download = install_path.exists() and (
                    (is_cacheable and not update_refs) or already_resolved or lockfile_match
                )

                if skip_download:
                    display_name = (
                        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
                    )
                    ref_str = f" @{dep_ref.reference}" if dep_ref.reference else ""
                    _rich_info(f"✓ {display_name}{ref_str} (cached)")

                    # Still need to integrate prompts for cached packages (zero-config behavior)
                    if integrate_vscode or integrate_claude:
                        try:
                            # Create PackageInfo from cached package
                            from apm_cli.models.apm_package import (
                                APMPackage,
                                PackageInfo,
                                PackageType,
                                ResolvedReference,
                                GitReferenceType,
                            )
                            from datetime import datetime

                            # Load package from apm.yml in install path
                            apm_yml_path = install_path / "apm.yml"
                            if apm_yml_path.exists():
                                cached_package = APMPackage.from_apm_yml(apm_yml_path)
                                # Ensure source is set to the repo URL for sync matching
                                if not cached_package.source:
                                    cached_package.source = dep_ref.repo_url
                            else:
                                # Virtual package or no apm.yml - create minimal package
                                cached_package = APMPackage(
                                    name=dep_ref.repo_url.split("/")[-1],
                                    version="unknown",
                                    package_path=install_path,
                                    source=dep_ref.repo_url,
                                )

                            # Create basic resolved reference for cached packages
                            resolved_ref = ResolvedReference(
                                original_ref=dep_ref.reference or "default",
                                ref_type=GitReferenceType.BRANCH,
                                resolved_commit="cached",  # Mark as cached since we don't know exact commit
                                ref_name=dep_ref.reference or "default",
                            )

                            cached_package_info = PackageInfo(
                                package=cached_package,
                                install_path=install_path,
                                resolved_reference=resolved_ref,
                                installed_at=datetime.now().isoformat(),
                                dependency_ref=dep_ref,  # Store for canonical dependency string
                            )

                            # Detect package_type from disk contents so
                            # skill integration is not silently skipped
                            skill_md_exists = (install_path / "SKILL.md").exists()
                            apm_yml_exists = (install_path / "apm.yml").exists()
                            if skill_md_exists and apm_yml_exists:
                                cached_package_info.package_type = PackageType.HYBRID
                            elif skill_md_exists:
                                cached_package_info.package_type = PackageType.CLAUDE_SKILL
                            elif apm_yml_exists:
                                cached_package_info.package_type = PackageType.APM_PACKAGE

                            # Collect for lockfile (cached packages still need to be tracked)
                            node = dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
                            depth = node.depth if node else 1
                            resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
                            # Get commit SHA: callback capture > existing lockfile > explicit reference
                            dep_key = dep_ref.get_unique_key()
                            cached_commit = callback_downloaded.get(dep_key)
                            if not cached_commit and existing_lockfile:
                                locked_dep = existing_lockfile.get_dependency(dep_key)
                                if locked_dep:
                                    cached_commit = locked_dep.resolved_commit
                            if not cached_commit:
                                cached_commit = dep_ref.reference
                            installed_packages.append((dep_ref, cached_commit, depth, resolved_by))
                            dep_deployed: list = []  # collect deployed paths for this package

                            # VSCode + Claude integration (prompts + agents)
                            if integrate_vscode or integrate_claude:
                                # Integrate prompts
                                prompt_result = (
                                    prompt_integrator.integrate_package_prompts(
                                        cached_package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if prompt_result.files_integrated > 0:
                                    total_prompts_integrated += (
                                        prompt_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {prompt_result.files_integrated} prompts integrated → .github/prompts/"
                                    )
                                if prompt_result.files_updated > 0:
                                    _rich_info(
                                        f"  └─ {prompt_result.files_updated} prompts updated"
                                    )
                                # Track links resolved
                                total_links_resolved += prompt_result.links_resolved
                                for tp in prompt_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                                # Integrate agents
                                agent_result = (
                                    agent_integrator.integrate_package_agents(
                                        cached_package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if agent_result.files_integrated > 0:
                                    total_agents_integrated += (
                                        agent_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {agent_result.files_integrated} agents integrated → .github/agents/"
                                    )
                                if agent_result.files_updated > 0:
                                    _rich_info(
                                        f"  └─ {agent_result.files_updated} agents updated"
                                    )
                                # Track links resolved
                                total_links_resolved += agent_result.links_resolved
                                for tp in agent_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                            # Skill integration (works for both VSCode and Claude)
                            # Skills go to .github/skills/ (primary) and .claude/skills/ (if .claude/ exists)
                            if integrate_vscode or integrate_claude:
                                skill_result = skill_integrator.integrate_package_skill(
                                    cached_package_info, project_root
                                )
                                if skill_result.skill_created:
                                    total_skills_integrated += 1
                                    _rich_info(
                                        f"  └─ Skill integrated → .github/skills/"
                                    )
                                if skill_result.sub_skills_promoted > 0:
                                    total_sub_skills_promoted += skill_result.sub_skills_promoted
                                    _rich_info(
                                        f"  └─ {skill_result.sub_skills_promoted} skill(s) integrated → .github/skills/"
                                    )
                                for tp in skill_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                            # Integrate instructions → .github/instructions/
                            if integrate_vscode:
                                instruction_result = (
                                    instruction_integrator.integrate_package_instructions(
                                        cached_package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if instruction_result.files_integrated > 0:
                                    total_instructions_integrated += (
                                        instruction_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {instruction_result.files_integrated} instruction(s) integrated → .github/instructions/"
                                    )
                                total_links_resolved += instruction_result.links_resolved
                                for tp in instruction_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                            # Claude-specific integration (agents + commands)
                            if integrate_claude:
                                # Integrate agents to .claude/agents/
                                claude_agent_result = (
                                    agent_integrator.integrate_package_agents_claude(
                                        cached_package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if claude_agent_result.files_integrated > 0:
                                    total_agents_integrated += (
                                        claude_agent_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {claude_agent_result.files_integrated} agents integrated → .claude/agents/"
                                    )
                                total_links_resolved += claude_agent_result.links_resolved
                                for tp in claude_agent_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                                # Generate Claude commands from prompts
                                command_result = (
                                    command_integrator.integrate_package_commands(
                                        cached_package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if command_result.files_integrated > 0:
                                    total_commands_integrated += (
                                        command_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {command_result.files_integrated} commands integrated → .claude/commands/"
                                    )
                                if command_result.files_updated > 0:
                                    _rich_info(
                                        f"  └─ {command_result.files_updated} commands updated"
                                    )
                                total_links_resolved += command_result.links_resolved
                                for tp in command_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                            # Hook integration (target-aware)
                            if integrate_vscode:
                                hook_result = hook_integrator.integrate_package_hooks(
                                    cached_package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                                if hook_result.hooks_integrated > 0:
                                    total_hooks_integrated += hook_result.hooks_integrated
                                    _rich_info(
                                        f"  └─ {hook_result.hooks_integrated} hook(s) integrated → .github/hooks/"
                                    )
                                for tp in hook_result.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())
                            if integrate_claude:
                                hook_result_claude = hook_integrator.integrate_package_hooks_claude(
                                    cached_package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                                if hook_result_claude.hooks_integrated > 0:
                                    total_hooks_integrated += hook_result_claude.hooks_integrated
                                    _rich_info(
                                        f"  └─ {hook_result_claude.hooks_integrated} hook(s) integrated → .claude/settings.json"
                                    )
                                for tp in hook_result_claude.target_paths:
                                    dep_deployed.append(tp.relative_to(project_root).as_posix())

                            # Record deployed files for this package
                            package_deployed_files[dep_key] = dep_deployed
                        except Exception as e:
                            # Don't fail installation if integration fails
                            _rich_warning(
                                f"  ⚠ Failed to integrate primitives from cached package: {e}"
                            )

                    continue

                # Download the package with progress feedback
                try:
                    display_name = (
                        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
                    )
                    short_name = (
                        display_name.split("/")[-1]
                        if "/" in display_name
                        else display_name
                    )

                    # Create a progress task for this download
                    task_id = progress.add_task(
                        description=f"Fetching {short_name}",
                        total=None,  # Indeterminate initially; git will update with actual counts
                    )

                    # T5: Build download ref - use locked commit if available
                    download_ref = str(dep_ref)
                    if existing_lockfile and not update_refs:
                        locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                        if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                            # Override with locked commit for reproducible install
                            base_ref = dep_ref.repo_url
                            if dep_ref.virtual_path:
                                base_ref = f"{base_ref}/{dep_ref.virtual_path}"
                            download_ref = f"{base_ref}#{locked_dep.resolved_commit}"

                    # Phase 4 (#171): Use pre-downloaded result if available
                    _dep_key = dep_ref.get_unique_key()
                    if _dep_key in _pre_download_results:
                        package_info = _pre_download_results[_dep_key]
                    else:
                        # Fallback: sequential download (should rarely happen)
                        package_info = downloader.download_package(
                            download_ref,
                            install_path,
                            progress_task_id=task_id,
                            progress_obj=progress,
                        )

                    # CRITICAL: Hide progress BEFORE printing success message to avoid overlap
                    progress.update(task_id, visible=False)
                    progress.refresh()  # Force immediate refresh to hide the bar

                    installed_count += 1
                    _rich_success(f"✓ {display_name}")

                    # Collect for lockfile: get resolved commit and depth
                    resolved_commit = None
                    if hasattr(package_info, 'resolved_reference') and package_info.resolved_reference:
                        resolved_commit = package_info.resolved_reference.resolved_commit
                    # Get depth from dependency tree
                    node = dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
                    depth = node.depth if node else 1
                    resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
                    installed_packages.append((dep_ref, resolved_commit, depth, resolved_by))
                    dep_deployed_fresh: list = []  # collect deployed paths for this package

                    # Show package type in verbose mode
                    if verbose and hasattr(package_info, "package_type"):
                        from apm_cli.models.apm_package import PackageType

                        package_type = package_info.package_type
                        if package_type == PackageType.CLAUDE_SKILL:
                            _rich_info(
                                f"  └─ Package type: Skill (SKILL.md detected)"
                            )
                        elif package_type == PackageType.MARKETPLACE_PLUGIN:
                            _rich_info(
                                f"  └─ Package type: Marketplace Plugin (plugin.json detected)"
                            )
                        elif package_type == PackageType.HYBRID:
                            _rich_info(
                                f"  └─ Package type: Hybrid (apm.yml + SKILL.md)"
                            )
                        elif package_type == PackageType.APM_PACKAGE:
                            _rich_info(f"  └─ Package type: APM Package (apm.yml)")

                    # Auto-integrate prompts and agents if enabled
                    if integrate_vscode or integrate_claude:
                        try:
                            # Integrate prompts + agents (dual-target: .github/ + .claude/)
                            # Integrate prompts
                            prompt_result = (
                                prompt_integrator.integrate_package_prompts(
                                    package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                            )
                            if prompt_result.files_integrated > 0:
                                total_prompts_integrated += (
                                    prompt_result.files_integrated
                                )
                                _rich_info(
                                    f"  └─ {prompt_result.files_integrated} prompts integrated → .github/prompts/"
                                )
                            if prompt_result.files_updated > 0:
                                _rich_info(
                                    f"  └─ {prompt_result.files_updated} prompts updated"
                                )
                            # Track links resolved
                            total_links_resolved += prompt_result.links_resolved
                            for tp in prompt_result.target_paths:
                                dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Integrate agents
                            agent_result = (
                                agent_integrator.integrate_package_agents(
                                    package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                            )
                            if agent_result.files_integrated > 0:
                                total_agents_integrated += (
                                    agent_result.files_integrated
                                )
                                _rich_info(
                                    f"  └─ {agent_result.files_integrated} agents integrated → .github/agents/"
                                )
                            if agent_result.files_updated > 0:
                                _rich_info(
                                    f"  └─ {agent_result.files_updated} agents updated"
                                )
                            # Track links resolved
                            total_links_resolved += agent_result.links_resolved
                            for tp in agent_result.target_paths:
                                dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Skill integration (works for both VSCode and Claude)
                            # Skills go to .github/skills/ (primary) and .claude/skills/ (if .claude/ exists)
                            if integrate_vscode or integrate_claude:
                                skill_result = skill_integrator.integrate_package_skill(
                                    package_info, project_root
                                )
                                if skill_result.skill_created:
                                    total_skills_integrated += 1
                                    _rich_info(
                                        f"  └─ Skill integrated → .github/skills/"
                                    )
                                if skill_result.sub_skills_promoted > 0:
                                    total_sub_skills_promoted += skill_result.sub_skills_promoted
                                    _rich_info(
                                        f"  └─ {skill_result.sub_skills_promoted} skill(s) integrated → .github/skills/"
                                    )
                                for tp in skill_result.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Integrate instructions → .github/instructions/
                            if integrate_vscode:
                                instruction_result = (
                                    instruction_integrator.integrate_package_instructions(
                                        package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if instruction_result.files_integrated > 0:
                                    total_instructions_integrated += (
                                        instruction_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {instruction_result.files_integrated} instruction(s) integrated → .github/instructions/"
                                    )
                                total_links_resolved += instruction_result.links_resolved
                                for tp in instruction_result.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Claude-specific integration (agents + commands)
                            if integrate_claude:
                                # Integrate agents to .claude/agents/
                                claude_agent_result = (
                                    agent_integrator.integrate_package_agents_claude(
                                        package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if claude_agent_result.files_integrated > 0:
                                    total_agents_integrated += (
                                        claude_agent_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {claude_agent_result.files_integrated} agents integrated → .claude/agents/"
                                    )
                                total_links_resolved += claude_agent_result.links_resolved
                                for tp in claude_agent_result.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                                # Generate Claude commands from prompts
                                command_result = (
                                    command_integrator.integrate_package_commands(
                                        package_info, project_root,
                                        force=force, managed_files=managed_files,
                                    )
                                )
                                if command_result.files_integrated > 0:
                                    total_commands_integrated += (
                                        command_result.files_integrated
                                    )
                                    _rich_info(
                                        f"  └─ {command_result.files_integrated} commands integrated → .claude/commands/"
                                    )
                                if command_result.files_updated > 0:
                                    _rich_info(
                                        f"  └─ {command_result.files_updated} commands updated"
                                    )
                                total_links_resolved += command_result.links_resolved
                                for tp in command_result.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Hook integration (target-aware)
                            if integrate_vscode:
                                hook_result = hook_integrator.integrate_package_hooks(
                                    package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                                if hook_result.hooks_integrated > 0:
                                    total_hooks_integrated += hook_result.hooks_integrated
                                    _rich_info(
                                        f"  └─ {hook_result.hooks_integrated} hook(s) integrated → .github/hooks/"
                                    )
                                for tp in hook_result.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())
                            if integrate_claude:
                                hook_result_claude = hook_integrator.integrate_package_hooks_claude(
                                    package_info, project_root,
                                    force=force, managed_files=managed_files,
                                )
                                if hook_result_claude.hooks_integrated > 0:
                                    total_hooks_integrated += hook_result_claude.hooks_integrated
                                    _rich_info(
                                        f"  └─ {hook_result_claude.hooks_integrated} hook(s) integrated → .claude/settings.json"
                                    )
                                for tp in hook_result_claude.target_paths:
                                    dep_deployed_fresh.append(tp.relative_to(project_root).as_posix())

                            # Record deployed files for this package
                            package_deployed_files[dep_ref.get_unique_key()] = dep_deployed_fresh
                        except Exception as e:
                            # Don't fail installation if integration fails
                            _rich_warning(f"  ⚠ Failed to integrate primitives: {e}")

                except Exception as e:
                    display_name = (
                        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
                    )
                    # Remove the progress task on error
                    if "task_id" in locals():
                        progress.remove_task(task_id)
                    _rich_error(f"❌ Failed to install {display_name}: {e}")
                    # Continue with other packages instead of failing completely
                    continue

        # Update .gitignore
        _update_gitignore_for_apm_modules()

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        if installed_packages:
            try:
                lockfile = LockFile.from_installed_packages(installed_packages, dependency_graph)
                # Attach deployed_files to each LockedDependency
                for dep_key, dep_files in package_deployed_files.items():
                    if dep_key in lockfile.dependencies:
                        lockfile.dependencies[dep_key].deployed_files = dep_files
                lockfile_path = get_lockfile_path(project_root)
                lockfile.save(lockfile_path)
                _rich_info(f"Generated apm.lock with {len(lockfile.dependencies)} dependencies")
            except Exception as e:
                _rich_warning(f"Could not generate apm.lock: {e}")

        # Show link resolution stats if any were resolved
        if total_links_resolved > 0:
            _rich_info(f"✓ Resolved {total_links_resolved} context file links")

        # Show Claude commands stats if any were integrated
        if total_commands_integrated > 0:
            _rich_info(f"✓ Integrated {total_commands_integrated} command(s)")

        # Show hooks stats if any were integrated
        if total_hooks_integrated > 0:
            _rich_info(f"✓ Integrated {total_hooks_integrated} hook(s)")

        # Show instructions stats if any were integrated
        if total_instructions_integrated > 0:
            _rich_info(f"✓ Integrated {total_instructions_integrated} instruction(s)")

        _rich_success(f"Installed {installed_count} APM dependencies")

        return installed_count, total_prompts_integrated, total_agents_integrated

    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")


def _collect_transitive_mcp_deps(apm_modules_dir: Path, lock_path: Path = None, trust_private: bool = False) -> list:
    """Collect MCP dependencies from resolved APM packages listed in apm.lock.

    Only scans apm.yml files for packages present in apm.lock to avoid
    picking up stale/orphaned packages from previous installs.
    Falls back to scanning all apm.yml files if no lock file is available.

    Self-defined servers (registry: false) from transitive packages are
    skipped with a warning unless trust_private is True.

    Args:
        apm_modules_dir: Path to the apm_modules directory.
        lock_path: Path to the apm.lock file (optional).
        trust_private: When True, include self-defined servers from transitive
            packages without requiring re-declaration.

    Returns:
        List of MCPDependency entries from transitive packages.
    """
    if not apm_modules_dir.exists():
        return []

    from apm_cli.models.apm_package import APMPackage

    # Build set of expected apm.yml paths from apm.lock
    locked_paths = None
    if lock_path and lock_path.exists():
        lockfile = LockFile.read(lock_path)
        if lockfile is not None:
            locked_paths = builtins.set()
            for dep in lockfile.get_all_dependencies():
                if dep.repo_url:
                    yml = apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml" if dep.virtual_path else apm_modules_dir / dep.repo_url / "apm.yml"
                    locked_paths.add(yml.resolve())

    # Prefer iterating lock-derived paths directly (existing files only).
    # Fall back to full scan only when lock parsing is unavailable.
    if locked_paths is not None:
        apm_yml_paths = [path for path in sorted(locked_paths) if path.exists()]
    else:
        apm_yml_paths = apm_modules_dir.rglob("apm.yml")

    collected = []
    for apm_yml_path in apm_yml_paths:
        try:
            pkg = APMPackage.from_apm_yml(apm_yml_path)
            mcp = pkg.get_mcp_dependencies()
            if mcp:
                for dep in mcp:
                    if hasattr(dep, 'is_self_defined') and dep.is_self_defined:
                        if trust_private:
                            _rich_info(
                                f"Trusting self-defined MCP server '{dep.name}' "
                                f"from transitive package '{pkg.name}' (--trust-transitive-mcp)"
                            )
                        else:
                            _rich_warning(
                                f"Transitive package '{pkg.name}' declares self-defined "
                                f"MCP server '{dep.name}' (registry: false). "
                                f"Re-declare it in your apm.yml or use --trust-transitive-mcp."
                            )
                            continue
                    collected.append(dep)
        except Exception:
            # Skip packages that fail to parse
            continue
    return collected


def _deduplicate_mcp_deps(deps: list) -> list:
    """Deduplicate a list of MCPDependency objects.

    Deduplicates by name; first occurrence wins (root deps listed before
    transitive, so root overlays take precedence).

    Args:
        deps: List of MCPDependency entries.

    Returns:
        Deduplicated list preserving insertion order.
    """
    seen_names: builtins.set = builtins.set()
    result = []
    for dep in deps:
        if hasattr(dep, 'name'):
            name = dep.name
        elif isinstance(dep, dict):
            name = dep.get("name", "")
        else:
            name = str(dep)
        if not name:
            if dep not in result:
                result.append(dep)
            continue
        if name not in seen_names:
            seen_names.add(name)
            result.append(dep)
    return result


def _build_self_defined_server_info(dep) -> dict:
    """Build a synthetic server_info dict from a self-defined MCPDependency.

    This mimics the structure returned by the MCP registry so that existing
    adapter code (configure_mcp_server / _format_server_config) can consume
    self-defined deps without changes.
    """
    info: dict = {"name": dep.name}

    if dep.transport in ("http", "sse", "streamable-http"):
        # Build as a remote endpoint
        remote = {
            "transport_type": dep.transport,
            "url": dep.url or "",
        }
        if dep.headers:
            remote["headers"] = [
                {"name": k, "value": v} for k, v in dep.headers.items()
            ]
        info["remotes"] = [remote]
    else:
        # Build as a stdio package
        env_vars = []
        if dep.env:
            env_vars = [
                {"name": k, "description": "", "required": True}
                for k in dep.env
            ]

        runtime_args = []
        if dep.args:
            if isinstance(dep.args, builtins.list):
                runtime_args = [
                    {"is_required": True, "value_hint": a} for a in dep.args
                ]
            elif isinstance(dep.args, builtins.dict):
                runtime_args = [
                    {"is_required": True, "value_hint": v} for v in dep.args.values()
                ]

        info["packages"] = [
            {
                "runtime_hint": dep.command or dep.name,
                "name": dep.name,
                "registry_name": "",
                "runtime_arguments": runtime_args,
                "package_arguments": [],
                "environment_variables": env_vars,
            }
        ]

    # Embed tools override for adapters to pick up
    if dep.tools:
        info["_apm_tools_override"] = dep.tools

    return info


def _apply_mcp_overlay(server_info_cache: dict, dep) -> None:
    """Apply MCPDependency overlay fields onto cached server_info (in-place).

    Modifies the server_info dict in server_info_cache[dep.name] to reflect
    the overlay preferences (transport selection, env, headers, tools).
    """
    info = server_info_cache.get(dep.name)
    if not info:
        return

    # Transport overlay: select matching transport from available options
    if dep.transport:
        if dep.transport in ("http", "sse", "streamable-http"):
            # User prefers remote transport — remove packages to force remote path
            if "remotes" in info and info["remotes"]:
                info.pop("packages", None)
        elif dep.transport == "stdio":
            # User prefers stdio — remove remotes to force package path
            if "packages" in info and info["packages"]:
                info.pop("remotes", None)

    # Package type overlay: select specific package registry (npm, pypi, oci)
    if dep.package and "packages" in info:
        filtered = [p for p in info["packages"] if p.get("registry_name", "").lower() == dep.package.lower()]
        if filtered:
            info["packages"] = filtered

    # Headers overlay: merge into remote headers
    if dep.headers and "remotes" in info:
        for remote in info["remotes"]:
            existing_headers = remote.get("headers", [])
            if isinstance(existing_headers, builtins.list):
                for k, v in dep.headers.items():
                    existing_headers.append({"name": k, "value": v})
                remote["headers"] = existing_headers
            elif isinstance(existing_headers, builtins.dict):
                existing_headers.update(dep.headers)

    # Args overlay: merge into package runtime arguments
    if dep.args and "packages" in info:
        for pkg in info["packages"]:
            existing_args = pkg.get("runtime_arguments", [])
            if isinstance(dep.args, builtins.list):
                for arg in dep.args:
                    existing_args.append({"value_hint": str(arg)})
            elif isinstance(dep.args, builtins.dict):
                for k, v in dep.args.items():
                    existing_args.append({"value_hint": f"--{k}={v}"})
            pkg["runtime_arguments"] = existing_args

    # Tools overlay: embed for adapters to pick up
    if dep.tools:
        info["_apm_tools_override"] = dep.tools

    # Warn about overlay fields not yet applied at install time
    if dep.version:
        warnings.warn(
            f"MCP overlay field 'version' on '{dep.name}' is not yet applied at install time and will be ignored.",
            stacklevel=2,
        )
    if isinstance(dep.registry, str):
        warnings.warn(
            f"MCP overlay field 'registry' on '{dep.name}' is not yet applied at install time and will be ignored.",
            stacklevel=2,
        )


def _install_mcp_dependencies(
    mcp_deps: list, runtime: str = None, exclude: str = None, verbose: bool = False
):
    """Install MCP dependencies using existing logic.

    Args:
        mcp_deps: List of MCP dependency entries (registry strings)
        runtime: Target specific runtime only
        exclude: Exclude specific runtime from installation
        verbose: Show detailed installation information

    Returns:
        int: Number of MCP servers newly configured
    """
    if not mcp_deps:
        _rich_warning("No MCP dependencies found in apm.yml")
        return 0

    # Split into registry-resolved and self-defined deps
    # Backward compat: plain strings are treated as registry deps
    registry_deps = [dep for dep in mcp_deps if isinstance(dep, str) or (hasattr(dep, 'is_registry_resolved') and dep.is_registry_resolved)]
    self_defined_deps = [dep for dep in mcp_deps if hasattr(dep, 'is_self_defined') and dep.is_self_defined]
    registry_dep_names = [dep.name if hasattr(dep, 'name') else dep for dep in registry_deps]
    registry_dep_map = {dep.name: dep for dep in registry_deps if hasattr(dep, 'name')}

    console = _get_console()

    # Start MCP section with clean header
    if console:
        try:
            from rich.panel import Panel
            from rich.text import Text

            header = Text()
            header.append("┌─ MCP Servers (", style="cyan")
            header.append(str(len(mcp_deps)), style="cyan bold")
            header.append(")", style="cyan")
            console.print(header)
        except Exception:
            _rich_info(f"Installing MCP dependencies ({len(mcp_deps)})...")
    else:
        _rich_info(f"Installing MCP dependencies ({len(mcp_deps)})...")

    # Runtime detection and multi-runtime installation (existing logic)
    if runtime:
        # Single runtime mode
        target_runtimes = [runtime]
        _rich_info(f"Targeting specific runtime: {runtime}")
    else:
        # NEW LOGIC: First get installed runtimes, then filter by scripts
        apm_config = _load_apm_config()

        # Step 1: Get all installed runtimes on the system
        try:
            from apm_cli.factory import ClientFactory
            from apm_cli.runtime.manager import RuntimeManager

            manager = RuntimeManager()
            installed_runtimes = []

            # Check each MCP-compatible runtime (prioritize Copilot CLI)
            for runtime_name in ["copilot", "codex", "vscode"]:
                try:
                    if runtime_name == "vscode":
                        # VS Code is special - check if it's installed
                        if shutil.which("code") is not None:
                            ClientFactory.create_client(
                                runtime_name
                            )  # Verify MCP support
                            installed_runtimes.append(runtime_name)
                    else:
                        # For other runtimes, use RuntimeManager
                        if manager.is_runtime_available(runtime_name):
                            ClientFactory.create_client(
                                runtime_name
                            )  # Verify MCP support
                            installed_runtimes.append(runtime_name)
                except (ValueError, ImportError):
                    # Runtime not supported or doesn't have MCP support
                    continue
        except ImportError:
            # Fallback to basic shutil check for known MCP runtimes (prioritize Copilot CLI)
            installed_runtimes = [
                rt
                for rt in ["copilot", "codex", "vscode"]
                if shutil.which(rt if rt != "vscode" else "code") is not None
            ]

        # Step 2: Get runtimes referenced in apm.yml scripts
        script_runtimes = _detect_runtimes_from_scripts(
            apm_config.get("scripts", {}) if apm_config else {}
        )

        # Step 3: Target runtimes that are BOTH installed AND referenced in scripts
        if script_runtimes:
            # Only install for runtimes that are installed AND used in scripts
            target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]

            # Show runtime detection details only in verbose mode
            if verbose:
                if console:
                    console.print("│  [cyan]ℹ️  Runtime Detection[/cyan]")
                    console.print(
                        f"│     └─ Installed: {', '.join(installed_runtimes)}"
                    )
                    console.print(
                        f"│     └─ Used in scripts: {', '.join(script_runtimes)}"
                    )
                    if target_runtimes:
                        console.print(
                            f"│     └─ Target: {', '.join(target_runtimes)} (available + used in scripts)"
                        )
                    console.print("│")
                else:
                    _rich_info(f"Installed runtimes: {', '.join(installed_runtimes)}")
                    _rich_info(f"Script runtimes: {', '.join(script_runtimes)}")
                    if target_runtimes:
                        _rich_info(f"Target runtimes: {', '.join(target_runtimes)}")

            if not target_runtimes:
                _rich_warning("Scripts reference runtimes that are not installed")
                _rich_info(
                    f"Install missing runtimes with: apm runtime setup <runtime>"
                )
        else:
            # No scripts reference any runtimes - install for all installed runtimes
            target_runtimes = installed_runtimes
            if target_runtimes:
                if verbose:
                    _rich_info(
                        f"No scripts detected, using all installed runtimes: {', '.join(target_runtimes)}"
                    )
            else:
                _rich_warning("No MCP-compatible runtimes installed")
                _rich_info("Install a runtime with: apm runtime setup copilot")

        # Apply exclusions
        if exclude:
            target_runtimes = [r for r in target_runtimes if r != exclude]

        # Fall back to VS Code only if no runtimes are installed at all
        if not target_runtimes and not installed_runtimes:
            target_runtimes = ["vscode"]
            _rich_info("No runtimes installed, using VS Code as fallback")

    # Use the new registry operations module for better server detection
    configured_count = 0

    # --- Registry-based deps ---
    if registry_dep_names:
        try:
            from apm_cli.registry.operations import MCPServerOperations

            operations = MCPServerOperations()

            # Early validation: check if all servers exist in registry (fail-fast like npm)
            if verbose:
                _rich_info(f"Validating {len(registry_deps)} registry servers...")
            valid_servers, invalid_servers = operations.validate_servers_exist(registry_dep_names)

            if invalid_servers:
                _rich_error(
                    f"Server(s) not found in registry: {', '.join(invalid_servers)}"
                )
                _rich_info("Run 'apm mcp search <query>' to find available servers")
                raise RuntimeError(
                    f"Cannot install {len(invalid_servers)} missing server(s)"
                )

            if valid_servers:
                # Check which valid servers actually need installation
                servers_to_install = operations.check_servers_needing_installation(
                    target_runtimes, valid_servers
                )
                already_configured_servers = [
                    dep for dep in valid_servers if dep not in servers_to_install
                ]

                if not servers_to_install:
                    # All already configured
                    if console:
                        for dep in already_configured_servers:
                            console.print(
                                f"│  [green]✓[/green] {dep} [dim](already configured)[/dim]"
                            )
                    else:
                        _rich_success("All registry MCP servers already configured")
                else:
                    # Surface already-configured servers distinctly from newly configured ones
                    if already_configured_servers:
                        if console:
                            for dep in already_configured_servers:
                                console.print(
                                    f"│  [green]✓[/green] {dep} [dim](already configured)[/dim]"
                                )
                        elif verbose:
                            _rich_info(
                                "Already configured registry MCP servers: "
                                f"{', '.join(already_configured_servers)}"
                            )

                    # Batch fetch server info once to avoid duplicate registry calls
                    if verbose:
                        _rich_info(f"Installing {len(servers_to_install)} servers...")
                    server_info_cache = operations.batch_fetch_server_info(servers_to_install)

                    # Apply overlays from MCPDependency fields onto fetched server_info
                    for server_name in servers_to_install:
                        dep = registry_dep_map.get(server_name)
                        if dep:
                            _apply_mcp_overlay(server_info_cache, dep)

                    # Collect both environment and runtime variables using cached server info
                    shared_env_vars = operations.collect_environment_variables(
                        servers_to_install, server_info_cache
                    )
                    # Merge overlay env vars (overlay values take precedence)
                    for server_name in servers_to_install:
                        dep = registry_dep_map.get(server_name)
                        if dep and dep.env:
                            shared_env_vars.update(dep.env)
                    shared_runtime_vars = operations.collect_runtime_variables(
                        servers_to_install, server_info_cache
                    )

                    # Install for each target runtime using cached server info and shared variables
                    for dep in servers_to_install:
                        if console:
                            console.print(f"│  [cyan]⬇️[/cyan]  {dep}")
                            console.print(
                                f"│     └─ Configuring for {', '.join([rt.title() for rt in target_runtimes])}..."
                            )

                        for rt in target_runtimes:
                            if verbose:
                                _rich_info(f"Configuring {rt}...")
                            _install_for_runtime(
                                rt,
                                [dep],  # Install one at a time for better output
                                shared_env_vars,
                                server_info_cache,
                                shared_runtime_vars,
                            )

                        if console:
                            console.print(
                                f"│  [green]✓[/green]  {dep} → {', '.join([rt.title() for rt in target_runtimes])}"
                            )
                        configured_count += 1

        except ImportError:
            _rich_warning("Registry operations not available")
            _rich_error("Cannot validate MCP servers without registry operations")
            raise RuntimeError("Registry operations module required for MCP installation")

    # --- Self-defined deps (registry: false) ---
    if self_defined_deps:
        for dep in self_defined_deps:
            synthetic_info = _build_self_defined_server_info(dep)
            self_defined_cache = {dep.name: synthetic_info}
            self_defined_env = dep.env or {}

            if console:
                transport_label = dep.transport or "stdio"
                console.print(f"│  [cyan]⬇️[/cyan]  {dep.name} [dim](self-defined, {transport_label})[/dim]")
                console.print(
                    f"│     └─ Configuring for {', '.join([rt.title() for rt in target_runtimes])}..."
                )

            for rt in target_runtimes:
                if verbose:
                    _rich_info(f"Configuring {dep.name} for {rt}...")
                _install_for_runtime(
                    rt,
                    [dep.name],
                    self_defined_env,
                    self_defined_cache,
                )

            if console:
                console.print(
                    f"│  [green]✓[/green]  {dep.name} → {', '.join([rt.title() for rt in target_runtimes])}"
                )
            configured_count += 1

    # Close the panel
    if console:
        if configured_count > 0:
            console.print(
                f"└─ [green]Configured {configured_count} server{'s' if configured_count != 1 else ''}[/green]"
            )
        else:
            console.print("└─ [green]All servers up to date[/green]")

    return configured_count





def _show_install_summary(
    apm_count: int, prompt_count: int, agent_count: int, mcp_count: int, apm_config
):
    """Show post-install summary.

    Args:
        apm_count: Number of APM packages installed
        prompt_count: Number of prompts integrated
        agent_count: Number of agents integrated
        mcp_count: Number of MCP servers configured
        apm_config: The apm.yml configuration dict
    """
    parts = []
    if apm_count > 0:
        parts.append(f"{apm_count} APM package(s)")
    if mcp_count > 0:
        parts.append(f"{mcp_count} MCP server(s)")
    if parts:
        _rich_success(f"Installation complete: {', '.join(parts)}")
    else:
        _rich_success("Installation complete")


def _update_gitignore_for_apm_modules():
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(".gitignore")
    apm_modules_pattern = "apm_modules/"

    # Read current .gitignore content
    current_content = []
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                current_content = [line.rstrip("\n\r") for line in f.readlines()]
        except Exception as e:
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

        _rich_info(f"Added {apm_modules_pattern} to .gitignore")
    except Exception as e:
        _rich_warning(f"Could not update .gitignore: {e}")


def _load_apm_config():
    """Load configuration from apm.yml."""
    if Path("apm.yml").exists():
        with open("apm.yml", "r") as f:
            yaml = _lazy_yaml()
            return yaml.safe_load(f)
    return None


def _detect_runtimes_from_scripts(scripts: dict) -> List[str]:
    """Extract runtime commands from apm.yml scripts."""
    import re
    import builtins

    # CRITICAL FIX: Use builtins.set explicitly to avoid Click command collision!
    # The 'set' name collides with the 'config set' Click subcommand
    detected = builtins.set()

    for script_name, command in scripts.items():
        # Simple regex matching for runtime commands
        if re.search(r"\bcopilot\b", command):
            detected.add("copilot")
        if re.search(r"\bcodex\b", command):
            detected.add("codex")
        if re.search(r"\bllm\b", command):
            detected.add("llm")

    return builtins.list(detected)


def _filter_available_runtimes(detected_runtimes: List[str]) -> List[str]:
    """Filter to only runtimes that are actually installed and support MCP."""
    from apm_cli.factory import ClientFactory

    # First filter to only MCP-compatible runtimes
    try:
        # Get supported client types from factory
        mcp_compatible = []
        for rt in detected_runtimes:
            try:
                ClientFactory.create_client(rt)
                mcp_compatible.append(rt)
            except ValueError:
                # Runtime not supported by MCP client factory
                continue

        # Then filter to only installed runtimes
        try:
            from apm_cli.runtime.manager import RuntimeManager

            manager = RuntimeManager()

            return [rt for rt in mcp_compatible if manager.is_runtime_available(rt)]
        except ImportError:
            # Fallback to basic shutil check
            available = []
            for rt in mcp_compatible:
                if shutil.which(rt):
                    available.append(rt)
            return available

    except ImportError:
        # If factory is not available, fall back to known MCP runtimes
        mcp_compatible = [rt for rt in detected_runtimes if rt in ["vscode", "copilot"]]

        return [rt for rt in mcp_compatible if shutil.which(rt)]


def _install_for_runtime(
    runtime: str,
    mcp_deps: List[str],
    shared_env_vars: dict = None,
    server_info_cache: dict = None,
    shared_runtime_vars: dict = None,
):
    """Install MCP dependencies for a specific runtime."""
    try:
        from apm_cli.core.operations import install_package
        from apm_cli.factory import ClientFactory

        # Get the appropriate client for the runtime
        client = ClientFactory.create_client(runtime)

        for dep in mcp_deps:
            click.echo(f"  Installing {dep}...")
            try:
                result = install_package(
                    runtime,
                    dep,
                    shared_env_vars=shared_env_vars,
                    server_info_cache=server_info_cache,
                    shared_runtime_vars=shared_runtime_vars,
                )
                # Only show warnings for actual failures, not skips due to conflicts
                if result["failed"]:
                    click.echo(f"  ✗ Failed to install {dep}")
                # Safe installer provides comprehensive feedback for success/skip cases
            except Exception as install_error:
                click.echo(f"  ✗ Failed to install {dep}: {install_error}")

    except ImportError as e:
        _rich_warning(f"Core operations not available for runtime {runtime}: {e}")
        _rich_info(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
    except ValueError as e:
        _rich_warning(f"Runtime {runtime} not supported: {e}")
        _rich_info(f"Supported runtimes: vscode, copilot, codex, llm")
    except Exception as e:
        _rich_error(f"Error installing for runtime {runtime}: {e}")


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


@cli.command(help="Run a script with parameters")
@click.argument("script_name", required=False)
@click.option("--param", "-p", multiple=True, help="Parameter in format name=value")
@click.pass_context
def run(ctx, script_name, param):
    """Run a script from apm.yml (uses 'start' script if no name specified)."""
    try:
        # If no script name specified, use 'start' script
        if not script_name:
            script_name = _get_default_script()
            if not script_name:
                _rich_error(
                    "No script specified and no 'start' script defined in apm.yml"
                )
                _rich_info("Available scripts:")
                scripts = _list_available_scripts()

                console = _get_console()
                if console:
                    try:
                        from rich.table import Table

                        # Show available scripts in a table
                        table = Table(show_header=False, box=None, padding=(0, 1))
                        table.add_column("Icon", style="cyan")
                        table.add_column("Script", style="highlight")
                        table.add_column("Command", style="white")

                        for name, command in scripts.items():
                            table.add_row("  ", name, command)

                        console.print(table)
                    except Exception:
                        for name, command in scripts.items():
                            click.echo(f"  - {HIGHLIGHT}{name}{RESET}: {command}")
                else:
                    for name, command in scripts.items():
                        click.echo(f"  - {HIGHLIGHT}{name}{RESET}: {command}")
                sys.exit(1)

        # Parse parameters
        params = {}
        for p in param:
            if "=" in p:
                param_name, value = p.split("=", 1)
                params[param_name] = value
                _rich_echo(f"  - {param_name}: {value}", style="muted")

        # Import and use script runner
        try:
            from apm_cli.core.script_runner import ScriptRunner

            script_runner = ScriptRunner()
            success = script_runner.run_script(script_name, params)

            if not success:
                _rich_error("Script execution failed")
                sys.exit(1)

            _rich_blank_line()
            _rich_success("Script executed successfully!", symbol="sparkles")

        except ImportError as ie:
            _rich_warning("Script runner not available yet")
            _rich_info(f"Import error: {ie}")
            _rich_info(f"Would run script: {script_name} with params {params}")
        except Exception as ee:
            _rich_error(f"Script execution error: {ee}")
            sys.exit(1)

    except Exception as e:
        _rich_error(f"Error running script: {e}")
        sys.exit(1)


@cli.command(help="Preview a script's compiled prompt files")
@click.argument("script_name", required=False)
@click.option("--param", "-p", multiple=True, help="Parameter in format name=value")
@click.pass_context
def preview(ctx, script_name, param):
    """Preview compiled prompt files for a script."""
    try:
        # If no script name specified, use 'start' script
        if not script_name:
            script_name = _get_default_script()
            if not script_name:
                _rich_error(
                    "No script specified and no 'start' script defined in apm.yml"
                )
                sys.exit(1)

        _rich_info(f"Previewing script: {script_name}", symbol="info")

        # Parse parameters
        params = {}
        for p in param:
            if "=" in p:
                param_name, value = p.split("=", 1)
                params[param_name] = value
                _rich_echo(f"  - {param_name}: {value}", style="muted")

        # Import and use script runner for preview
        try:
            from apm_cli.core.script_runner import ScriptRunner

            script_runner = ScriptRunner()

            # Get the script command
            scripts = script_runner.list_scripts()
            if script_name not in scripts:
                _rich_error(f"Script '{script_name}' not found")
                sys.exit(1)

            command = scripts[script_name]

            try:
                # Show original and compiled commands in panels
                _rich_panel(command, title="📄 Original command", style="blue")

                # Auto-compile prompts to show what would be executed
                compiled_command, compiled_prompt_files = (
                    script_runner._auto_compile_prompts(command, params)
                )

                if compiled_prompt_files:
                    _rich_panel(
                        compiled_command, title="⚡ Compiled command", style="green"
                    )
                else:
                    _rich_panel(
                        compiled_command,
                        title="⚡ Command (no prompt compilation)",
                        style="yellow",
                    )
                    _rich_warning(
                        f"No .prompt.md files found in command. APM only compiles files ending with '.prompt.md'"
                    )

                # Show compiled files if any .prompt.md files were processed
                if compiled_prompt_files:
                    file_list = []
                    for prompt_file in compiled_prompt_files:
                        output_name = (
                            Path(prompt_file).stem.replace(".prompt", "") + ".txt"
                        )
                        compiled_path = Path(".apm/compiled") / output_name
                        file_list.append(str(compiled_path))

                    files_content = "\n".join([f"📄 {file}" for file in file_list])
                    _rich_panel(
                        files_content, title="📁 Compiled prompt files", style="cyan"
                    )
                else:
                    _rich_panel(
                        "No .prompt.md files were compiled.\n\n"
                        + "APM only compiles files ending with '.prompt.md' extension.\n"
                        + "Other files are executed as-is by the runtime.",
                        title="ℹ️  Compilation Info",
                        style="cyan",
                    )

            except (ImportError, NameError):
                # Fallback display
                _rich_info("Original command:")
                click.echo(f"  {command}")

                compiled_command, compiled_prompt_files = (
                    script_runner._auto_compile_prompts(command, params)
                )

                if compiled_prompt_files:
                    _rich_info("Compiled command:")
                    click.echo(f"  {compiled_command}")

                    _rich_info("Compiled prompt files:")
                    for prompt_file in compiled_prompt_files:
                        output_name = (
                            Path(prompt_file).stem.replace(".prompt", "") + ".txt"
                        )
                        compiled_path = Path(".apm/compiled") / output_name
                        click.echo(f"  - {compiled_path}")
                else:
                    _rich_warning("Command (no prompt compilation):")
                    click.echo(f"  {compiled_command}")
                    _rich_info(
                        "APM only compiles files ending with '.prompt.md' extension."
                    )

            _rich_blank_line()
            _rich_success(
                f"Preview complete! Use 'apm run {script_name}' to execute.",
                symbol="sparkles",
            )

        except ImportError:
            _rich_warning("Script runner not available yet")

    except Exception as e:
        _rich_error(f"Error previewing script: {e}")
        sys.exit(1)


@cli.command(help="List available scripts in the current project")
@click.pass_context
def list(ctx):
    """List all available scripts from apm.yml."""
    try:
        scripts = _list_available_scripts()

        if not scripts:
            _rich_warning("No scripts found.")

            # Show helpful example in a panel
            example_content = """scripts:
  start: "codex run main.prompt.md"
  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini" """

            try:
                _rich_panel(
                    example_content,
                    title=f"{STATUS_SYMBOLS['info']} Add scripts to your apm.yml file",
                    style="blue",
                )
            except (ImportError, NameError):
                _rich_info("💡 Add scripts to your apm.yml file:")
                click.echo("scripts:")
                click.echo('  start: "codex run main.prompt.md"')
                click.echo('  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini"')
            return

        # Show default script if 'start' exists
        default_script = "start" if "start" in scripts else None

        console = _get_console()
        if console:
            try:
                from rich.table import Table

                # Create a nice table for scripts
                table = Table(
                    title="📋 Available Scripts",
                    show_header=True,
                    header_style="bold cyan",
                )
                table.add_column("", style="cyan", width=3)
                table.add_column("Script", style="bold white", min_width=12)
                table.add_column("Command", style="white")

                for name, command in scripts.items():
                    icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                    table.add_row(icon, name, command)

                console.print(table)

                if default_script:
                    console.print(
                        f"\n[muted]{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script (runs when no script name specified)[/muted]"
                    )

            except Exception:
                # Fallback to simple output
                _rich_info("Available scripts:")
                for name, command in scripts.items():
                    icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                    click.echo(f"  {icon} {HIGHLIGHT}{name}{RESET}: {command}")
                if default_script:
                    click.echo(
                        f"\n{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script"
                    )
        else:
            # Fallback to simple output
            _rich_info("Available scripts:")
            for name, command in scripts.items():
                icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                click.echo(f"  {icon} {HIGHLIGHT}{name}{RESET}: {command}")
            if default_script:
                click.echo(
                    f"\n{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script"
                )
            # Fallback to simple output
            _rich_info("Available scripts:")
            for name, command in scripts.items():
                prefix = "📍 " if name == default_script else "   "
                click.echo(f"{prefix}{HIGHLIGHT}{name}{RESET}: {command}")

            if default_script:
                _rich_info("📍 = default script (runs when no script name specified)")

    except Exception as e:
        _rich_error(f"Error listing scripts: {e}")
        sys.exit(1)


def _display_validation_errors(errors):
    """Display validation errors in a Rich table with actionable feedback."""
    try:
        console = _get_console()
        if console:
            from rich.table import Table

            error_table = Table(
                title="❌ Primitive Validation Errors",
                show_header=True,
                header_style="bold red",
            )
            error_table.add_column("File", style="bold red", min_width=20)
            error_table.add_column("Error", style="white", min_width=30)
            error_table.add_column("Suggestion", style="yellow", min_width=25)

            for error in errors:
                file_path = str(error) if hasattr(error, "__str__") else "Unknown"
                # Extract file path from error string if it contains file info
                if ":" in file_path:
                    parts = file_path.split(":", 1)
                    file_name = parts[0] if len(parts) > 1 else "Unknown"
                    error_msg = parts[1].strip() if len(parts) > 1 else file_path
                else:
                    file_name = "Unknown"
                    error_msg = file_path

                # Provide actionable suggestions based on error type
                suggestion = _get_validation_suggestion(error_msg)
                error_table.add_row(file_name, error_msg, suggestion)

            console.print(error_table)
            return

    except (ImportError, NameError):
        pass

    # Fallback to simple text output
    _rich_error("Validation errors found:")
    for error in errors:
        click.echo(f"  ❌ {error}")


def _get_validation_suggestion(error_msg):
    """Get actionable suggestions for validation errors."""
    if "Missing 'description'" in error_msg:
        return "Add 'description: Your description here' to frontmatter"
    elif "Missing 'applyTo'" in error_msg:
        return "Add 'applyTo: \"**/*.py\"' to frontmatter"
    elif "Empty content" in error_msg:
        return "Add markdown content below the frontmatter"
    else:
        return "Check primitive structure and frontmatter"


def _watch_mode(output, chatmode, no_links, dry_run):
    """Watch for changes in .apm/ directories and auto-recompile."""
    try:
        # Try to import watchdog for file system monitoring
        import time

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class APMFileHandler(FileSystemEventHandler):
            def __init__(self, output, chatmode, no_links, dry_run):
                self.output = output
                self.chatmode = chatmode
                self.no_links = no_links
                self.dry_run = dry_run
                self.last_compile = 0
                self.debounce_delay = 1.0  # 1 second debounce

            def on_modified(self, event):
                if event.is_directory:
                    return

                # Check if it's a relevant file
                if event.src_path.endswith(".md") or event.src_path.endswith("apm.yml"):

                    # Debounce rapid changes
                    current_time = time.time()
                    if current_time - self.last_compile < self.debounce_delay:
                        return

                    self.last_compile = current_time
                    self._recompile(event.src_path)

            def _recompile(self, changed_file):
                """Recompile after file change."""
                try:
                    _rich_info(f"File changed: {changed_file}", symbol="eyes")
                    _rich_info("Recompiling...", symbol="gear")

                    # Create configuration from apm.yml with overrides
                    config = CompilationConfig.from_apm_yml(
                        output_path=self.output if self.output != "AGENTS.md" else None,
                        chatmode=self.chatmode,
                        resolve_links=not self.no_links if self.no_links else None,
                        dry_run=self.dry_run,
                    )

                    # Create compiler and compile
                    compiler = AgentsCompiler(".")
                    result = compiler.compile(config)

                    if result.success:
                        if self.dry_run:
                            _rich_success(
                                "Recompilation successful (dry run)", symbol="sparkles"
                            )
                        else:
                            _rich_success(
                                f"Recompiled to {result.output_path}", symbol="sparkles"
                            )
                    else:
                        _rich_error("Recompilation failed")
                        for error in result.errors:
                            click.echo(f"  ❌ {error}")

                except Exception as e:
                    _rich_error(f"Error during recompilation: {e}")

        # Set up file watching
        event_handler = APMFileHandler(output, chatmode, no_links, dry_run)
        observer = Observer()

        # Watch patterns for APM files
        watch_paths = []

        # Check for .apm directory
        if Path(".apm").exists():
            observer.schedule(event_handler, ".apm", recursive=True)
            watch_paths.append(".apm/")

        # Check for .github/instructions and agents/chatmodes
        if Path(".github/instructions").exists():
            observer.schedule(event_handler, ".github/instructions", recursive=True)
            watch_paths.append(".github/instructions/")

        # Watch .github/agents/ (new standard)
        if Path(".github/agents").exists():
            observer.schedule(event_handler, ".github/agents", recursive=True)
            watch_paths.append(".github/agents/")

        # Watch .github/chatmodes/ (legacy)
        if Path(".github/chatmodes").exists():
            observer.schedule(event_handler, ".github/chatmodes", recursive=True)
            watch_paths.append(".github/chatmodes/")

        # Watch apm.yml if it exists
        if Path("apm.yml").exists():
            observer.schedule(event_handler, ".", recursive=False)
            watch_paths.append("apm.yml")

        if not watch_paths:
            _rich_warning("No APM directories found to watch")
            _rich_info("Run 'apm init' to create an APM project")
            return

        # Start watching
        observer.start()
        _rich_info(
            f"👀 Watching for changes in: {', '.join(watch_paths)}", symbol="eyes"
        )
        _rich_info("Press Ctrl+C to stop watching...", symbol="info")

        # Do initial compilation
        _rich_info("Performing initial compilation...", symbol="gear")

        config = CompilationConfig.from_apm_yml(
            output_path=output if output != "AGENTS.md" else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
        )

        compiler = AgentsCompiler(".")
        result = compiler.compile(config)

        if result.success:
            if dry_run:
                _rich_success(
                    "Initial compilation successful (dry run)", symbol="sparkles"
                )
            else:
                _rich_success(
                    f"Initial compilation complete: {result.output_path}",
                    symbol="sparkles",
                )
        else:
            _rich_error("Initial compilation failed")
            for error in result.errors:
                click.echo(f"  ❌ {error}")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            _rich_info("Stopped watching for changes", symbol="info")

        observer.join()

    except ImportError:
        _rich_error("Watch mode requires the 'watchdog' library")
        _rich_info("Install it with: uv pip install watchdog")
        _rich_info(
            "Or reinstall APM: uv pip install -e . (from the apm directory)"
        )
        sys.exit(1)
    except Exception as e:
        _rich_error(f"Error in watch mode: {e}")
        sys.exit(1)


@cli.command(help="🚀 Compile APM context into distributed AGENTS.md files")
@click.option(
    "--output",
    "-o",
    default="AGENTS.md",
    help="Output file path (for single-file mode)",
)
@click.option(
    "--target",
    "-t",
    type=click.Choice(["vscode", "agents", "claude", "all"]),
    default=None,
    help="Target platform: vscode/agents (AGENTS.md), claude (CLAUDE.md), or all. Auto-detects if not specified.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="🔍 Preview compilation without writing files (shows placement decisions)",
)
@click.option("--no-links", is_flag=True, help="Skip markdown link resolution")
@click.option("--chatmode", help="Chatmode to prepend to AGENTS.md files")
@click.option("--watch", is_flag=True, help="Auto-regenerate on changes")
@click.option("--validate", is_flag=True, help="Validate primitives without compiling")
@click.option(
    "--with-constitution/--no-constitution",
    default=True,
    show_default=True,
    help="Include Spec Kit constitution block at top if memory/constitution.md present",
)
# Distributed compilation options (Task 7)
@click.option(
    "--single-agents",
    is_flag=True,
    help="📄 Force single-file compilation (legacy mode)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="🔍 Show detailed source attribution and optimizer analysis",
)
@click.option(
    "--local-only",
    is_flag=True,
    help="🏠 Ignore dependencies, compile only local primitives",
)
@click.option(
    "--clean",
    is_flag=True,
    help="🧹 Remove orphaned AGENTS.md files that are no longer generated",
)
@click.pass_context
def compile(
    ctx,
    output,
    target,
    dry_run,
    no_links,
    chatmode,
    watch,
    validate,
    with_constitution,
    single_agents,
    verbose,
    local_only,
    clean,
):
    """Compile APM context into distributed AGENTS.md files.

    By default, uses distributed compilation to generate multiple focused AGENTS.md
    files across your directory structure following the Minimal Context Principle.

    Use --single-agents for traditional single-file compilation when needed.

    Target platforms:
    • vscode/agents: Generates AGENTS.md + .github/ structure (VSCode/GitHub Copilot)
    • claude: Generates CLAUDE.md + .claude/ structure (Claude Code)
    • all: Generates both targets (default)

    Advanced options:
    • --dry-run: Preview compilation without writing files (shows placement decisions)
    • --verbose: Show detailed source attribution and optimizer analysis
    • --local-only: Ignore dependencies, compile only local .apm/ primitives
    • --clean: Remove orphaned AGENTS.md files that are no longer generated
    """
    try:
        # Check if this is an APM project first
        from pathlib import Path

        if not Path("apm.yml").exists():
            _rich_error("❌ Not an APM project - no apm.yml found")
            _rich_info("💡 To initialize an APM project, run:")
            _rich_info("   apm init")
            sys.exit(1)

        # Check if there are any instruction files to compile
        from apm_cli.compilation.constitution import find_constitution

        apm_modules_exists = Path("apm_modules").exists()
        constitution_exists = find_constitution(Path(".")).exists()

        # Check if .apm directory has actual content
        apm_dir = Path(".apm")
        local_apm_has_content = apm_dir.exists() and (
            any(apm_dir.rglob("*.instructions.md"))
            or any(apm_dir.rglob("*.chatmode.md"))
        )

        # If no primitive sources exist, check deeper to provide better feedback
        if (
            not apm_modules_exists
            and not local_apm_has_content
            and not constitution_exists
        ):
            # Check if .apm directories exist but are empty
            has_empty_apm = (
                apm_dir.exists()
                and not any(apm_dir.rglob("*.instructions.md"))
                and not any(apm_dir.rglob("*.chatmode.md"))
            )

            if has_empty_apm:
                _rich_error("❌ No instruction files found in .apm/ directory")
                _rich_info("💡 To add instructions, create files like:")
                _rich_info("   .apm/instructions/coding-standards.instructions.md")
                _rich_info("   .apm/chatmodes/backend-engineer.chatmode.md")
            else:
                _rich_error("❌ No APM content found to compile")
                _rich_info("💡 To get started:")
                _rich_info("   1. Install APM dependencies: apm install <owner>/<repo>")
                _rich_info(
                    "   2. Or create local instructions: mkdir -p .apm/instructions"
                )
                _rich_info("   3. Then create .instructions.md or .chatmode.md files")

            if not dry_run:  # Don't exit on dry-run to allow testing
                sys.exit(1)

        # Validation-only mode
        if validate:
            _rich_info("Validating APM context...", symbol="gear")
            compiler = AgentsCompiler(".")
            try:
                primitives = discover_primitives(".")
            except Exception as e:
                _rich_error(f"Failed to discover primitives: {e}")
                _rich_info(f"💡 Error details: {type(e).__name__}")
                sys.exit(1)
            validation_errors = compiler.validate_primitives(primitives)
            if validation_errors:
                _display_validation_errors(validation_errors)
                _rich_error(f"Validation failed with {len(validation_errors)} errors")
                sys.exit(1)
            _rich_success("All primitives validated successfully!", symbol="sparkles")
            _rich_info(f"Validated {primitives.count()} primitives:")
            _rich_info(f"  • {len(primitives.chatmodes)} chatmodes")
            _rich_info(f"  • {len(primitives.instructions)} instructions")
            _rich_info(f"  • {len(primitives.contexts)} contexts")
            # Show MCP dependency validation count
            try:
                from apm_cli.models.apm_package import APMPackage
                apm_pkg = APMPackage.from_apm_yml(Path("apm.yml"))
                mcp_count = len(apm_pkg.get_mcp_dependencies())
                if mcp_count > 0:
                    _rich_info(f"  • {mcp_count} MCP dependencies")
            except Exception:
                pass
            return

        # Watch mode
        if watch:
            _watch_mode(output, chatmode, no_links, dry_run)
            return

        _rich_info("Starting context compilation...", symbol="cogs")

        # Auto-detect target if not explicitly provided
        from apm_cli.core.target_detection import detect_target, get_target_description

        # Get config target from apm.yml if available
        config_target = None
        try:
            from apm_cli.models.apm_package import APMPackage

            apm_pkg = APMPackage.from_apm_yml(Path("apm.yml"))
            config_target = apm_pkg.target
        except Exception:
            # No apm.yml or parsing error - proceed with auto-detection
            pass

        detected_target, detection_reason = detect_target(
            project_root=Path("."),
            explicit_target=target,
            config_target=config_target,
        )

        # Map 'minimal' to 'vscode' for the compiler (AGENTS.md only, no folder integration)
        effective_target = detected_target if detected_target != "minimal" else "vscode"

        # Build config with distributed compilation flags (Task 7)
        config = CompilationConfig.from_apm_yml(
            output_path=output if output != "AGENTS.md" else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
            single_agents=single_agents,
            trace=verbose,
            local_only=local_only,
            debug=verbose,
            clean_orphaned=clean,
            target=effective_target,
        )
        config.with_constitution = with_constitution

        # Handle distributed vs single-file compilation
        if config.strategy == "distributed" and not single_agents:
            # Show target-aware message with detection reason
            if detected_target == "minimal":
                _rich_info(f"Compiling for AGENTS.md only ({detection_reason})")
                _rich_info(
                    "💡 Create .github/ or .claude/ folder for full integration",
                    symbol="light_bulb",
                )
            elif detected_target == "vscode" or detected_target == "agents":
                _rich_info(
                    f"Compiling for AGENTS.md (VSCode/Copilot) - {detection_reason}"
                )
            elif detected_target == "claude":
                _rich_info(
                    f"Compiling for CLAUDE.md (Claude Code) - {detection_reason}"
                )
            else:  # "all"
                _rich_info(f"Compiling for AGENTS.md + CLAUDE.md - {detection_reason}")

            if dry_run:
                _rich_info(
                    "Dry run mode: showing placement without writing files",
                    symbol="eye",
                )
            if verbose:
                _rich_info(
                    "Verbose mode: showing source attribution and optimizer analysis",
                    symbol="magnifying_glass",
                )
        else:
            _rich_info("Using single-file compilation (legacy mode)", symbol="page")

        # Perform compilation
        compiler = AgentsCompiler(".")
        result = compiler.compile(config)

        if result.success:
            # Handle different compilation modes
            if config.strategy == "distributed" and not single_agents:
                # Distributed compilation results - output already shown by professional formatter
                # Just show final success message
                if dry_run:
                    # Success message for dry run already included in formatter output
                    pass
                else:
                    # Success message for actual compilation
                    _rich_success("Compilation completed successfully!", symbol="check")

            else:
                # Traditional single-file compilation - keep existing logic
                # Perform initial compilation in dry-run to get generated body (without constitution)
                intermediate_config = CompilationConfig(
                    output_path=config.output_path,
                    chatmode=config.chatmode,
                    resolve_links=config.resolve_links,
                    dry_run=True,  # force
                    with_constitution=config.with_constitution,
                    strategy="single-file",
                )
                intermediate_result = compiler.compile(intermediate_config)

                if intermediate_result.success:
                    # Perform constitution injection / preservation
                    from apm_cli.compilation.injector import ConstitutionInjector

                    injector = ConstitutionInjector(base_dir=".")
                    output_path = Path(config.output_path)
                    final_content, c_status, c_hash = injector.inject(
                        intermediate_result.content,
                        with_constitution=config.with_constitution,
                        output_path=output_path,
                    )

                    # Compute deterministic Build ID (12-char SHA256) over content with placeholder removed
                    import hashlib

                    from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER

                    lines = final_content.splitlines()
                    # Identify placeholder line index
                    try:
                        idx = lines.index(BUILD_ID_PLACEHOLDER)
                    except ValueError:
                        idx = None
                    hash_input_lines = [l for i, l in enumerate(lines) if i != idx]
                    hash_bytes = "\n".join(hash_input_lines).encode("utf-8")
                    build_id = hashlib.sha256(hash_bytes).hexdigest()[:12]
                    if idx is not None:
                        lines[idx] = f"<!-- Build ID: {build_id} -->"
                        final_content = "\n".join(lines) + (
                            "\n" if final_content.endswith("\n") else ""
                        )

                    if not dry_run:
                        # Only rewrite when content materially changes (creation, update, missing constitution case)
                        if c_status in ("CREATED", "UPDATED", "MISSING"):
                            try:
                                _atomic_write(output_path, final_content)
                            except OSError as e:
                                _rich_error(f"Failed to write final AGENTS.md: {e}")
                                sys.exit(1)
                        else:
                            _rich_info(
                                "No changes detected; preserving existing AGENTS.md for idempotency"
                            )

                    # Report success at the top
                    if dry_run:
                        _rich_success(
                            "Context compilation completed successfully (dry run)",
                            symbol="check",
                        )
                    else:
                        _rich_success(
                            f"Context compiled successfully to {output_path}",
                            symbol="sparkles",
                        )

                    stats = (
                        intermediate_result.stats
                    )  # timestamp removed; stats remain version + counts

                    # Add spacing before summary table
                    _rich_blank_line()

                    # Single comprehensive compilation summary table
                    try:
                        console = _get_console()
                        if console:
                            import os

                            from rich.table import Table

                            table = Table(
                                title="Compilation Summary",
                                show_header=True,
                                header_style="bold cyan",
                            )
                            table.add_column(
                                "Component", style="bold white", min_width=15
                            )
                            table.add_column("Count", style="cyan", min_width=8)
                            table.add_column("Details", style="white", min_width=20)

                            # Constitution row
                            constitution_details = f"Hash: {c_hash or '-'}"
                            table.add_row(
                                "Spec-kit Constitution", c_status, constitution_details
                            )

                            # Primitives rows
                            table.add_row(
                                "Instructions",
                                str(stats.get("instructions", 0)),
                                "✅ All validated",
                            )
                            table.add_row(
                                "Contexts",
                                str(stats.get("contexts", 0)),
                                "✅ All validated",
                            )
                            table.add_row(
                                "Chatmodes",
                                str(stats.get("chatmodes", 0)),
                                "✅ All validated",
                            )

                            # Output row with file size
                            try:
                                file_size = (
                                    os.path.getsize(output_path) if not dry_run else 0
                                )
                                size_str = (
                                    f"{file_size/1024:.1f}KB"
                                    if file_size > 0
                                    else "Preview"
                                )
                                output_details = f"{output_path.name} ({size_str})"
                            except:
                                output_details = f"{output_path.name}"

                            table.add_row("Output", "✨ SUCCESS", output_details)

                            console.print(table)
                        else:
                            # Fallback for no Rich console
                            _rich_info(
                                f"Processed {stats.get('primitives_found', 0)} primitives:"
                            )
                            _rich_info(
                                f"  • {stats.get('instructions', 0)} instructions"
                            )
                            _rich_info(f"  • {stats.get('contexts', 0)} contexts")
                            _rich_info(
                                f"Constitution status: {c_status} hash={c_hash or '-'}"
                            )
                    except Exception:
                        # Fallback for any errors
                        _rich_info(
                            f"Processed {stats.get('primitives_found', 0)} primitives:"
                        )
                        _rich_info(f"  • {stats.get('instructions', 0)} instructions")
                        _rich_info(f"  • {stats.get('contexts', 0)} contexts")
                        _rich_info(
                            f"Constitution status: {c_status} hash={c_hash or '-'}"
                        )

                    if dry_run:
                        preview = final_content[:500] + (
                            "..." if len(final_content) > 500 else ""
                        )
                        _rich_panel(
                            preview, title="📋 Generated Content Preview", style="cyan"
                        )
                    else:
                        next_steps = [
                            f"Review the generated {output} file",
                            "Install MCP dependencies: apm install",
                            "Execute agentic workflows: apm run <script> --param key=value",
                        ]
                        try:
                            console = _get_console()
                            if console:
                                from rich.panel import Panel

                                steps_content = "\n".join(
                                    f"• {step}" for step in next_steps
                                )
                                console.print(
                                    Panel(
                                        steps_content,
                                        title="💡 Next Steps",
                                        border_style="blue",
                                    )
                                )
                            else:
                                _rich_info("Next steps:")
                                for step in next_steps:
                                    click.echo(f"  • {step}")
                        except (ImportError, NameError):
                            _rich_info("Next steps:")
                            for step in next_steps:
                                click.echo(f"  • {step}")

        # Common error handling for both compilation modes
        # Note: Warnings are handled by professional formatters for distributed mode
        if config.strategy != "distributed" or single_agents:
            # Only show warnings for single-file mode (backward compatibility)
            if result.warnings:
                _rich_warning(
                    f"Compilation completed with {len(result.warnings)} warnings:"
                )
                for warning in result.warnings:
                    click.echo(f"  ⚠️  {warning}")

        if result.errors:
            _rich_error(f"Compilation failed with {len(result.errors)} errors:")
            for error in result.errors:
                click.echo(f"  ❌ {error}")
            sys.exit(1)

        # Check for orphaned packages after successful compilation
        try:
            orphaned_packages = _check_orphaned_packages()
            if orphaned_packages:
                _rich_blank_line()
                _rich_warning(
                    f"⚠️ Found {len(orphaned_packages)} orphaned package(s) that were included in compilation:"
                )
                for pkg in orphaned_packages:
                    _rich_info(f"  • {pkg}")
                _rich_info("💡 Run 'apm prune' to remove orphaned packages")
        except Exception:
            pass  # Continue if orphan check fails

    except ImportError as e:
        _rich_error(f"Compilation module not available: {e}")
        _rich_info("This might be a development environment issue.")
        sys.exit(1)
    except Exception as e:
        _rich_error(f"Error during compilation: {e}")
        sys.exit(1)


@cli.group(help="Configure APM CLI")
@click.pass_context
def config(ctx):
    """Configure APM CLI settings."""
    # If no subcommand, show current configuration
    if ctx.invoked_subcommand is None:
        try:
            # Lazy import rich table
            from rich.table import Table  # type: ignore

            console = _get_console()
            # Create configuration display
            config_table = Table(
                title="⚙️  Current APM Configuration",
                show_header=True,
                header_style="bold cyan",
            )
            config_table.add_column("Category", style="bold yellow", min_width=12)
            config_table.add_column("Setting", style="white", min_width=15)
            config_table.add_column("Value", style="cyan")

            # Show apm.yml if in project
            if Path("apm.yml").exists():
                apm_config = _load_apm_config()
                config_table.add_row(
                    "Project", "Name", apm_config.get("name", "Unknown")
                )
                config_table.add_row(
                    "", "Version", apm_config.get("version", "Unknown")
                )
                config_table.add_row(
                    "", "Entrypoint", apm_config.get("entrypoint", "None")
                )
                config_table.add_row(
                    "",
                    "MCP Dependencies",
                    str(len(apm_config.get("dependencies", {}).get("mcp", []))),
                )

                # Show compilation configuration
                compilation_config = apm_config.get("compilation", {})
                if compilation_config:
                    config_table.add_row(
                        "Compilation",
                        "Output",
                        compilation_config.get("output", "AGENTS.md"),
                    )
                    config_table.add_row(
                        "",
                        "Chatmode",
                        compilation_config.get("chatmode", "auto-detect"),
                    )
                    config_table.add_row(
                        "",
                        "Resolve Links",
                        str(compilation_config.get("resolve_links", True)),
                    )
                else:
                    config_table.add_row(
                        "Compilation", "Status", "Using defaults (no config)"
                    )
            else:
                config_table.add_row(
                    "Project", "Status", "Not in an APM project directory"
                )

            config_table.add_row("Global", "APM CLI Version", get_version())

            console.print(config_table)

        except (ImportError, NameError):
            # Fallback display
            _rich_info("Current APM Configuration:")

            if Path("apm.yml").exists():
                apm_config = _load_apm_config()
                click.echo(f"\n{HIGHLIGHT}Project (apm.yml):{RESET}")
                click.echo(f"  Name: {apm_config.get('name', 'Unknown')}")
                click.echo(f"  Version: {apm_config.get('version', 'Unknown')}")
                click.echo(f"  Entrypoint: {apm_config.get('entrypoint', 'None')}")
                click.echo(
                    f"  MCP Dependencies: {len(apm_config.get('dependencies', {}).get('mcp', []))}"
                )
            else:
                _rich_info("Not in an APM project directory")

            click.echo(f"\n{HIGHLIGHT}Global:{RESET}")
            click.echo(f"  APM CLI Version: {get_version()}")


@config.command(help="Set a configuration value")
@click.argument("key")
@click.argument("value")
def set(key, value):
    """Set a configuration value.

    Examples:
        apm config set auto-integrate false
        apm config set auto-integrate true
    """
    from apm_cli.config import set_auto_integrate

    if key == "auto-integrate":
        if value.lower() in ["true", "1", "yes"]:
            set_auto_integrate(True)
            _rich_success("Auto-integration enabled")
        elif value.lower() in ["false", "0", "no"]:
            set_auto_integrate(False)
            _rich_success("Auto-integration disabled")
        else:
            _rich_error(f"Invalid value '{value}'. Use 'true' or 'false'.")
            sys.exit(1)
    else:
        _rich_error(f"Unknown configuration key: '{key}'")
        _rich_info("Valid keys: auto-integrate")
        _rich_info(
            "This error may indicate a bug in command routing. Please report this issue."
        )
        sys.exit(1)


@config.command(help="Get a configuration value")
@click.argument("key", required=False)
def get(key):
    """Get a configuration value or show all configuration.

    Examples:
        apm config get auto-integrate
        apm config get
    """
    from apm_cli.config import get_config, get_auto_integrate

    if key:
        if key == "auto-integrate":
            value = get_auto_integrate()
            click.echo(f"auto-integrate: {value}")
        else:
            _rich_error(f"Unknown configuration key: '{key}'")
            _rich_info("Valid keys: auto-integrate")
            _rich_info(
                "This error may indicate a bug in command routing. Please report this issue."
            )
            sys.exit(1)
    else:
        # Show all config
        config_data = get_config()
        _rich_info("APM Configuration:")
        for k, v in config_data.items():
            # Map internal keys to user-friendly names
            if k == "auto_integrate":
                click.echo(f"  auto-integrate: {v}")
            else:
                click.echo(f"  {k}: {v}")


@cli.group(help="Manage AI runtimes")
def runtime():
    """Manage Coding Agent CLI runtime installations and configurations."""
    pass


def _atomic_write(path: Path, data: str) -> None:
    """Atomically write text data to path (best-effort)."""
    import os
    import tempfile

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


@cli.group(help="Browse MCP server registry")
def mcp():
    """Manage MCP server discovery and information."""
    pass


@runtime.command(help="Set up a runtime")
@click.argument("runtime_name", type=click.Choice(["copilot", "codex", "llm"]))
@click.option("--version", help="Specific version to install")
@click.option(
    "--vanilla",
    is_flag=True,
    help="Install runtime without APM configuration (uses runtime's native defaults)",
)
def setup(runtime_name, version, vanilla):
    """Set up an AI runtime with APM-managed installation."""
    try:
        _rich_info(f"Setting up {runtime_name} runtime...")

        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        success = manager.setup_runtime(runtime_name, version, vanilla)

        if not success:
            sys.exit(1)
        else:
            _rich_success(f"{runtime_name} runtime setup complete!", symbol="sparkles")

    except Exception as e:
        _rich_error(f"Error setting up runtime: {e}")
        sys.exit(1)


@runtime.command(help="List available and installed runtimes")
def list():
    """List all available runtimes and their installation status."""
    try:
        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        runtimes = manager.list_runtimes()

        try:
            from rich.table import Table  # type: ignore

            console = _get_console()
            # Create a nice table for runtimes
            table = Table(
                title="🤖 Available Runtimes",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Status", style="green", width=8)
            table.add_column("Runtime", style="bold white", min_width=10)
            table.add_column("Description", style="white")
            table.add_column("Details", style="muted")

            for name, info in runtimes.items():
                status_icon = (
                    STATUS_SYMBOLS["check"]
                    if info["installed"]
                    else STATUS_SYMBOLS["cross"]
                )
                status_text = "Installed" if info["installed"] else "Not installed"

                details = ""
                if info["installed"]:
                    details_list = [f"Path: {info['path']}"]
                    if "version" in info:
                        details_list.append(f"Version: {info['version']}")
                    details = "\n".join(details_list)

                table.add_row(
                    f"{status_icon} {status_text}", name, info["description"], details
                )

            console.print(table)

        except (ImportError, NameError):
            # Fallback to simple output
            _rich_info("Available Runtimes:")
            click.echo()

            for name, info in runtimes.items():
                status_icon = "✅" if info["installed"] else "❌"
                status_text = "Installed" if info["installed"] else "Not installed"

                click.echo(f"{status_icon} {HIGHLIGHT}{name}{RESET}")
                click.echo(f"   Description: {info['description']}")
                click.echo(f"   Status: {status_text}")

                if info["installed"]:
                    click.echo(f"   Path: {info['path']}")
                    if "version" in info:
                        click.echo(f"   Version: {info['version']}")

                click.echo()

    except Exception as e:
        _rich_error(f"Error listing runtimes: {e}")
        sys.exit(1)


@runtime.command(help="Remove an installed runtime")
@click.argument("runtime_name", type=click.Choice(["copilot", "codex", "llm"]))
@click.confirmation_option(prompt="Are you sure you want to remove this runtime?")
def remove(runtime_name):
    """Remove an installed runtime from APM management."""
    try:
        _rich_info(f"Removing {runtime_name} runtime...")

        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        success = manager.remove_runtime(runtime_name)

        if not success:
            sys.exit(1)
        else:
            _rich_success(
                f"{runtime_name} runtime removed successfully!", symbol="sparkles"
            )

    except Exception as e:
        _rich_error(f"Error removing runtime: {e}")
        sys.exit(1)


@runtime.command(help="Check which runtime will be used")
def status():
    """Show which runtime APM will use for execution."""
    try:
        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        available_runtime = manager.get_available_runtime()
        preference = manager.get_runtime_preference()

        try:
            # Create a nice status display
            status_content = f"""Preference order: {' → '.join(preference)}

Active runtime: {available_runtime if available_runtime else 'None available'}"""

            if not available_runtime:
                status_content += f"\n\n{STATUS_SYMBOLS['info']} Run 'apm runtime setup copilot' to install the primary runtime"

            _rich_panel(status_content, title="📊 Runtime Status", style="cyan")

        except (ImportError, NameError):
            # Fallback display
            _rich_info("Runtime Status:")
            click.echo()

            click.echo(f"Preference order: {' → '.join(preference)}")

            if available_runtime:
                _rich_success(f"Active runtime: {available_runtime}")
            else:
                _rich_error("No runtimes available")
                _rich_info(
                    "Run 'apm runtime setup copilot' to install the primary runtime"
                )

    except Exception as e:
        _rich_error(f"Error checking runtime status: {e}")
        sys.exit(1)


@mcp.command(help="Search MCP servers in registry")
@click.argument("query", required=True)
@click.option("--limit", default=10, help="Number of results to show")
@click.pass_context
def search(ctx, query, limit):
    """Search for MCP servers in the registry."""
    try:
        from apm_cli.registry.integration import RegistryIntegration

        registry = RegistryIntegration("https://api.mcp.github.com")
        servers = registry.search_packages(query)[:limit]

        console = _get_console()
        if not console:
            # Fallback for non-rich environments
            click.echo(f"Searching for: {query}")
            if not servers:
                click.echo("No servers found")
                return
            for server in servers:
                click.echo(f"  {server.get('name', 'Unknown')}")
                click.echo(f"    {server.get('description', 'No description')[:80]}")
            return

        # Professional header with search context
        console.print(f"\n[bold cyan]MCP Registry Search[/bold cyan]")
        console.print(f"[muted]Query: {query}[/muted]")

        if not servers:
            console.print(
                f"\n[yellow]⚠[/yellow] No MCP servers found matching '[bold]{query}[/bold]'"
            )
            console.print(
                "\n[muted]💡 Try broader search terms or check the spelling[/muted]"
            )
            return

        # Results summary
        total_shown = len(servers)
        console.print(
            f"\n[green]✓[/green] Found [bold]{total_shown}[/bold] MCP server{'s' if total_shown != 1 else ''}"
        )

        # Professional results table
        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan", border_style="cyan")
        table.add_column("Name", style="bold white", no_wrap=True, min_width=20)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Latest", style="cyan", justify="center", min_width=8)

        for server in servers:
            name = server.get("name", "Unknown")
            desc = server.get("description", "No description available")
            version = server.get("version", "—")

            # Intelligent description truncation
            if len(desc) > 80:
                # Find a good break point near the limit
                truncate_pos = 77
                if " " in desc[70:85]:
                    space_pos = desc.rfind(" ", 70, 85)
                    if space_pos > 70:
                        truncate_pos = space_pos
                desc = desc[:truncate_pos] + "..."

            table.add_row(name, desc, version)

        console.print(table)

        # Helpful next steps
        console.print(
            f"\n[muted]💡 Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        if total_shown == limit:
            console.print(
                f"[muted]   Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )

    except Exception as e:
        _rich_error(f"Error searching registry: {e}")
        sys.exit(1)


@mcp.command(help="Show detailed MCP server information")
@click.argument("server_name", required=True)
@click.pass_context
def show(ctx, server_name):
    """Show detailed information about an MCP server."""
    try:
        from apm_cli.registry.integration import RegistryIntegration

        registry = RegistryIntegration("https://api.mcp.github.com")

        console = _get_console()
        if not console:
            # Fallback for non-rich environments
            click.echo(f"Getting details for: {server_name}")
            try:
                server_info = registry.get_package_info(server_name)
                click.echo(f"Name: {server_info.get('name', 'Unknown')}")
                click.echo(
                    f"Description: {server_info.get('description', 'No description')}"
                )
                click.echo(
                    f"Repository: {server_info.get('repository', {}).get('url', 'Unknown')}"
                )
            except ValueError:
                click.echo(f"Server '{server_name}' not found")
                sys.exit(1)
            return

        # Professional loading indicator
        console.print(f"\n[bold cyan]MCP Server Details[/bold cyan]")
        console.print(f"[muted]Fetching: {server_name}[/muted]")

        try:
            server_info = registry.get_package_info(server_name)
        except ValueError:
            console.print(
                f"\n[red]✗[/red] MCP server '[bold]{server_name}[/bold]' not found in registry"
            )
            console.print(
                f"\n[muted]💡 Use [bold cyan]apm mcp search <query>[/bold cyan] to find available servers[/muted]"
            )
            sys.exit(1)

        # Main server information in professional table format
        name = server_info.get("name", "Unknown")
        description = server_info.get("description", "No description available")

        # Get key metadata
        version = "Unknown"
        if "version_detail" in server_info:
            version = server_info["version_detail"].get("version", "Unknown")
        elif "version" in server_info:
            version = server_info["version"]

        repo_url = "Unknown"
        if "repository" in server_info:
            repo_url = server_info["repository"].get("url", "Unknown")

        # Professional server info table with consistent styling
        from rich.table import Table

        # Main server information table
        info_table = Table(
            title=f"📦 MCP Server: {name}",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        info_table.add_column("Property", style="bold white", min_width=12)
        info_table.add_column("Value", style="white", min_width=40)

        info_table.add_row("Name", f"[bold white]{name}[/bold white]")
        info_table.add_row("Version", f"[cyan]{version}[/cyan]")
        info_table.add_row("Description", description)
        info_table.add_row("Repository", repo_url)
        if "id" in server_info:
            info_table.add_row("Registry ID", server_info["id"][:8] + "...")

        # Add deployment type information
        remotes = server_info.get("remotes", [])
        packages = server_info.get("packages", [])

        deployment_info = []
        if remotes:
            for remote in remotes:
                transport_type = remote.get("transport_type", "unknown")
                if transport_type == "sse":
                    deployment_info.append("🌐 Remote SSE Endpoint")
        if packages:
            deployment_info.append("📦 Local Package")

        if deployment_info:
            info_table.add_row("Deployment Type", " + ".join(deployment_info))

        console.print(info_table)

        # Show remote endpoints if available
        if remotes:
            remote_table = Table(
                title="🌐 Remote Endpoints",
                show_header=True,
                header_style="bold cyan",
                border_style="cyan",
            )
            remote_table.add_column("Type", style="yellow", width=10)
            remote_table.add_column("URL", style="white", min_width=40)
            remote_table.add_column("Features", style="cyan", min_width=20)

            for remote in remotes:
                transport_type = remote.get("transport_type", "unknown")
                url = remote.get("url", "unknown")

                # Describe features/limitations of remote endpoints
                features = "Hosted by provider"
                if "github" in name.lower():
                    features = "No toolset customization"

                remote_table.add_row(transport_type.upper(), url, features)

            console.print(remote_table)

        # Installation packages in consistent table format
        if packages:
            pkg_table = Table(
                title="📦 Local Packages",
                show_header=True,
                header_style="bold cyan",
                border_style="cyan",
            )
            pkg_table.add_column("Registry", style="yellow", width=10)
            pkg_table.add_column("Package", style="white", min_width=25)
            pkg_table.add_column("Runtime", style="cyan", width=8, justify="center")
            pkg_table.add_column("Features", style="green", min_width=20)

            for pkg in packages:
                registry_name = pkg.get("registry_name", "unknown")
                pkg_name = pkg.get("name", "unknown")
                runtime_hint = pkg.get("runtime_hint", "—")

                # Describe features of local packages
                features = "Full configuration control"
                if "github" in name.lower():
                    features = "Supports GITHUB_TOOLSETS"

                # Truncate long package names intelligently
                if len(pkg_name) > 25:
                    pkg_name = pkg_name[:22] + "..."

                pkg_table.add_row(registry_name, pkg_name, runtime_hint, features)

            console.print(pkg_table)

        # Installation instructions in structured table format
        install_name = server_info.get("name", server_name)
        install_table = Table(
            title="✨ Installation Guide",
            show_header=True,
            header_style="bold cyan",
            border_style="green",
        )
        install_table.add_column("Step", style="bold white", width=5)
        install_table.add_column("Action", style="white", min_width=30)
        install_table.add_column("Command/Config", style="cyan", min_width=25)

        install_table.add_row(
            "1",
            "Add to apm.yml dependencies",
            f"[yellow]mcp:[/yellow] [cyan]- {install_name}[/cyan]",
        )
        install_table.add_row(
            "2", "Install dependencies", "[bold cyan]apm install[/bold cyan]"
        )
        install_table.add_row(
            "3",
            "Direct install (coming soon)",
            f"[bold cyan]apm install {install_name}[/bold cyan]",
        )

        console.print(install_table)

    except Exception as e:
        _rich_error(f"Error getting server details: {e}")
        sys.exit(1)


@mcp.command(help="List all available MCP servers")
@click.option("--limit", default=20, help="Number of results to show")
@click.pass_context
def list(ctx, limit):
    """List all available MCP servers in the registry."""
    try:
        from apm_cli.registry.integration import RegistryIntegration

        registry = RegistryIntegration("https://api.mcp.github.com")

        console = _get_console()
        if not console:
            # Fallback for non-rich environments
            click.echo("Fetching available MCP servers...")
            servers = registry.list_available_packages()[:limit]
            if not servers:
                click.echo("No servers found")
                return
            for server in servers:
                click.echo(f"  {server.get('name', 'Unknown')}")
                click.echo(f"    {server.get('description', 'No description')[:80]}")
            return

        # Professional header
        console.print(f"\n[bold cyan]MCP Registry Catalog[/bold cyan]")
        console.print(f"[muted]Discovering available servers...[/muted]")

        servers = registry.list_available_packages()[:limit]

        if not servers:
            console.print(f"\n[yellow]⚠[/yellow] No MCP servers found in registry")
            console.print(
                f"\n[muted]💡 The registry might be temporarily unavailable[/muted]"
            )
            return

        # Results summary with pagination info
        total_shown = len(servers)
        console.print(
            f"\n[green]✓[/green] Showing [bold]{total_shown}[/bold] MCP servers"
        )
        if total_shown == limit:
            console.print(
                f"[muted]Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )

        # Professional catalog table
        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan", border_style="cyan")
        table.add_column("Name", style="bold white", no_wrap=True, min_width=25)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Latest", style="cyan", justify="center", min_width=8)

        for server in servers:
            name = server.get("name", "Unknown")
            desc = server.get("description", "No description available")
            version = server.get("version", "—")

            # Intelligent description truncation
            if len(desc) > 80:
                # Find a good break point near the limit
                truncate_pos = 77
                if " " in desc[70:85]:
                    space_pos = desc.rfind(" ", 70, 85)
                    if space_pos > 70:
                        truncate_pos = space_pos
                desc = desc[:truncate_pos] + "..."

            table.add_row(name, desc, version)

        console.print(table)

        # Helpful navigation
        console.print(
            f"\n[muted]💡 Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        console.print(
            f"[muted]   Use [bold cyan]apm mcp search <query>[/bold cyan] to find specific servers[/muted]"
        )

    except Exception as e:
        _rich_error(f"Error listing servers: {e}")
        sys.exit(1)


def _interactive_project_setup(default_name):
    """Interactive setup for new APM projects with auto-detection."""
    # Get auto-detected defaults
    auto_author = _auto_detect_author()
    auto_description = _auto_detect_description(default_name)

    try:
        # Lazy import rich pieces
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.prompt import Confirm, Prompt  # type: ignore

        console = _get_console() or Console()
        console.print("\n[info]Setting up your APM project...[/info]")
        console.print("[muted]Press ^C at any time to quit.[/muted]\n")

        name = Prompt.ask("Project name", default=default_name).strip()
        version = Prompt.ask("Version", default="1.0.0").strip()
        description = Prompt.ask("Description", default=auto_description).strip()
        author = Prompt.ask("Author", default=auto_author).strip()

        summary_content = f"""name: {name}
version: {version}
description: {description}
author: {author}"""
        console.print(
            Panel(summary_content, title="About to create", border_style="cyan")
        )

        if not Confirm.ask("\nIs this OK?", default=True):
            console.print("[info]Aborted.[/info]")
            sys.exit(0)

    except (ImportError, NameError):
        # Fallback to click prompts
        _rich_info("Setting up your APM project...")
        _rich_info("Press ^C at any time to quit.")

        name = click.prompt("Project name", default=default_name).strip()
        version = click.prompt("Version", default="1.0.0").strip()
        description = click.prompt("Description", default=auto_description).strip()
        author = click.prompt("Author", default=auto_author).strip()

        click.echo(f"\n{INFO}About to create:{RESET}")
        click.echo(f"  name: {name}")
        click.echo(f"  version: {version}")
        click.echo(f"  description: {description}")
        click.echo(f"  author: {author}")

        if not click.confirm("\nIs this OK?", default=True):
            _rich_info("Aborted.")
            sys.exit(0)

    return {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
    }

    _rich_info("Preserving existing configuration where possible")
    return config


def _auto_detect_author():
    """Auto-detect author from git config."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, timeout=5
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


def _create_minimal_apm_yml(config):
    """Create minimal apm.yml file with auto-detected metadata."""
    yaml = _lazy_yaml()

    # Create minimal apm.yml structure
    apm_yml_data = {
        "name": config["name"],
        "version": config["version"],
        "description": config["description"],
        "author": config["author"],
        "dependencies": {"apm": [], "mcp": []},
        "scripts": {},
    }

    # Write apm.yml
    with open("apm.yml", "w") as f:
        yaml.safe_dump(apm_yml_data, f, default_flow_style=False, sort_keys=False)


def main():
    """Main entry point for the CLI."""
    try:
        cli(obj={})
    except Exception as e:
        click.echo(f"{ERROR}Error: {e}{RESET}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
