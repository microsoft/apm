"""APM install command and dependency installation engine."""

import builtins
import sys
from pathlib import Path
from typing import List

import click

from ..constants import (
    APM_LOCK_FILENAME,
    APM_MODULES_DIR,
    APM_YML_FILENAME,
    GITHUB_DIR,
    CLAUDE_DIR,
    SKILL_MD_FILENAME,
    InstallMode,
)
from ..drift import (
    build_download_ref,
    detect_orphans,
    detect_ref_change,
    detect_stale_files,
)
from ..models.results import InstallResult
from ..core.command_logger import InstallLogger, _ValidationOutcome
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success
from ..utils.diagnostics import DiagnosticCollector
from ..utils.github_host import default_host, is_valid_fqdn
from ..utils.path_security import safe_rmtree
from ._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _rich_blank_line,
    _update_gitignore_for_apm_modules,
)

# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict

# AuthResolver has no optional deps (stdlib + internal utils only), so it must
# be imported unconditionally here -- NOT inside the APM_DEPS_AVAILABLE guard.
# If it were gated, a missing optional dep (e.g. GitPython) would cause a
# NameError in install() before the graceful APM_DEPS_AVAILABLE check fires.
from ..core.auth import AuthResolver

# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ..deps.apm_resolver import APMDependencyResolver
    from ..deps.github_downloader import GitHubPackageDownloader
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ..integration import AgentIntegrator, PromptIntegrator
    from ..integration.mcp_integrator import MCPIntegrator
    from ..models.apm_package import APMPackage, DependencyReference

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Root primitive detection helper
# ---------------------------------------------------------------------------

def _project_has_root_primitives(project_root) -> bool:
    """Return True when *project_root* has a .apm/ directory of its own.

    Used to decide whether ``apm install`` should enter the integration
    pipeline even when no external APM dependencies are declared (#714).
    The integrators themselves determine whether the directory contains
    anything actionable, so we only check for the directory's existence.
    """
    from pathlib import Path as _Path
    root = _Path(project_root)
    return (root / ".apm").is_dir()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_and_add_packages_to_apm_yml(packages, dry_run=False, dev=False, logger=None, manifest_path=None, auth_resolver=None, scope=None):
    """Validate packages exist and can be accessed, then add to apm.yml dependencies section.

    Implements normalize-on-write: any input form (HTTPS URL, SSH URL, FQDN, shorthand)
    is canonicalized before storage. Default host (github.com) is stripped;
    non-default hosts are preserved. Duplicates are detected by identity.

    Args:
        packages: Package specifiers to validate and add.
        dry_run: If True, only show what would be added.
        dev: If True, write to devDependencies instead of dependencies.
        logger: InstallLogger for structured output.
        manifest_path: Explicit path to apm.yml (defaults to cwd/apm.yml).
        auth_resolver: Shared auth resolver for caching credentials.
        scope: InstallScope controlling project vs user deployment.

    Returns:
        Tuple of (validated_packages list, _ValidationOutcome).
    """
    import subprocess
    import tempfile
    from pathlib import Path

    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)

    # Read current apm.yml
    try:
        from ..utils.yaml_io import load_yaml
        data = load_yaml(apm_yml_path) or {}
    except Exception as e:
        if logger:
            logger.error(f"Failed to read {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    # Ensure dependencies structure exists
    dep_section = "devDependencies" if dev else "dependencies"
    if dep_section not in data:
        data[dep_section] = {}
    if "apm" not in data[dep_section]:
        data[dep_section]["apm"] = []

    current_deps = data[dep_section]["apm"] or []
    validated_packages = []

    # Build identity set from existing deps for duplicate detection
    existing_identities = builtins.set()
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, str):
                ref = DependencyReference.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                ref = DependencyReference.parse_from_dict(dep_entry)
            else:
                continue
            existing_identities.add(ref.get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    # First, validate all packages
    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}

    if logger:
        logger.validation_start(len(packages))

    for package in packages:
        # --- Marketplace pre-parse intercept ---
        # If input has no slash and is not a local path, check if it is a
        # marketplace ref (NAME@MARKETPLACE).  If so, resolve it to a
        # canonical owner/repo[#ref] string before entering the standard
        # parse path.  Anything that doesn't match is rejected as an
        # invalid format.
        marketplace_provenance = None
        if "/" not in package and not DependencyReference.is_local_path(package):
            try:
                from ..marketplace.resolver import (
                    parse_marketplace_ref,
                    resolve_marketplace_plugin,
                )

                mkt_ref = parse_marketplace_ref(package)
            except ImportError:
                mkt_ref = None

            if mkt_ref is not None:
                plugin_name, marketplace_name = mkt_ref
                try:
                    if logger:
                        logger.verbose_detail(
                            f"    Resolving {plugin_name}@{marketplace_name} via marketplace..."
                        )
                    canonical_str, resolved_plugin = resolve_marketplace_plugin(
                        plugin_name,
                        marketplace_name,
                        auth_resolver=auth_resolver,
                    )
                    if logger:
                        logger.verbose_detail(
                            f"    Resolved to: {canonical_str}"
                        )
                    marketplace_provenance = {
                        "discovered_via": marketplace_name,
                        "marketplace_plugin_name": plugin_name,
                    }
                    package = canonical_str
                except Exception as mkt_err:
                    reason = str(mkt_err)
                    invalid_outcomes.append((package, reason))
                    if logger:
                        logger.validation_fail(package, reason)
                    continue
            else:
                # No slash, not a local path, and not a marketplace ref
                reason = "invalid format -- use 'owner/repo' or 'plugin-name@marketplace'"
                invalid_outcomes.append((package, reason))
                if logger:
                    logger.validation_fail(package, reason)
                continue

        # Canonicalize input
        try:
            dep_ref = DependencyReference.parse(package)
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
        except ValueError as e:
            reason = str(e)
            invalid_outcomes.append((package, reason))
            if logger:
                logger.validation_fail(package, reason)
            continue

        # Reject local packages at user scope -- relative paths resolve
        # against cwd during validation but against $HOME during copy,
        # causing silent failures.
        if dep_ref.is_local and scope is not None:
            from ..core.scope import InstallScope
            if scope is InstallScope.USER:
                reason = (
                    "local packages are not supported at user scope (--global). "
                    "Use a remote reference (owner/repo) instead"
                )
                invalid_outcomes.append((package, reason))
                if logger:
                    logger.validation_fail(package, reason)
                continue

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        verbose = bool(logger and logger.verbose)
        if _validate_package_exists(package, verbose=verbose, auth_resolver=auth_resolver):
            valid_outcomes.append((canonical, already_in_deps))
            if logger:
                logger.validation_pass(canonical, already_present=already_in_deps)

            if not already_in_deps:
                validated_packages.append(canonical)
                existing_identities.add(identity)  # prevent duplicates within batch
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = _local_path_failure_reason(dep_ref)
            if not reason:
                reason = "not accessible or doesn't exist"
                if not verbose:
                    reason += " -- run with --verbose for auth details"
            invalid_outcomes.append((package, reason))
            if logger:
                logger.validation_fail(package, reason)

    outcome = _ValidationOutcome(
        valid=valid_outcomes,
        invalid=invalid_outcomes,
        marketplace_provenance=_marketplace_provenance or None,
    )

    # Let the logger emit a summary and decide whether to continue
    if logger:
        should_continue = logger.validation_summary(outcome)
        if not should_continue:
            return [], outcome

    if not validated_packages:
        if dry_run:
            if logger:
                logger.progress("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them
        return [], outcome

    if dry_run:
        if logger:
            logger.progress(
                f"Dry run: Would add {len(validated_packages)} package(s) to apm.yml"
            )
            for pkg in validated_packages:
                logger.verbose_detail(f"  + {pkg}")
        return validated_packages, outcome

    # Add validated packages to dependencies (already canonical)
    dep_label = "devDependencies" if dev else "apm.yml"
    for package in validated_packages:
        current_deps.append(package)
        if logger:
            logger.verbose_detail(f"Added {package} to {dep_label}")

    # Update dependencies
    data[dep_section]["apm"] = current_deps

    # Write back to apm.yml
    try:
        from ..utils.yaml_io import dump_yaml
        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)")
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    return validated_packages, outcome


def _local_path_failure_reason(dep_ref):
    """Return a specific failure reason for local path deps, or None for remote."""
    if not (dep_ref.is_local and dep_ref.local_path):
        return None
    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():
        local = Path.cwd() / local
    local = local.resolve()
    if not local.exists():
        return "path does not exist"
    if not local.is_dir():
        return "path is not a directory"
    # Directory exists but has no package markers
    return "no apm.yml, SKILL.md, or plugin.json found"


def _local_path_no_markers_hint(local_dir, verbose_log=None):
    """Scan two levels for sub-packages and print a hint if any are found."""
    from apm_cli.utils.helpers import find_plugin_json

    markers = ("apm.yml", "SKILL.md")
    found = []
    for child in sorted(local_dir.iterdir()):
        if not child.is_dir():
            continue
        if any((child / m).exists() for m in markers) or find_plugin_json(child) is not None:
            found.append(child)
        # Also check one more level (e.g. skills/<name>/)
        for grandchild in sorted(child.iterdir()) if child.is_dir() else []:
            if not grandchild.is_dir():
                continue
            if any((grandchild / m).exists() for m in markers) or find_plugin_json(grandchild) is not None:
                found.append(grandchild)

    if not found:
        return

    _rich_info("  [i] Found installable package(s) inside this directory:")
    for p in found[:5]:
        _rich_echo(f"      apm install {p}", color="dim")
    if len(found) > 5:
        _rich_echo(f"      ... and {len(found) - 5} more", color="dim")


def _validate_package_exists(package, verbose=False, auth_resolver=None):
    """Validate that a package exists and is accessible on GitHub, Azure DevOps, or locally."""
    import os
    import subprocess
    import tempfile
    from apm_cli.core.auth import AuthResolver

    verbose_log = (lambda msg: _rich_echo(f"  {msg}", color="dim")) if verbose else None
    # Use provided resolver or create new one if not in a CLI session context
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    try:
        # Parse the package to check if it's a virtual package or ADO
        from apm_cli.models.apm_package import DependencyReference
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = DependencyReference.parse(package)

        # For local packages, validate directory exists and has valid package content
        if dep_ref.is_local and dep_ref.local_path:
            local = Path(dep_ref.local_path).expanduser()
            if not local.is_absolute():
                local = Path.cwd() / local
            local = local.resolve()
            if not local.is_dir():
                return False
            # Must contain apm.yml, SKILL.md, or plugin.json
            if (local / "apm.yml").exists() or (local / "SKILL.md").exists():
                return True
            from apm_cli.utils.helpers import find_plugin_json
            if find_plugin_json(local) is not None:
                return True
            # Directory exists but lacks package markers -- surface a hint
            _local_path_no_markers_hint(local, verbose_log)
            return False

        # For virtual packages, use the downloader's validation method
        if dep_ref.is_virtual:
            ctx = auth_resolver.resolve_for_dep(dep_ref)
            host = dep_ref.host or default_host()
            org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
            if verbose_log:
                verbose_log(f"Auth resolved: host={host}, org={org}, source={ctx.source}, type={ctx.token_type}")
            virtual_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
            result = virtual_downloader.validate_virtual_package_exists(dep_ref)
            if not result and verbose_log:
                try:
                    err_ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
                    for line in err_ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return result

        # For Azure DevOps or GitHub Enterprise (non-github.com hosts),
        # use the downloader which handles authentication properly
        if dep_ref.is_azure_devops() or (dep_ref.host and dep_ref.host != "github.com"):
            from apm_cli.utils.github_host import is_github_hostname, is_azure_devops_hostname

            # Determine host type before building the URL so we know whether to
            # embed a token.  Generic (non-GitHub, non-ADO) hosts are excluded
            # from APM-managed auth; they rely on git credential helpers via the
            # relaxed validate_env below.
            is_generic = not is_github_hostname(dep_ref.host) and not is_azure_devops_hostname(dep_ref.host)

            # For GHES / ADO: resolve per-dependency auth up front so the URL
            # carries an embedded token and avoids triggering OS credential
            # helper popups during git ls-remote validation.
            _url_token = None
            if not is_generic:
                _dep_ctx = auth_resolver.resolve_for_dep(dep_ref)
                _url_token = _dep_ctx.token

            ado_downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)
            # Set the host
            if dep_ref.host:
                ado_downloader.github_host = dep_ref.host

            # Build authenticated URL using the resolved per-dep token.
            package_url = ado_downloader._build_repo_url(
                dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref, token=_url_token
            )

            # For generic hosts (not GitHub, not ADO), relax the env so native
            # credential helpers (SSH keys, macOS Keychain, etc.) can work.
            # This mirrors _clone_with_fallback() which does the same relaxation.
            if is_generic:
                validate_env = {k: v for k, v in ado_downloader.git_env.items()
                                if k not in ('GIT_ASKPASS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
                validate_env['GIT_TERMINAL_PROMPT'] = '0'
            else:
                validate_env = {**os.environ, **ado_downloader.git_env}

            if verbose_log:
                verbose_log(f"Trying git ls-remote for {dep_ref.host}")

            # For generic hosts, try SSH first (no credentials needed when SSH
            # keys are configured) before falling back to HTTPS.
            urls_to_try = []
            if is_generic:
                ssh_url = ado_downloader._build_repo_url(
                    dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref
                )
                urls_to_try = [ssh_url, package_url]
            else:
                urls_to_try = [package_url]

            result = None
            for probe_url in urls_to_try:
                cmd = ["git", "ls-remote", "--heads", "--exit-code", probe_url]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    env=validate_env,
                )
                if result.returncode == 0:
                    break

            if verbose_log:
                if result.returncode == 0:
                    verbose_log(f"git ls-remote rc=0 for {package}")
                else:
                    # Sanitize stderr to avoid leaking tokens
                    stderr_snippet = (result.stderr or "").strip()[:200]
                    for env_var in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL"):
                        stderr_snippet = stderr_snippet.replace(
                            validate_env.get(env_var, ""), "***"
                        )
                    verbose_log(f"git ls-remote rc={result.returncode}: {stderr_snippet}")

            return result.returncode == 0

        # For GitHub.com, use AuthResolver with unauth-first fallback
        host = dep_ref.host or default_host()
        org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
        host_info = auth_resolver.classify_host(host)

        if verbose_log:
            ctx = auth_resolver.resolve(host, org=org)
            verbose_log(f"Auth resolved: host={host}, org={org}, source={ctx.source}, type={ctx.token_type}")

        def _check_repo(token, git_env):
            """Check repo accessibility via GitHub API (or git ls-remote for non-GitHub)."""
            import urllib.request
            import urllib.error

            api_base = host_info.api_base
            api_url = f"{api_base}/repos/{dep_ref.repo_url}"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "apm-cli",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(api_url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                if verbose_log:
                    verbose_log(f"API {api_url} -> {resp.status}")
                return True
            except urllib.error.HTTPError as e:
                if verbose_log:
                    verbose_log(f"API {api_url} -> {e.code} {e.reason}")
                if e.code == 404 and token:
                    # 404 with token could mean no access — raise to trigger fallback
                    raise RuntimeError(f"API returned {e.code}")
                raise RuntimeError(f"API returned {e.code}: {e.reason}")
            except Exception as e:
                if verbose_log:
                    verbose_log(f"API request failed: {e}")
                raise

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo,
                org=org,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception:
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False

    except Exception:
        # If parsing fails, assume it's a regular GitHub package
        host = default_host()
        org = package.split('/')[0] if '/' in package else None
        repo_path = package  # owner/repo format

        def _check_repo_fallback(token, git_env):
            import urllib.request
            import urllib.error

            host_info = auth_resolver.classify_host(host)
            api_url = f"{host_info.api_base}/repos/{repo_path}"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "apm-cli",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(api_url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                return True
            except urllib.error.HTTPError as e:
                if verbose_log:
                    verbose_log(f"API fallback -> {e.code} {e.reason}")
                raise RuntimeError(f"API returned {e.code}")
            except Exception as e:
                if verbose_log:
                    verbose_log(f"API fallback failed: {e}")
                raise

        try:
            return auth_resolver.try_with_fallback(
                host, _check_repo_fallback,
                org=org,
                unauth_first=True,
                verbose_callback=verbose_log,
            )
        except Exception:
            if verbose_log:
                try:
                    ctx = auth_resolver.build_error_context(host, f"accessing {package}", org=org)
                    for line in ctx.splitlines():
                        verbose_log(line)
                except Exception:
                    pass
            return False


# ---------------------------------------------------------------------------
# Install command
# ---------------------------------------------------------------------------


@click.command(
    help="Install APM and MCP dependencies (auto-creates apm.yml when installing packages)"
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
@click.option("--force", is_flag=True, help="Overwrite locally-authored files on collision and deploy despite critical security findings")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed installation information")
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
@click.option(
    "--dev",
    is_flag=True,
    default=False,
    help="Install as development dependency (devDependencies)",
)
@click.option(
    "--target",
    "-t",
    "target",
    type=click.Choice(
        ["copilot", "claude", "cursor", "opencode", "codex", "vscode", "agents", "all"],
        case_sensitive=False,
    ),
    default=None,
    help="Force deployment to a specific target (overrides auto-detection)",
)
@click.option(
    "--global", "-g", "global_",
    is_flag=True,
    default=False,
    help="Install to user scope (~/.apm/) instead of the current project",
)
@click.pass_context
def install(ctx, packages, runtime, exclude, only, update, dry_run, force, verbose, trust_transitive_mcp, parallel_downloads, dev, target, global_):
    """Install APM and MCP dependencies from apm.yml (like npm install).

    This command automatically detects AI runtimes from your apm.yml scripts and installs
    MCP servers for all detected and available runtimes. It also installs APM package
    dependencies from GitHub repositories.

    The --only flag filters by dependency type (apm or mcp). Internally converted
    to an InstallMode enum for type-safe dispatch.

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1                    # Add package to apm.yml and install
        apm install org/pkg1 org/pkg2           # Add multiple packages and install
        apm install --exclude codex             # Install for all except Codex CLI
        apm install --only=apm                  # Install only APM dependencies
        apm install --only=mcp                  # Install only MCP dependencies
        apm install --update                    # Update dependencies to latest Git refs
        apm install --dry-run                   # Show what would be installed
        apm install -g org/pkg1                 # Install to user scope (~/.apm/)
    """
    try:
        # Create structured logger for install output early so exception
        # handlers can always reference it (avoids UnboundLocalError if
        # scope initialisation below throws).
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)

        # Resolve scope
        from ..core.scope import InstallScope, get_apm_dir, get_manifest_path, get_modules_dir, ensure_user_dirs, warn_unsupported_user_scope
        scope = InstallScope.USER if global_ else InstallScope.PROJECT

        if scope is InstallScope.USER:
            ensure_user_dirs()
            logger.progress("Installing to user scope (~/.apm/)")
            _scope_warn = warn_unsupported_user_scope()
            if _scope_warn:
                logger.warning(_scope_warn)

        # Scope-aware paths
        manifest_path = get_manifest_path(scope)
        apm_dir = get_apm_dir(scope)
        # Display name for messages (short for project scope, full for user scope)
        manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME

        # Project root for integration (used by both dep and local integration)
        from ..core.scope import get_deploy_root
        project_root = get_deploy_root(scope)

        # Create shared auth resolver for all downloads in this CLI invocation
        # to ensure credentials are cached and reused (prevents duplicate auth popups)
        auth_resolver = AuthResolver()

        # Check if apm.yml exists
        apm_yml_exists = manifest_path.exists()

        # Auto-bootstrap: create minimal apm.yml when packages specified but no apm.yml
        if not apm_yml_exists and packages:
            # Get current directory name as project name
            project_name = Path.cwd().name if scope is InstallScope.PROJECT else Path.home().name
            config = _get_default_config(project_name)
            _create_minimal_apm_yml(config, target_path=manifest_path)
            logger.success(f"Created {manifest_display}")

        # Error when NO apm.yml AND NO packages
        if not apm_yml_exists and not packages:
            logger.error(f"No {manifest_display} found")
            if scope is InstallScope.USER:
                logger.progress("Run 'apm install -g <org/repo>' to auto-create + install")
            else:
                logger.progress("Run 'apm init' to create one, or:")
                logger.progress("  apm install <org/repo> to auto-create + install")
            sys.exit(1)

        # If packages are specified, validate and add them to apm.yml first
        if packages:
            validated_packages, outcome = _validate_and_add_packages_to_apm_yml(
                packages, dry_run, dev=dev, logger=logger,
                manifest_path=manifest_path, auth_resolver=auth_resolver,
                scope=scope,
            )
            # Short-circuit: all packages failed validation — nothing to install
            if outcome.all_failed:
                return
            # Note: Empty validated_packages is OK if packages are already in apm.yml
            # We'll proceed with installation from apm.yml to ensure everything is synced

        logger.resolution_start(
            to_install_count=len(validated_packages) if packages else 0,
            lockfile_count=0,  # Refined later inside _install_apm_dependencies
        )

        # Parse apm.yml to get both APM and MCP dependencies
        try:
            apm_package = APMPackage.from_apm_yml(manifest_path)
        except Exception as e:
            logger.error(f"Failed to parse {manifest_display}: {e}")
            sys.exit(1)

        logger.verbose_detail(
            f"Parsed {APM_YML_FILENAME}: {len(apm_package.get_apm_dependencies())} APM deps, "
            f"{len(apm_package.get_mcp_dependencies())} MCP deps"
            + (f", {len(apm_package.get_dev_apm_dependencies())} dev deps"
               if apm_package.get_dev_apm_dependencies() else "")
        )

        # Get APM and MCP dependencies
        apm_deps = apm_package.get_apm_dependencies()
        dev_apm_deps = apm_package.get_dev_apm_dependencies()
        has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)
        mcp_deps = apm_package.get_mcp_dependencies()

        # Convert --only string to InstallMode enum
        if only is None:
            install_mode = InstallMode.ALL
        else:
            install_mode = InstallMode(only)

        # Determine what to install based on install mode
        should_install_apm = install_mode != InstallMode.MCP
        should_install_mcp = install_mode != InstallMode.APM
        # MCP servers are workspace-scoped (.vscode/mcp.json); skip at user scope
        if scope is InstallScope.USER:
            should_install_mcp = False
            if logger:
                logger.verbose_detail(
                    "MCP servers skipped at user scope (workspace-scoped concept)"
                )

        # Show what will be installed if dry run
        if dry_run:
            logger.progress("Dry run mode - showing what would be installed:")

            if should_install_apm and apm_deps:
                logger.progress(f"APM dependencies ({len(apm_deps)}):")
                for dep in apm_deps:
                    action = "update" if update else "install"
                    logger.progress(
                        f"  - {dep.repo_url}#{dep.reference or 'main'} -> {action}"
                    )

            if should_install_mcp and mcp_deps:
                logger.progress(f"MCP dependencies ({len(mcp_deps)}):")
                for dep in mcp_deps:
                    logger.progress(f"  - {dep}")

            if not apm_deps and not dev_apm_deps and not mcp_deps:
                logger.progress("No dependencies found in apm.yml")

            logger.success("Dry run complete - no changes made")
            return

        # Install APM dependencies first (if requested)
        apm_count = 0
        prompt_count = 0
        agent_count = 0

        # Migrate legacy apm.lock → apm.lock.yaml if needed (one-time, transparent)
        migrate_lockfile_if_needed(apm_dir)

        # Capture old MCP servers and configs from lockfile BEFORE
        # _install_apm_dependencies regenerates it (which drops the fields).
        # We always read this — even when --only=apm — so we can restore the
        # field after the lockfile is regenerated by the APM install step.
        old_mcp_servers: builtins.set = builtins.set()
        old_mcp_configs: builtins.dict = {}
        old_local_deployed: builtins.list = []
        _lock_path = get_lockfile_path(apm_dir)
        _existing_lock = LockFile.read(_lock_path)
        if _existing_lock:
            old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
            old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)
            old_local_deployed = builtins.list(_existing_lock.local_deployed_files)

        # Also enter the APM install path when the project root has local .apm/
        # primitives, even if there are no external APM dependencies (#714).
        from apm_cli.core.scope import get_deploy_root as _get_deploy_root
        _cli_project_root = _get_deploy_root(scope)

        apm_diagnostics = None
        if should_install_apm and (has_any_apm_deps or _project_has_root_primitives(_cli_project_root)):
            if not APM_DEPS_AVAILABLE:
                logger.error("APM dependency system not available")
                logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
                sys.exit(1)

            try:
                # If specific packages were requested, only install those
                # Otherwise install all from apm.yml.
                # Use validated_packages (canonical strings) instead of
                # raw packages (which may contain marketplace refs like
                # NAME@MARKETPLACE that don't match resolved dep identities).
                only_pkgs = builtins.list(validated_packages) if packages else None
                install_result = _install_apm_dependencies(
                    apm_package, update, verbose, only_pkgs, force=force,
                    parallel_downloads=parallel_downloads,
                    logger=logger,
                    scope=scope,
                    auth_resolver=auth_resolver,
                    target=target,
                    marketplace_provenance=(
                        outcome.marketplace_provenance if packages and outcome else None
                    ),
                )
                apm_count = install_result.installed_count
                prompt_count = install_result.prompts_integrated
                agent_count = install_result.agents_integrated
                apm_diagnostics = install_result.diagnostics
            except Exception as e:
                logger.error(f"Failed to install APM dependencies: {e}")
                if not verbose:
                    logger.progress("Run with --verbose for detailed diagnostics")
                sys.exit(1)
        elif should_install_apm and not has_any_apm_deps:
            logger.verbose_detail("No APM dependencies found in apm.yml")

        # When --update is used, package files on disk may have changed.
        # Clear the parse cache so transitive MCP collection reads fresh data.
        if update:
            from apm_cli.models.apm_package import clear_apm_yml_cache
            clear_apm_yml_cache()

        # Collect transitive MCP dependencies from resolved APM packages
        apm_modules_path = get_modules_dir(scope)
        if should_install_mcp and apm_modules_path.exists():
            lock_path = get_lockfile_path(apm_dir)
            transitive_mcp = MCPIntegrator.collect_transitive(
                apm_modules_path, lock_path, trust_transitive_mcp,
                diagnostics=apm_diagnostics,
            )
            if transitive_mcp:
                logger.verbose_detail(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
                mcp_deps = MCPIntegrator.deduplicate(mcp_deps + transitive_mcp)

        # Continue with MCP installation (existing logic)
        mcp_count = 0
        new_mcp_servers: builtins.set = builtins.set()
        if should_install_mcp and mcp_deps:
            mcp_count = MCPIntegrator.install(
                mcp_deps, runtime, exclude, verbose,
                stored_mcp_configs=old_mcp_configs,
                diagnostics=apm_diagnostics,
            )
            new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps)
            new_mcp_configs = MCPIntegrator.get_server_configs(mcp_deps)

            # Remove stale MCP servers that are no longer needed
            stale_servers = old_mcp_servers - new_mcp_servers
            if stale_servers:
                MCPIntegrator.remove_stale(stale_servers, runtime, exclude)

            # Persist the new MCP server set and configs in the lockfile
            MCPIntegrator.update_lockfile(new_mcp_servers, mcp_configs=new_mcp_configs)
        elif should_install_mcp and not mcp_deps:
            # No MCP deps at all — remove any old APM-managed servers
            if old_mcp_servers:
                MCPIntegrator.remove_stale(old_mcp_servers, runtime, exclude)
                MCPIntegrator.update_lockfile(builtins.set(), mcp_configs={})
            logger.verbose_detail("No MCP dependencies found in apm.yml")
        elif not should_install_mcp and old_mcp_servers:
            # --only=apm: APM install regenerated the lockfile and dropped
            # mcp_servers.  Restore the previous set so it is not lost.
            MCPIntegrator.update_lockfile(old_mcp_servers, mcp_configs=old_mcp_configs)

        # --- Local .apm/ content integration ---
        # Deploy primitives from the project's own .apm/ folder to target
        # directories, just like dependency primitives.  Runs AFTER deps so
        # local content wins on collision.
        if (
            should_install_apm
            and scope is InstallScope.PROJECT
            and not dry_run
            and (_has_local_apm_content(project_root) or old_local_deployed)
        ):
            try:
                from apm_cli.integration.targets import resolve_targets as _local_resolve
                from apm_cli.integration.skill_integrator import SkillIntegrator
                from apm_cli.integration.command_integrator import CommandIntegrator
                from apm_cli.integration.hook_integrator import HookIntegrator
                from apm_cli.integration.instruction_integrator import InstructionIntegrator
                from apm_cli.integration.base_integrator import BaseIntegrator
                from apm_cli.deps.lockfile import LockFile as _LocalLF, get_lockfile_path as _local_lf_path
                from apm_cli.integration import AgentIntegrator as _AgentInt, PromptIntegrator as _PromptInt

                # Resolve targets (same precedence as _install_apm_dependencies)
                _local_config_target = apm_package.target
                _local_explicit = target or _local_config_target or None
                _local_targets = _local_resolve(
                    project_root, user_scope=False, explicit_target=_local_explicit,
                )

                if _local_targets:
                    # Build managed_files: dep-deployed files + previous local
                    # deployed files.  This ensures local content wins
                    # collisions with deps and previous local files are not
                    # treated as user-authored content.
                    _local_managed = builtins.set()
                    _local_lock_path = _local_lf_path(apm_dir)
                    _local_lock = _LocalLF.read(_local_lock_path)
                    if _local_lock:
                        for dep in _local_lock.dependencies.values():
                            _local_managed.update(dep.deployed_files)
                    # Include previous local deployed files so re-deploys
                    # overwrite rather than skip.
                    _local_managed.update(old_local_deployed)
                    _local_managed = BaseIntegrator.normalize_managed_files(_local_managed)

                    # Create integrators
                    _local_diagnostics = apm_diagnostics or DiagnosticCollector(verbose=verbose)
                    _errors_before_local = _local_diagnostics.error_count
                    _local_prompt_int = _PromptInt()
                    _local_agent_int = _AgentInt()
                    _local_skill_int = SkillIntegrator()
                    _local_instr_int = InstructionIntegrator()
                    _local_cmd_int = CommandIntegrator()
                    _local_hook_int = HookIntegrator()

                    logger.verbose_detail("Integrating local .apm/ content...")

                    local_int_result = _integrate_local_content(
                        project_root,
                        targets=_local_targets,
                        prompt_integrator=_local_prompt_int,
                        agent_integrator=_local_agent_int,
                        skill_integrator=_local_skill_int,
                        instruction_integrator=_local_instr_int,
                        command_integrator=_local_cmd_int,
                        hook_integrator=_local_hook_int,
                        force=force,
                        managed_files=_local_managed,
                        diagnostics=_local_diagnostics,
                        logger=logger,
                        scope=scope,
                    )

                    # Track what local integration deployed
                    _local_deployed = local_int_result.get("deployed_files", [])
                    _local_total = sum(
                        local_int_result.get(k, 0)
                        for k in ("prompts", "agents", "skills", "sub_skills",
                                  "instructions", "commands", "hooks")
                    )

                    if _local_total > 0:
                        logger.verbose_detail(
                            f"Deployed {_local_total} local primitive(s) from .apm/"
                        )

                    # Stale cleanup: remove files deployed by previous local
                    # integration that are no longer produced.  Only run when
                    # integration completed without errors to avoid deleting
                    # files that failed to re-deploy.
                    _errors_before = (
                        _local_diagnostics.error_count
                        if _local_diagnostics else 0
                    )
                    _local_had_errors = (
                        _local_diagnostics is not None
                        and _local_diagnostics.error_count > _errors_before_local
                    )
                    if old_local_deployed and not _local_had_errors:
                        _prev_local = builtins.set(old_local_deployed)
                        _curr_local = builtins.set(_local_deployed)
                        _stale = _prev_local - _curr_local
                        if _stale:
                            import shutil as _local_shutil
                            _stale_removed = 0
                            _stale_deleted_paths = []
                            _stale_failed = []
                            for _stale_path in sorted(_stale):
                                if BaseIntegrator.validate_deploy_path(
                                    _stale_path, project_root, targets=_local_targets
                                ):
                                    _stale_target = project_root / _stale_path
                                    if _stale_target.exists():
                                        try:
                                            if _stale_target.is_dir():
                                                _local_shutil.rmtree(_stale_target)
                                            else:
                                                _stale_target.unlink()
                                            _stale_deleted_paths.append(_stale_target)
                                            _stale_removed += 1
                                        except Exception:
                                            _stale_failed.append(_stale_path)
                                            logger.verbose_detail(
                                                f"Failed to remove stale file: {_stale_path}"
                                            )
                            # Keep failed paths in local_deployed so they
                            # are retried on the next install.
                            _local_deployed.extend(_stale_failed)
                            if _stale_deleted_paths:
                                BaseIntegrator.cleanup_empty_parents(
                                    _stale_deleted_paths, project_root
                                )
                            if _stale_removed > 0:
                                logger.verbose_detail(
                                    f"Removed {_stale_removed} stale local file(s)"
                                )

                    # Persist local_deployed_files in the lockfile
                    _persist_lock = _LocalLF.read(_local_lock_path) or _LocalLF()
                    _persist_lock.local_deployed_files = sorted(_local_deployed)
                    # Only write if changed
                    _existing_for_cmp = _LocalLF.read(_local_lock_path)
                    if not _existing_for_cmp or not _persist_lock.is_semantically_equivalent(_existing_for_cmp):
                        _persist_lock.save(_local_lock_path)

                    # Ensure diagnostics flow into the final summary
                    if apm_diagnostics is None:
                        apm_diagnostics = _local_diagnostics

            except Exception as e:
                logger.verbose_detail(f"Local .apm/ integration failed: {e}")
                if apm_diagnostics:
                    apm_diagnostics.error(f"Local .apm/ integration failed: {e}")

        # Show diagnostics and final install summary
        if apm_diagnostics and apm_diagnostics.has_diagnostics:
            apm_diagnostics.render_summary()
        else:
            _rich_blank_line()

        error_count = 0
        if apm_diagnostics:
            try:
                error_count = int(apm_diagnostics.error_count)
            except (TypeError, ValueError):
                error_count = 0
        logger.install_summary(apm_count=apm_count, mcp_count=mcp_count, errors=error_count)

        # Hard-fail when critical security findings blocked any package.
        # Consistent with apm unpack which also hard-fails on critical.
        # Use --force to override.
        if not force and apm_diagnostics and apm_diagnostics.has_critical_security:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error installing dependencies: {e}")
        if not verbose:
            logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Install engine
# ---------------------------------------------------------------------------


def _pre_deploy_security_scan(
    install_path: Path,
    diagnostics: DiagnosticCollector,
    package_name: str = "",
    force: bool = False,
    logger=None,
) -> bool:
    """Scan package source files for hidden characters BEFORE deployment.

    Delegates to :class:`SecurityGate` for the scan->classify->decide pipeline.
    Inline CLI feedback (error/info lines) is kept here because it is
    install-specific formatting.

    Returns:
        True if deployment should proceed, False to block.
    """
    from ..security.gate import BLOCK_POLICY, SecurityGate

    verdict = SecurityGate.scan_files(
        install_path, policy=BLOCK_POLICY, force=force
    )
    if not verdict.has_findings:
        return True

    # Record into diagnostics (consistent messaging via gate)
    SecurityGate.report(verdict, diagnostics, package=package_name, force=force)

    if verdict.should_block:
        if logger:
            logger.error(
                f"  Blocked: {package_name or 'package'} contains "
                f"critical hidden character(s)"
            )
            logger.progress(f"  └─ Inspect source: {install_path}")
            logger.progress("  └─ Use --force to deploy anyway")
        return False

    return True


def _integrate_package_primitives(
    package_info,
    project_root,
    *,
    targets,
    prompt_integrator,
    agent_integrator,
    skill_integrator,
    instruction_integrator,
    command_integrator,
    hook_integrator,
    force,
    managed_files,
    diagnostics,
    package_name="",
    logger=None,
    scope=None,
):
    """Run the full integration pipeline for a single package.

    Iterates over *targets* (``TargetProfile`` list) and dispatches each
    primitive to the appropriate integrator via the target-driven API.
    Skills are handled separately because ``SkillIntegrator`` already
    routes across all targets internally.

    When *scope* is ``InstallScope.USER``, targets and primitives that
    do not support user-scope deployment are silently skipped.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }

    deployed = result["deployed_files"]

    if not targets:
        return result

    def _log_integration(msg):
        if logger:
            logger.tree_item(msg)

    # Map integrator kwargs to dispatch table keys
    _INTEGRATOR_KWARGS = {
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    # --- per-target dispatch loop ---
    for _target in targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.multi_target:
                continue  # skills handled below

            _integrator = _INTEGRATOR_KWARGS[_prim_name]
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target, package_info, project_root,
                force=force, managed_files=managed_files,
                diagnostics=diagnostics,
            )

            if _int_result.files_integrated > 0:
                result[_entry.counter_key] += _int_result.files_integrated
                _effective_root = _mapping.deploy_root or _target.root_dir
                _deploy_dir = f"{_effective_root}/{_mapping.subdir}/" if _mapping.subdir else f"{_effective_root}/"
                # Determine display label
                if _prim_name == "instructions" and _mapping.format_id in ("cursor_rules", "claude_rules"):
                    _label = "rule(s)"
                elif _prim_name == "instructions":
                    _label = "instruction(s)"
                elif _prim_name == "hooks":
                    if _target.name == "claude":
                        _deploy_dir = ".claude/settings.json"
                    elif _target.name == "cursor":
                        _deploy_dir = ".cursor/hooks.json"
                    elif _target.name == "codex":
                        _deploy_dir = ".codex/hooks.json"
                    _label = "hook(s)"
                else:
                    _label = _prim_name
                _log_integration(
                    f"  |-- {_int_result.files_integrated} {_label} integrated -> {_deploy_dir}"
                )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(tp.relative_to(project_root).as_posix())

    # --- skills (multi-target, handled by SkillIntegrator internally) ---
    skill_result = skill_integrator.integrate_package_skill(
        package_info, project_root,
        diagnostics=diagnostics, managed_files=managed_files, force=force,
        targets=targets,
    )
    _skill_target_dirs: set[str] = builtins.set()
    for tp in skill_result.target_paths:
        rel = tp.relative_to(project_root)
        if rel.parts:
            _skill_target_dirs.add(rel.parts[0])
    _skill_targets = sorted(_skill_target_dirs)
    _skill_target_str = ", ".join(f"{d}/skills/" for d in _skill_targets) or "skills/"
    if skill_result.skill_created:
        result["skills"] += 1
        _log_integration(f"  |-- Skill integrated -> {_skill_target_str}")
    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        _log_integration(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_target_str}")
    for tp in skill_result.target_paths:
        deployed.append(tp.relative_to(project_root).as_posix())

    return result


def _has_local_apm_content(project_root):
    """Check if the project has local .apm/ content worth integrating.

    Returns True if .apm/ exists and contains at least one primitive file
    in a recognized subdirectory (skills, instructions, agents/chatmodes,
    prompts, hooks, commands).
    """
    apm_dir = project_root / ".apm"
    if not apm_dir.is_dir():
        return False
    _PRIMITIVE_DIRS = ("skills", "instructions", "chatmodes", "agents", "prompts", "hooks", "commands")
    for subdir_name in _PRIMITIVE_DIRS:
        subdir = apm_dir / subdir_name
        if subdir.is_dir() and any(p.is_file() for p in subdir.rglob("*")):
            return True
    return False


def _integrate_local_content(
    project_root,
    *,
    targets,
    prompt_integrator,
    agent_integrator,
    skill_integrator,
    instruction_integrator,
    command_integrator,
    hook_integrator,
    force,
    managed_files,
    diagnostics,
    logger=None,
    scope=None,
):
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Returns a dict with integration counters and deployed file paths,
    same shape as ``_integrate_package_primitives()``.
    """
    from ..models.apm_package import APMPackage, PackageInfo, PackageType

    # Build a lightweight synthetic PackageInfo rooted at the project.
    # package_type=APM_PACKAGE prevents SkillIntegrator from treating
    # a root SKILL.md as a native skill to deploy.
    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=project_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=project_root,
        package_type=PackageType.APM_PACKAGE,
    )

    return _integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        prompt_integrator=prompt_integrator,
        agent_integrator=agent_integrator,
        skill_integrator=skill_integrator,
        instruction_integrator=instruction_integrator,
        command_integrator=command_integrator,
        hook_integrator=hook_integrator,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
    )


def _copy_local_package(dep_ref, install_path, project_root):
    """Copy a local package to apm_modules/.

    Args:
        dep_ref: DependencyReference with is_local=True
        install_path: Target path under apm_modules/
        project_root: Project root for resolving relative paths

    Returns:
        install_path on success, None on failure
    """
    import shutil

    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():
        local = (project_root / local).resolve()
    else:
        local = local.resolve()

    if not local.is_dir():
        _rich_error(f"Local package path does not exist: {dep_ref.local_path}")
        return None
    from apm_cli.utils.helpers import find_plugin_json
    if (
        not (local / "apm.yml").exists()
        and not (local / "SKILL.md").exists()
        and find_plugin_json(local) is None
    ):
        _rich_error(
            f"Local package is not a valid APM package (no apm.yml, SKILL.md, or plugin.json): {dep_ref.local_path}"
        )
        return None

    # Ensure parent exists and clean target (always re-copy for local deps)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    if install_path.exists():
        # install_path is already validated by get_install_path() (Layer 2),
        # but use safe_rmtree for defense-in-depth.
        apm_modules_dir = install_path.parent.parent  # _local/<name> → apm_modules
        safe_rmtree(install_path, apm_modules_dir)

    shutil.copytree(local, install_path, dirs_exist_ok=False, symlinks=True)
    return install_path


def _install_apm_dependencies(
    apm_package: "APMPackage",
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: "builtins.list" = None,
    force: bool = False,
    parallel_downloads: int = 4,
    logger: "InstallLogger" = None,
    scope=None,
    auth_resolver: "AuthResolver" = None,
    target: str = None,
    marketplace_provenance: dict = None,
):
    """Install APM package dependencies.

    Args:
        apm_package: Parsed APM package with dependencies
        update_refs: Whether to update existing packages to latest refs
        verbose: Show detailed installation information
        only_packages: If provided, only install these specific packages (not all from apm.yml)
        force: Whether to overwrite locally-authored files on collision
        parallel_downloads: Max concurrent downloads (0 disables parallelism)
        logger: InstallLogger for structured output
        scope: InstallScope controlling project vs user deployment
        auth_resolver: Shared auth resolver for caching credentials
        target: Explicit target override from --target CLI flag
    """
    if not APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    from apm_cli.core.scope import InstallScope, get_deploy_root, get_apm_dir, get_modules_dir
    if scope is None:
        scope = InstallScope.PROJECT

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    all_apm_deps = apm_deps + dev_apm_deps

    project_root = get_deploy_root(scope)
    apm_dir = get_apm_dir(scope)

    # Check whether the project root itself has local .apm/ primitives (#714).
    # Users should be able to keep root-level .apm/ rules alongside their apm.yml
    # without creating a dummy sub-package stub.
    _root_has_local_primitives = _project_has_root_primitives(project_root)

    if not all_apm_deps and not _root_has_local_primitives:
        return InstallResult()

    # T5: Check for existing lockfile - use locked versions for reproducible installs
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    lockfile_path = get_lockfile_path(apm_dir)
    existing_lockfile = None
    lockfile_count = 0
    if lockfile_path.exists():
        existing_lockfile = LockFile.read(lockfile_path)
        if existing_lockfile and existing_lockfile.dependencies:
            lockfile_count = len(existing_lockfile.dependencies)
            if logger:
                if update_refs:
                    logger.verbose_detail(f"Loaded apm.lock.yaml for SHA comparison ({lockfile_count} dependencies)")
                else:
                    logger.verbose_detail(f"Using apm.lock.yaml ({lockfile_count} locked dependencies)")
                if logger.verbose:
                    for locked_dep in existing_lockfile.get_all_dependencies():
                        _sha = locked_dep.resolved_commit[:8] if locked_dep.resolved_commit else ""
                        _ref = locked_dep.resolved_ref if hasattr(locked_dep, 'resolved_ref') and locked_dep.resolved_ref else ""
                        logger.lockfile_entry(locked_dep.get_unique_key(), ref=_ref, sha=_sha)

    apm_modules_dir = get_modules_dir(scope)
    apm_modules_dir.mkdir(parents=True, exist_ok=True)

    # Use provided resolver or create new one if not in a CLI session context
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    # Create downloader early so it can be used for transitive dependency resolution
    downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)

    # Track direct dependency keys so the download callback can distinguish them from transitive
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in all_apm_deps)

    # Track paths already downloaded by the resolver callback to avoid re-downloading
    # Maps dep_key -> resolved_commit (SHA or None) so the cached path can use it
    callback_downloaded = {}

    # Collect transitive dep failures during resolution — they'll be routed to
    # diagnostics after the DiagnosticCollector is created (later in the flow).
    transitive_failures: list[tuple[str, str]] = []  # (dep_display, message)

    # Track dep keys that failed in download_callback so the main install loop
    # skips them instead of re-trying and producing a duplicate error entry.
    callback_failures: builtins.set = builtins.set()

    # Create a download callback for transitive dependency resolution
    # This allows the resolver to fetch packages on-demand during tree building
    def download_callback(dep_ref, modules_dir, parent_chain=""):
        """Download a package during dependency resolution.

        Args:
            dep_ref: The dependency to download.
            modules_dir: Target apm_modules directory.
            parent_chain: Human-readable breadcrumb (e.g. "root > mid")
                showing which dependency path led to this transitive dep.
        """
        install_path = dep_ref.get_install_path(modules_dir)
        if install_path.exists():
            return install_path
        try:
            # Handle local packages: copy instead of git clone
            if dep_ref.is_local and dep_ref.local_path:
                if scope is InstallScope.USER:
                    # Cannot resolve local paths at user scope
                    callback_failures[dep_ref.get_unique_key()] = (
                        f"local package '{dep_ref.local_path}' skipped at user scope"
                    )
                    return None
                result_path = _copy_local_package(dep_ref, install_path, project_root)
                if result_path:
                    callback_downloaded[dep_ref.get_unique_key()] = None
                    return result_path
                return None

            # T5: Use locked commit if available (reproducible installs)
            locked_ref = None
            if existing_lockfile:
                locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                    locked_ref = locked_dep.resolved_commit

            # Build a DependencyReference with the right ref to avoid lossy
            # str() -> parse() round-trips (#382).
            from dataclasses import replace as _dc_replace
            if locked_ref and not update_refs:
                download_dep = _dc_replace(dep_ref, reference=locked_ref)
            else:
                download_dep = dep_ref

            # Silent download - no progress display for transitive deps
            result = downloader.download_package(download_dep, install_path)
            # Capture resolved commit SHA for lockfile
            resolved_sha = None
            if result and hasattr(result, 'resolved_reference') and result.resolved_reference:
                resolved_sha = result.resolved_reference.resolved_commit
            callback_downloaded[dep_ref.get_unique_key()] = resolved_sha
            return install_path
        except Exception as e:
            dep_display = dep_ref.get_display_name()
            dep_key = dep_ref.get_unique_key()
            is_direct = dep_key in direct_dep_keys

            # Distinguish direct vs transitive failure messages so users
            # don't see a misleading "transitive dep" label for top-level deps.
            if is_direct:
                fail_msg = (
                    f"Failed to download dependency "
                    f"{dep_ref.repo_url}: {e}"
                )
            else:
                chain_hint = f" (via {parent_chain})" if parent_chain else ""
                fail_msg = (
                    f"Failed to resolve transitive dep "
                    f"{dep_ref.repo_url}{chain_hint}: {e}"
                )

            # Verbose: inline detail
            if logger:
                logger.verbose_detail(f"  {fail_msg}")
            elif verbose:
                _rich_error(f"  └─ {fail_msg}")
            # Collect for deferred diagnostics summary (always, even non-verbose)
            callback_failures.add(dep_key)
            transitive_failures.append((dep_display, fail_msg))
            return None

    # Resolve dependencies with transitive download support
    resolver = APMDependencyResolver(
        apm_modules_dir=apm_modules_dir,
        download_callback=download_callback
    )

    try:
        dependency_graph = resolver.resolve_dependencies(apm_dir)

        # Verbose: show resolved tree summary
        if logger:
            tree = dependency_graph.dependency_tree
            direct_count = len(tree.get_nodes_at_depth(1))
            transitive_count = len(tree.nodes) - direct_count
            if transitive_count > 0:
                logger.verbose_detail(
                    f"Resolved dependency tree: {direct_count} direct + "
                    f"{transitive_count} transitive deps (max depth {tree.max_depth})"
                )
                for node in tree.nodes.values():
                    if node.depth > 1:
                        logger.verbose_detail(
                            f"    {node.get_ancestor_chain()}"
                        )
            else:
                logger.verbose_detail(f"Resolved {direct_count} direct dependencies (no transitive)")

        # Check for circular dependencies
        if dependency_graph.circular_dependencies:
            if logger:
                logger.error("Circular dependencies detected:")
            for circular in dependency_graph.circular_dependencies:
                cycle_path = " -> ".join(circular.cycle_path)
                if logger:
                    logger.error(f"  {cycle_path}")
            raise RuntimeError("Cannot install packages with circular dependencies")

        # Get flattened dependencies for installation
        flat_deps = dependency_graph.flattened_dependencies
        deps_to_install = flat_deps.get_installation_list()

        # If specific packages were requested, filter to only those
        # **and their full transitive dependency subtrees** so that
        # sub-deps (and their MCP servers) are installed and recorded
        # in the lockfile.
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

            # Expand the set to include transitive descendants of the
            # requested packages so their MCP servers, primitives, etc.
            # are correctly installed and written to the lockfile.
            tree = dependency_graph.dependency_tree

            def _collect_descendants(node, visited=None):
                """Walk the tree and add every child identity (cycle-safe)."""
                if visited is None:
                    visited = builtins.set()
                for child in node.children:
                    identity = child.dependency_ref.get_identity()
                    if identity not in visited:
                        visited.add(identity)
                        only_identities.add(identity)
                        _collect_descendants(child, visited)

            for node in tree.nodes.values():
                if node.dependency_ref.get_identity() in only_identities:
                    _collect_descendants(node)

            deps_to_install = [
                dep for dep in deps_to_install
                if dep.get_identity() in only_identities
            ]

        if not deps_to_install and not _root_has_local_primitives:
            if logger:
                logger.nothing_to_install()
            return InstallResult()

        # ------------------------------------------------------------------
        # Orphan detection: packages in lockfile no longer in the manifest.
        # Only relevant for a full install (not apm install <pkg>).
        # We compute this NOW, before the download loop, so we know which old
        # lockfile entries to remove from the merge and which deployed files
        # to clean up after the loop.
        # ------------------------------------------------------------------
        intended_dep_keys: builtins.set = builtins.set(
            d.get_unique_key() for d in deps_to_install
        )

        # apm_modules directory already created above

        # Auto-detect target for integration (same logic as compile)
        from apm_cli.core.target_detection import (
            detect_target,
            get_target_description,
        )

        # Get config target from apm.yml if available
        config_target = apm_package.target

        # Resolve effective explicit target: CLI --target wins, then apm.yml
        _explicit = target or config_target or None

        # Determine active targets.  When --target or apm.yml target is set
        # the user's choice wins.  Otherwise auto-detect from existing dirs,
        # falling back to copilot when nothing is found.
        from apm_cli.integration.targets import resolve_targets as _resolve_targets

        _is_user = scope is InstallScope.USER
        _targets = _resolve_targets(
            project_root, user_scope=_is_user, explicit_target=_explicit,
        )

        # Log target detection results
        if logger and _targets:
            _scope_label = "global" if _is_user else "project"
            _target_names = ", ".join(
                f"{t.name} (~/{t.root_dir}/)"
                if _is_user else t.name
                for t in _targets
            )
            logger.verbose_detail(
                f"Active {_scope_label} targets: {_target_names}"
            )
            if _is_user:
                from apm_cli.deps.lockfile import get_lockfile_path
                logger.verbose_detail(
                    f"Lockfile: {get_lockfile_path(apm_dir)}"
                )

        for _t in _targets:
            if not _t.auto_create:
                continue
            _root = _t.root_dir
            _target_dir = project_root / _root
            if not _target_dir.exists():
                _target_dir.mkdir(parents=True, exist_ok=True)
                if logger:
                    logger.verbose_detail(
                        f"Created {_root}/ ({_t.name} target)"
                    )

        detected_target, detection_reason = detect_target(
            project_root=project_root,
            explicit_target=_explicit,
            config_target=config_target,
        )

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
        diagnostics = DiagnosticCollector(verbose=verbose)

        # Drain transitive failures collected during resolution into diagnostics
        for dep_display, fail_msg in transitive_failures:
            diagnostics.error(fail_msg, package=dep_display)

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
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.deps.registry_proxy import RegistryConfig
        from ..utils.content_hash import compute_package_hash as _compute_hash
        installed_packages: List[InstalledPackage] = []
        package_deployed_files: builtins.dict = {}  # dep_key → list of relative deployed paths
        package_types: builtins.dict = {}  # dep_key → package type string
        _package_hashes: builtins.dict = {}  # dep_key → sha256 hash (captured at download/verify time)

        # Resolve registry proxy configuration once for this install session.
        registry_config = RegistryConfig.from_env()

        # Build managed_files from existing lockfile for collision detection
        managed_files = builtins.set()
        existing_lockfile = LockFile.read(get_lockfile_path(apm_dir)) if apm_dir else None
        if existing_lockfile:
            for dep in existing_lockfile.dependencies.values():
                managed_files.update(dep.deployed_files)

            # Conflict: registry-only mode requires all locked deps to route
            # through the configured proxy. Deps locked to direct VCS sources
            # (github.com, GHE Cloud, GHES) are incompatible.
            if registry_config and registry_config.enforce_only:
                conflicts = registry_config.validate_lockfile_deps(
                    list(existing_lockfile.dependencies.values())
                )
                if conflicts:
                    _rich_error(
                        "PROXY_REGISTRY_ONLY is set but the lockfile contains "
                        "dependencies locked to direct VCS sources:"
                    )
                    for dep in conflicts[:10]:
                        host = dep.host or "github.com"
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        _rich_error(f"  - {name} (host: {host})")
                    _rich_error(
                        "Re-run with 'apm install --update' to re-resolve "
                        "through the registry, or unset PROXY_REGISTRY_ONLY."
                    )
                    sys.exit(1)

            # Supply chain warning: registry-proxy entries without a
            # content_hash cannot be verified on re-install.
            if registry_config and registry_config.enforce_only:
                missing = registry_config.find_missing_hashes(
                    list(existing_lockfile.dependencies.values())
                )
                if missing:
                    diagnostics.warn(
                        "The following registry-proxy dependencies have no "
                        "content_hash in the lockfile. Run 'apm install "
                        "--update' to populate hashes for tamper detection.",
                        package="lockfile",
                    )
                    for dep in missing[:10]:
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        diagnostics.warn(
                            f"  - {name} (host: {dep.host})",
                            package="lockfile",
                        )

        # Normalize path separators once for O(1) lookups in check_collision
        from apm_cli.integration.base_integrator import BaseIntegrator
        managed_files = BaseIntegrator.normalize_managed_files(managed_files)

        # Collect deployed file paths for packages that are no longer in the manifest.
        # detect_orphans() returns an empty set for partial installs automatically.
        orphaned_deployed_files = detect_orphans(
            existing_lockfile,
            intended_dep_keys,
            only_packages=only_packages,
        )

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
        unpinned_count = 0

        # Phase 4 (#171): Parallel package downloads using ThreadPoolExecutor
        # Pre-download all non-cached packages in parallel for wall-clock speedup.
        # Results are stored and consumed by the sequential integration loop below.
        from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_completed

        _pre_download_results = {}   # dep_key -> PackageInfo
        _need_download = []
        for _pd_ref in deps_to_install:
            _pd_key = _pd_ref.get_unique_key()
            _pd_path = (apm_modules_dir / _pd_ref.alias) if _pd_ref.alias else _pd_ref.get_install_path(apm_modules_dir)
            # Skip local packages — they are copied, not downloaded
            if _pd_ref.is_local:
                continue
            # Skip if already downloaded during BFS resolution
            if _pd_key in callback_downloaded:
                continue
            # Detect if manifest ref changed from what's recorded in the lockfile.
            # detect_ref_change() handles all transitions including None→ref.
            _pd_locked_chk = (
                existing_lockfile.get_dependency(_pd_key)
                if existing_lockfile
                else None
            )
            _pd_ref_changed = detect_ref_change(
                _pd_ref, _pd_locked_chk, update_refs=update_refs
            )
            # Skip if lockfile SHA matches local HEAD.
            # Normal mode: only when the ref hasn't changed in the manifest.
            # Update mode: defer to the sequential loop which resolves the
            # remote ref and compares -- if unchanged, the download is skipped
            # entirely; if changed, it falls back to sequential download.
            if (_pd_path.exists() and _pd_locked_chk
                    and _pd_locked_chk.resolved_commit
                    and _pd_locked_chk.resolved_commit != "cached"
                    and (update_refs or not _pd_ref_changed)):
                try:
                    from git import Repo as _PDGitRepo
                    if _PDGitRepo(_pd_path).head.commit.hexsha == _pd_locked_chk.resolved_commit:
                        continue
                except Exception:
                    pass
            # Build download ref (use locked commit for reproducibility).
            # build_download_ref() uses the manifest ref when ref_changed is True.
            _pd_dlref = build_download_ref(
                _pd_ref, existing_lockfile, update_refs=update_refs, ref_changed=_pd_ref_changed
            )
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
        # Reuse the shared auth_resolver (already created in this invocation) so
        # verbose auth logging does not trigger a duplicate credential-helper popup.
        _auth_resolver = auth_resolver

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

                # Skip deps that already failed during BFS resolution callback
                # to avoid a duplicate error entry in diagnostics.
                dep_key = dep_ref.get_unique_key()
                if dep_key in callback_failures:
                    if logger:
                        logger.verbose_detail(f"  Skipping {dep_key} (already failed during resolution)")
                    continue

                # --- Local package: copy from filesystem (no git download) ---
                if dep_ref.is_local and dep_ref.local_path:
                    # User scope: relative paths would resolve against $HOME
                    # instead of cwd, producing wrong results.  Skip with a
                    # clear diagnostic rather than silently failing.
                    if scope is InstallScope.USER:
                        diagnostics.warn(
                            f"Skipped local package '{dep_ref.local_path}' "
                            "-- local paths are not supported at user scope (--global). "
                            "Use a remote reference (owner/repo) instead.",
                            package=dep_ref.local_path,
                        )
                        if logger:
                            logger.verbose_detail(
                                f"  Skipping {dep_ref.local_path} (local packages "
                                "resolve against cwd, not $HOME)"
                            )
                        continue

                    result_path = _copy_local_package(dep_ref, install_path, project_root)
                    if not result_path:
                        diagnostics.error(
                            f"Failed to copy local package: {dep_ref.local_path}",
                            package=dep_ref.local_path,
                        )
                        continue

                    installed_count += 1
                    if logger:
                        logger.download_complete(dep_ref.local_path, ref_suffix="local")

                    # Build minimal PackageInfo for integration
                    from apm_cli.models.apm_package import (
                        APMPackage,
                        PackageInfo,
                        PackageType,
                        ResolvedReference,
                        GitReferenceType,
                    )
                    from datetime import datetime

                    local_apm_yml = install_path / "apm.yml"
                    if local_apm_yml.exists():
                        local_pkg = APMPackage.from_apm_yml(local_apm_yml)
                        if not local_pkg.source:
                            local_pkg.source = dep_ref.local_path
                    else:
                        local_pkg = APMPackage(
                            name=Path(dep_ref.local_path).name,
                            version="0.0.0",
                            package_path=install_path,
                            source=dep_ref.local_path,
                        )

                    local_ref = ResolvedReference(
                        original_ref="local",
                        ref_type=GitReferenceType.BRANCH,
                        resolved_commit="local",
                        ref_name="local",
                    )
                    local_info = PackageInfo(
                        package=local_pkg,
                        install_path=install_path,
                        resolved_reference=local_ref,
                        installed_at=datetime.now().isoformat(),
                        dependency_ref=dep_ref,
                    )

                    # Detect package type
                    from apm_cli.models.validation import detect_package_type
                    pkg_type, plugin_json_path = detect_package_type(install_path)
                    local_info.package_type = pkg_type
                    if pkg_type == PackageType.MARKETPLACE_PLUGIN:
                        # Normalize: synthesize .apm/ from plugin.json so
                        # integration can discover and deploy primitives
                        from apm_cli.deps.plugin_parser import normalize_plugin_directory
                        normalize_plugin_directory(install_path, plugin_json_path)

                    # Record for lockfile
                    node = dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
                    depth = node.depth if node else 1
                    resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
                    _is_dev = node.is_dev if node else False
                    installed_packages.append(InstalledPackage(
                        dep_ref=dep_ref, resolved_commit=None,
                        depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
                        registry_config=None,  # local deps never go through registry
                    ))
                    dep_key = dep_ref.get_unique_key()
                    if install_path.is_dir() and not dep_ref.is_local:
                        _package_hashes[dep_key] = _compute_hash(install_path)
                    dep_deployed_files: builtins.list = []

                    if hasattr(local_info, 'package_type') and local_info.package_type:
                        package_types[dep_key] = local_info.package_type.value

                    # Use the same variable name as the rest of the loop
                    package_info = local_info

                    # Run shared integration pipeline
                    try:
                        # Pre-deploy security gate
                        if not _pre_deploy_security_scan(
                            install_path, diagnostics,
                            package_name=dep_key, force=force,
                            logger=logger,
                        ):
                            package_deployed_files[dep_key] = []
                            continue

                        int_result = _integrate_package_primitives(
                            package_info, project_root,
                            targets=_targets,
                            prompt_integrator=prompt_integrator,
                            agent_integrator=agent_integrator,
                            skill_integrator=skill_integrator,
                            instruction_integrator=instruction_integrator,
                            command_integrator=command_integrator,
                            hook_integrator=hook_integrator,
                            force=force,
                            managed_files=managed_files,
                            diagnostics=diagnostics,
                            package_name=dep_key,
                            logger=logger,
                            scope=scope,
                        )
                        total_prompts_integrated += int_result["prompts"]
                        total_agents_integrated += int_result["agents"]
                        total_skills_integrated += int_result["skills"]
                        total_sub_skills_promoted += int_result["sub_skills"]
                        total_instructions_integrated += int_result["instructions"]
                        total_commands_integrated += int_result["commands"]
                        total_hooks_integrated += int_result["hooks"]
                        total_links_resolved += int_result["links_resolved"]
                        dep_deployed_files.extend(int_result["deployed_files"])
                    except Exception as e:
                        diagnostics.error(
                            f"Failed to integrate primitives from local package: {e}",
                            package=dep_ref.local_path,
                        )

                    package_deployed_files[dep_key] = dep_deployed_files

                    # In verbose mode, show inline skip/error count for this package
                    if logger and logger.verbose:
                        _skip_count = diagnostics.count_for_package(dep_key, "collision")
                        _err_count = diagnostics.count_for_package(dep_key, "error")
                        if _skip_count > 0:
                            noun = "file" if _skip_count == 1 else "files"
                            logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
                        if _err_count > 0:
                            noun = "error" if _err_count == 1 else "errors"
                            logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")
                    continue

                # npm-like behavior: Branches always fetch latest, only tags/commits use cache
                # Resolve git reference to determine type
                from apm_cli.models.apm_package import GitReferenceType

                resolved_ref = None
                if dep_ref.get_unique_key() not in _pre_downloaded_keys:
                    # Resolve when there is an explicit ref, OR when update_refs
                    # is True AND we have a non-cached lockfile entry to compare
                    # against (otherwise resolution is wasted work -- the package
                    # will be downloaded regardless).
                    _has_lockfile_sha = False
                    if update_refs and existing_lockfile:
                        _lck = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                        _has_lockfile_sha = bool(
                            _lck and _lck.resolved_commit and _lck.resolved_commit != "cached"
                        )
                    if dep_ref.reference or (update_refs and _has_lockfile_sha):
                        try:
                            resolved_ref = downloader.resolve_git_reference(dep_ref)
                        except Exception:
                            pass  # If resolution fails, skip cache (fetch latest)

                # Use cache only for tags and commits (not branches)
                is_cacheable = resolved_ref and resolved_ref.ref_type in [
                    GitReferenceType.TAG,
                    GitReferenceType.COMMIT,
                ]
                # Skip download if: already fetched by resolver callback, or cached tag/commit
                already_resolved = dep_ref.get_unique_key() in callback_downloaded
                # Detect if manifest ref changed vs what the lockfile recorded.
                # detect_ref_change() handles all transitions including None→ref.
                _dep_locked_chk = (
                    existing_lockfile.get_dependency(dep_ref.get_unique_key())
                    if existing_lockfile
                    else None
                )
                ref_changed = detect_ref_change(
                    dep_ref, _dep_locked_chk, update_refs=update_refs
                )
                # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
                # -- but not when the manifest ref has changed (user wants different version).
                lockfile_match = False
                if install_path.exists() and existing_lockfile:
                    locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                    if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                        if update_refs:
                            # Update mode: compare resolved remote SHA with lockfile SHA.
                            # If the remote ref still resolves to the same commit,
                            # the package content is unchanged -- skip download.
                            # Also verify local checkout matches to guard against
                            # corrupted installs that bypassed pre-download checks.
                            if resolved_ref and resolved_ref.resolved_commit == locked_dep.resolved_commit:
                                try:
                                    from git import Repo as GitRepo
                                    local_repo = GitRepo(install_path)
                                    if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                                        lockfile_match = True
                                except Exception:
                                    pass  # Local checkout invalid -- fall through to download
                        elif not ref_changed:
                            # Normal mode: compare local HEAD with lockfile SHA.
                            try:
                                from git import Repo as GitRepo
                                local_repo = GitRepo(install_path)
                                if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                                    lockfile_match = True
                            except Exception:
                                pass  # Not a git repo or invalid -- fall through to download
                skip_download = install_path.exists() and (
                    (is_cacheable and not update_refs)
                    or (already_resolved and not update_refs)
                    or lockfile_match
                )

                # Verify content integrity when lockfile has a hash
                if skip_download and _dep_locked_chk and _dep_locked_chk.content_hash:
                    from ..utils.content_hash import verify_package_hash
                    if not verify_package_hash(install_path, _dep_locked_chk.content_hash):
                        _hash_msg = (
                            f"Content hash mismatch for "
                            f"{dep_ref.get_unique_key()} -- re-downloading"
                        )
                        diagnostics.warn(_hash_msg, package=dep_ref.get_unique_key())
                        if logger:
                            logger.progress(_hash_msg)
                        safe_rmtree(install_path, apm_modules_dir)
                        skip_download = False

                # When registry-only mode is active, bypass cache if the
                # cached artifact was NOT previously downloaded via the
                # registry (no registry_prefix in lockfile). This handles
                # the transition from direct-VCS installs to proxy installs
                # for packages not yet in the lockfile.
                if (
                    skip_download
                    and registry_config
                    and registry_config.enforce_only
                    and not dep_ref.is_local
                ):
                    if not _dep_locked_chk or _dep_locked_chk.registry_prefix is None:
                        skip_download = False

                if skip_download:
                    display_name = (
                        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
                    )
                    # Show resolved ref from lockfile for consistency with fresh installs
                    _ref = dep_ref.reference or ""
                    _sha = ""
                    if _dep_locked_chk and _dep_locked_chk.resolved_commit and _dep_locked_chk.resolved_commit != "cached":
                        _sha = _dep_locked_chk.resolved_commit[:8]
                    if logger:
                        logger.download_complete(display_name, ref=_ref, sha=_sha, cached=True)
                    installed_count += 1
                    if not dep_ref.reference:
                        unpinned_count += 1

                    # Skip integration if not needed
                    if not _targets:
                        continue

                    # Integrate prompts for cached packages (zero-config behavior)
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
                        apm_yml_path = install_path / APM_YML_FILENAME
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

                        # Use resolved reference from ref resolution if available
                        # (e.g. when update_refs matched the lockfile SHA),
                        # otherwise create a placeholder for cached packages.
                        resolved_or_cached_ref = resolved_ref if resolved_ref else ResolvedReference(
                            original_ref=dep_ref.reference or "default",
                            ref_type=GitReferenceType.BRANCH,
                            resolved_commit="cached",  # Mark as cached since we don't know exact commit
                            ref_name=dep_ref.reference or "default",
                        )

                        cached_package_info = PackageInfo(
                            package=cached_package,
                            install_path=install_path,
                            resolved_reference=resolved_or_cached_ref,
                            installed_at=datetime.now().isoformat(),
                            dependency_ref=dep_ref,  # Store for canonical dependency string
                        )

                        # Detect package_type from disk contents so
                        # skill integration is not silently skipped
                        from apm_cli.models.validation import detect_package_type
                        pkg_type, _ = detect_package_type(install_path)
                        cached_package_info.package_type = pkg_type

                        # Collect for lockfile (cached packages still need to be tracked)
                        node = dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
                        depth = node.depth if node else 1
                        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
                        _is_dev = node.is_dev if node else False
                        # Get commit SHA: resolved ref > callback capture > existing lockfile > explicit reference
                        dep_key = dep_ref.get_unique_key()
                        cached_commit = None
                        if resolved_ref and resolved_ref.resolved_commit and resolved_ref.resolved_commit != "cached":
                            cached_commit = resolved_ref.resolved_commit
                        if not cached_commit:
                            cached_commit = callback_downloaded.get(dep_key)
                        if not cached_commit and existing_lockfile:
                            locked_dep = existing_lockfile.get_dependency(dep_key)
                            if locked_dep:
                                cached_commit = locked_dep.resolved_commit
                        if not cached_commit:
                            cached_commit = dep_ref.reference
                        # Determine if the cached package came from the registry:
                        # prefer the lockfile record, then the current registry config.
                        _cached_registry = None
                        if _dep_locked_chk and _dep_locked_chk.registry_prefix:
                            # Reconstruct RegistryConfig from lockfile to preserve original source
                            _cached_registry = registry_config
                        elif registry_config and not dep_ref.is_local:
                            _cached_registry = registry_config
                        installed_packages.append(InstalledPackage(
                            dep_ref=dep_ref, resolved_commit=cached_commit,
                            depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
                            registry_config=_cached_registry,
                        ))
                        if install_path.is_dir():
                            _package_hashes[dep_key] = _compute_hash(install_path)
                        # Track package type for lockfile
                        if hasattr(cached_package_info, 'package_type') and cached_package_info.package_type:
                            package_types[dep_key] = cached_package_info.package_type.value

                        # Pre-deploy security gate
                        if not _pre_deploy_security_scan(
                            install_path, diagnostics,
                            package_name=dep_key, force=force,
                            logger=logger,
                        ):
                            package_deployed_files[dep_key] = []
                            continue

                        int_result = _integrate_package_primitives(
                            cached_package_info, project_root,
                            targets=_targets,
                            prompt_integrator=prompt_integrator,
                            agent_integrator=agent_integrator,
                            skill_integrator=skill_integrator,
                            instruction_integrator=instruction_integrator,
                            command_integrator=command_integrator,
                            hook_integrator=hook_integrator,
                            force=force,
                            managed_files=managed_files,
                            diagnostics=diagnostics,
                            package_name=dep_key,
                            logger=logger,
                            scope=scope,
                        )
                        total_prompts_integrated += int_result["prompts"]
                        total_agents_integrated += int_result["agents"]
                        total_skills_integrated += int_result["skills"]
                        total_sub_skills_promoted += int_result["sub_skills"]
                        total_instructions_integrated += int_result["instructions"]
                        total_commands_integrated += int_result["commands"]
                        total_hooks_integrated += int_result["hooks"]
                        total_links_resolved += int_result["links_resolved"]
                        dep_deployed = int_result["deployed_files"]
                        package_deployed_files[dep_key] = dep_deployed
                    except Exception as e:
                        diagnostics.error(
                            f"Failed to integrate primitives from cached package: {e}",
                            package=dep_key,
                        )

                    # In verbose mode, show inline skip/error count for this package
                    if logger and logger.verbose:
                        _skip_count = diagnostics.count_for_package(dep_key, "collision")
                        _err_count = diagnostics.count_for_package(dep_key, "error")
                        if _skip_count > 0:
                            noun = "file" if _skip_count == 1 else "files"
                            logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
                        if _err_count > 0:
                            noun = "error" if _err_count == 1 else "errors"
                            logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

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

                    # T5: Build download ref - use locked commit if available.
                    # build_download_ref() uses manifest ref when ref_changed is True.
                    download_ref = build_download_ref(
                        dep_ref, existing_lockfile, update_refs=update_refs, ref_changed=ref_changed
                    )

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

                    # Show resolved ref alongside package name for visibility
                    resolved = getattr(package_info, 'resolved_reference', None)
                    if logger:
                        _ref = ""
                        _sha = ""
                        if resolved:
                            _ref = resolved.ref_name if resolved.ref_name else ""
                            _sha = resolved.resolved_commit[:8] if resolved.resolved_commit else ""
                        logger.download_complete(display_name, ref=_ref, sha=_sha)
                        # Log auth source for this download (verbose only)
                        if _auth_resolver:
                            try:
                                _host = dep_ref.host or "github.com"
                                _org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
                                _ctx = _auth_resolver.resolve(_host, org=_org)
                                logger.package_auth(_ctx.source, _ctx.token_type or "none")
                            except Exception:
                                pass
                    else:
                        _ref_suffix = ""
                        if resolved:
                            _r = resolved.ref_name if resolved.ref_name else ""
                            _s = resolved.resolved_commit[:8] if resolved.resolved_commit else ""
                            if _r and _s:
                                _ref_suffix = f" #{_r} @{_s}"
                            elif _r:
                                _ref_suffix = f" #{_r}"
                            elif _s:
                                _ref_suffix = f" @{_s}"
                        _rich_success(f"[+] {display_name}{_ref_suffix}")

                    # Track unpinned deps for aggregated diagnostic
                    if not dep_ref.reference:
                        unpinned_count += 1

                    # Collect for lockfile: get resolved commit and depth
                    resolved_commit = None
                    if resolved:
                        resolved_commit = package_info.resolved_reference.resolved_commit
                    # Get depth from dependency tree
                    node = dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
                    depth = node.depth if node else 1
                    resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
                    _is_dev = node.is_dev if node else False
                    installed_packages.append(InstalledPackage(
                        dep_ref=dep_ref, resolved_commit=resolved_commit,
                        depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
                        registry_config=registry_config if not dep_ref.is_local else None,
                    ))
                    if install_path.is_dir():
                        _package_hashes[dep_ref.get_unique_key()] = _compute_hash(install_path)

                    # Supply chain protection: verify content hash on fresh
                    # downloads when the lockfile already records a hash.
                    # A mismatch means the downloaded content differs from
                    # what was previously locked — possible tampering.
                    if (
                        not update_refs
                        and _dep_locked_chk
                        and _dep_locked_chk.content_hash
                        and dep_ref.get_unique_key() in _package_hashes
                    ):
                        _fresh_hash = _package_hashes[dep_ref.get_unique_key()]
                        if _fresh_hash != _dep_locked_chk.content_hash:
                            safe_rmtree(install_path, apm_modules_dir)
                            _rich_error(
                                f"Content hash mismatch for "
                                f"{dep_ref.get_unique_key()}: "
                                f"expected {_dep_locked_chk.content_hash}, "
                                f"got {_fresh_hash}. "
                                "The downloaded content differs from the "
                                "lockfile record. This may indicate a "
                                "supply-chain attack. Use 'apm install "
                                "--update' to accept new content and "
                                "update the lockfile."
                            )
                            sys.exit(1)

                    # Track package type for lockfile
                    if hasattr(package_info, 'package_type') and package_info.package_type:
                        package_types[dep_ref.get_unique_key()] = package_info.package_type.value

                    # Show package type in verbose mode
                    if hasattr(package_info, "package_type"):
                        from apm_cli.models.apm_package import PackageType

                        package_type = package_info.package_type
                        _type_label = {
                            PackageType.CLAUDE_SKILL: "Skill (SKILL.md detected)",
                            PackageType.MARKETPLACE_PLUGIN: "Marketplace Plugin (plugin.json detected)",
                            PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
                            PackageType.APM_PACKAGE: "APM Package (apm.yml)",
                        }.get(package_type)
                        if _type_label and logger:
                            logger.package_type_info(_type_label)

                    # Auto-integrate prompts and agents if enabled
                    # Pre-deploy security gate
                    if not _pre_deploy_security_scan(
                        package_info.install_path, diagnostics,
                        package_name=dep_ref.get_unique_key(), force=force,
                        logger=logger,
                    ):
                        package_deployed_files[dep_ref.get_unique_key()] = []
                        continue

                    if _targets:
                        try:
                            int_result = _integrate_package_primitives(
                                package_info, project_root,
                                targets=_targets,
                                prompt_integrator=prompt_integrator,
                                agent_integrator=agent_integrator,
                                skill_integrator=skill_integrator,
                                instruction_integrator=instruction_integrator,
                                command_integrator=command_integrator,
                                hook_integrator=hook_integrator,
                                force=force,
                                managed_files=managed_files,
                                diagnostics=diagnostics,
                                package_name=dep_ref.get_unique_key(),
                                logger=logger,
                                scope=scope,
                            )
                            total_prompts_integrated += int_result["prompts"]
                            total_agents_integrated += int_result["agents"]
                            total_skills_integrated += int_result["skills"]
                            total_sub_skills_promoted += int_result["sub_skills"]
                            total_instructions_integrated += int_result["instructions"]
                            total_commands_integrated += int_result["commands"]
                            total_hooks_integrated += int_result["hooks"]
                            total_links_resolved += int_result["links_resolved"]
                            dep_deployed_fresh = int_result["deployed_files"]
                            package_deployed_files[dep_ref.get_unique_key()] = dep_deployed_fresh
                        except Exception as e:
                            # Don't fail installation if integration fails
                            diagnostics.error(
                                f"Failed to integrate primitives: {e}",
                                package=dep_ref.get_unique_key(),
                            )

                        # In verbose mode, show inline skip/error count for this package
                        if logger and logger.verbose:
                            pkg_key = dep_ref.get_unique_key()
                            _skip_count = diagnostics.count_for_package(pkg_key, "collision")
                            _err_count = diagnostics.count_for_package(pkg_key, "error")
                            if _skip_count > 0:
                                noun = "file" if _skip_count == 1 else "files"
                                logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
                            if _err_count > 0:
                                noun = "error" if _err_count == 1 else "errors"
                                logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

                except Exception as e:
                    display_name = (
                        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
                    )
                    # Remove the progress task on error
                    if "task_id" in locals():
                        progress.remove_task(task_id)
                    diagnostics.error(
                        f"Failed to install {display_name}: {e}",
                        package=dep_ref.get_unique_key(),
                    )
                    # Continue with other packages instead of failing completely
                    continue

        # ------------------------------------------------------------------
        # Integrate root project's own .apm/ primitives (#714).
        #
        # Users should not need a dummy "./agent/apm.yml" stub to get their
        # root-level .apm/ rules deployed alongside external dependencies.
        # Treat the project root as an implicit local package: any primitives
        # found in <project_root>/.apm/ are integrated after all declared
        # dependency packages have been processed.
        # ------------------------------------------------------------------
        if _root_has_local_primitives and _targets:
            from apm_cli.models.apm_package import PackageInfo as _PackageInfo
            _root_pkg_info = _PackageInfo(
                package=apm_package,
                install_path=project_root,
            )
            if logger:
                logger.download_complete("<project root>", ref_suffix="local")
            try:
                _root_result = _integrate_package_primitives(
                    _root_pkg_info, project_root,
                    targets=_targets,
                    prompt_integrator=prompt_integrator,
                    agent_integrator=agent_integrator,
                    skill_integrator=skill_integrator,
                    instruction_integrator=instruction_integrator,
                    command_integrator=command_integrator,
                    hook_integrator=hook_integrator,
                    force=force,
                    managed_files=managed_files,
                    diagnostics=diagnostics,
                    package_name="<root>",
                    logger=logger,
                    scope=scope,
                )
                total_prompts_integrated += _root_result["prompts"]
                total_agents_integrated += _root_result["agents"]
                total_instructions_integrated += _root_result["instructions"]
                total_commands_integrated += _root_result["commands"]
                total_hooks_integrated += _root_result["hooks"]
                total_links_resolved += _root_result["links_resolved"]
                installed_count += 1
            except Exception as e:
                import traceback as _tb
                diagnostics.error(
                    f"Failed to integrate root project primitives: {e}",
                    package="<root>",
                    detail=_tb.format_exc(),
                )
                # When root integration is the *only* action (no external deps),
                # a failure means nothing was deployed — surface it clearly.
                if not all_apm_deps and logger:
                    logger.error(
                        f"Root project primitives could not be integrated: {e}"
                    )

        # Update .gitignore
        _update_gitignore_for_apm_modules(logger=logger)

        # ------------------------------------------------------------------
        # Orphan cleanup: remove deployed files for packages that were
        # removed from the manifest.  This happens on every full install
        # (no only_packages), making apm install idempotent with the manifest.
        # Handles both regular files and directory entries (e.g., legacy skills).
        # ------------------------------------------------------------------
        if orphaned_deployed_files:
            import shutil as _shutil
            _removed_orphan_count = 0
            _failed_orphan_count = 0
            _deleted_orphan_paths: builtins.list = []
            # Build validation targets that cover both default KNOWN_TARGETS
            # and scope-resolved targets so legacy project-scope orphan paths
            # (e.g., ".github/...") are also cleaned up at user scope.
            _validation_targets = _targets or {}
            _default_targets = getattr(BaseIntegrator, "KNOWN_TARGETS", None)
            if _default_targets:
                _validation_targets = {**_default_targets, **_validation_targets}
            for _orphan_path in sorted(orphaned_deployed_files):
                # validate_deploy_path() is the safety gate: it rejects path-traversal,
                # requires a known integration prefix, and checks the resolved path
                # stays within project_root -- so rmtree is safe here.
                if BaseIntegrator.validate_deploy_path(_orphan_path, project_root, targets=_validation_targets):
                    _target = project_root / _orphan_path
                    if _target.exists():
                        try:
                            if _target.is_dir():
                                _shutil.rmtree(_target)
                            else:
                                _target.unlink()
                            _deleted_orphan_paths.append(_target)
                            _removed_orphan_count += 1
                        except Exception as _orphan_err:
                            _orphan_msg = f"Could not remove orphaned path {_orphan_path}: {_orphan_err}"
                            diagnostics.error(_orphan_msg)
                            if logger:
                                logger.verbose_detail(f"  {_orphan_msg}")
                            _failed_orphan_count += 1
            # Clean up empty parent directories left after file removal
            if _deleted_orphan_paths:
                BaseIntegrator.cleanup_empty_parents(_deleted_orphan_paths, project_root)
            if _removed_orphan_count > 0:
                if logger:
                    logger.verbose_detail(
                        f"Removed {_removed_orphan_count} file(s) from packages "
                        "no longer in apm.yml"
                    )

        # ------------------------------------------------------------------
        # Stale-file cleanup: within each package still present in the
        # manifest, remove files that were in the previous lockfile's
        # deployed_files but are not in the fresh integration output.
        # Handles renames and intra-package file removals (issue #666).
        # Complements the package-level orphan cleanup above, which handles
        # packages that left the manifest entirely.
        # ------------------------------------------------------------------
        if existing_lockfile and package_deployed_files:
            import shutil as _shutil
            _validation_targets = _targets or {}
            _default_targets = getattr(BaseIntegrator, "KNOWN_TARGETS", None)
            if _default_targets:
                _validation_targets = {**_default_targets, **_validation_targets}

            for dep_key, new_deployed in package_deployed_files.items():
                # Skip packages whose integration reported errors this run --
                # a file that failed to re-deploy would look stale and get
                # wrongly deleted.
                if diagnostics.count_for_package(dep_key, "error") > 0:
                    continue

                prev_dep = existing_lockfile.get_dependency(dep_key)
                if not prev_dep:
                    continue  # new package this install -- nothing stale yet
                old_deployed = builtins.list(prev_dep.deployed_files)

                stale = detect_stale_files(old_deployed, new_deployed)
                if not stale:
                    continue

                _stale_failed: builtins.list = []
                _stale_deleted_paths: builtins.list = []
                for stale_path in sorted(stale):
                    # validate_deploy_path is the safety gate: no traversal,
                    # must start with a known integration prefix, must resolve
                    # inside project_root.
                    if not BaseIntegrator.validate_deploy_path(
                        stale_path, project_root, targets=_validation_targets
                    ):
                        continue
                    stale_target = project_root / stale_path
                    if not stale_target.exists():
                        continue
                    try:
                        if stale_target.is_dir():
                            _shutil.rmtree(stale_target)
                        else:
                            stale_target.unlink()
                        _stale_deleted_paths.append(stale_target)
                    except Exception as stale_err:
                        _stale_failed.append(stale_path)
                        stale_msg = f"Could not remove stale path {stale_path}: {stale_err}"
                        diagnostics.error(stale_msg, package=dep_key)
                        if logger:
                            logger.verbose_detail(f"  {stale_msg}")

                # Re-insert failed paths so the lockfile retains them for
                # retry on the next install (matches the local-package
                # stale-cleanup pattern in install.py:1025).
                new_deployed.extend(_stale_failed)

                if _stale_deleted_paths:
                    BaseIntegrator.cleanup_empty_parents(
                        _stale_deleted_paths, project_root
                    )
                _removed = len(_stale_deleted_paths)
                if _removed > 0 and logger:
                    logger.verbose_detail(
                        f"Removed {_removed} stale file(s) from {dep_key}"
                    )

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        if installed_packages:
            try:
                lockfile = LockFile.from_installed_packages(installed_packages, dependency_graph)
                # Attach deployed_files and package_type to each LockedDependency
                for dep_key, dep_files in package_deployed_files.items():
                    if dep_key in lockfile.dependencies:
                        lockfile.dependencies[dep_key].deployed_files = dep_files
                for dep_key, pkg_type in package_types.items():
                    if dep_key in lockfile.dependencies:
                        lockfile.dependencies[dep_key].package_type = pkg_type
                # Attach content hashes captured at download/verify time
                for dep_key, locked_dep in lockfile.dependencies.items():
                    if dep_key in _package_hashes:
                        locked_dep.content_hash = _package_hashes[dep_key]
                # Attach marketplace provenance if available
                if marketplace_provenance:
                    for dep_key, prov in marketplace_provenance.items():
                        if dep_key in lockfile.dependencies:
                            lockfile.dependencies[dep_key].discovered_via = prov.get("discovered_via")
                            lockfile.dependencies[dep_key].marketplace_plugin_name = prov.get("marketplace_plugin_name")
                # Selectively merge entries from the existing lockfile:
                #   - For partial installs (only_packages): preserve all old entries
                #     (sequential install — only the specified package was processed).
                #   - For full installs: only preserve entries for packages still in
                #     the manifest that failed to download (in intended_dep_keys but
                #     not in the new lockfile due to a download error).
                #   - Orphaned entries (not in intended_dep_keys) are intentionally
                #     dropped so the lockfile matches the manifest.
                # Skip merge entirely when update_refs is set — stale entries must not survive.
                if existing_lockfile and not update_refs:
                    for dep_key, dep in existing_lockfile.dependencies.items():
                        if dep_key not in lockfile.dependencies:
                            if only_packages or dep_key in intended_dep_keys:
                                # Preserve: partial install (sequential install support)
                                # OR package still in manifest but failed to download.
                                lockfile.dependencies[dep_key] = dep
                            # else: orphan — package was in lockfile but is no longer in
                            # the manifest (full install only). Don't preserve so the
                            # lockfile stays in sync with what apm.yml declares.
                lockfile_path = get_lockfile_path(apm_dir)

                # When installing a subset of packages (apm install <pkg>),
                # merge new entries into the existing lockfile instead of
                # overwriting it — otherwise the uninstalled packages disappear.
                if only_packages:
                    existing = LockFile.read(lockfile_path)
                    if existing:
                        for key, dep in lockfile.dependencies.items():
                            existing.add_dependency(dep)
                        lockfile = existing

                # Only write when the semantic content has actually changed
                # (avoids generated_at churn in version control).
                existing_lockfile = LockFile.read(lockfile_path) if lockfile_path.exists() else None
                if existing_lockfile and lockfile.is_semantically_equivalent(existing_lockfile):
                    if logger:
                        logger.verbose_detail("apm.lock.yaml unchanged -- skipping write")
                else:
                    lockfile.save(lockfile_path)
                    if logger:
                        logger.verbose_detail(f"Generated apm.lock.yaml with {len(lockfile.dependencies)} dependencies")
            except Exception as e:
                _lock_msg = f"Could not generate apm.lock.yaml: {e}"
                diagnostics.error(_lock_msg)
                if logger:
                    logger.error(_lock_msg)

        # Show integration stats (verbose-only when logger is available)
        if total_links_resolved > 0:
            if logger:
                logger.verbose_detail(f"Resolved {total_links_resolved} context file links")

        if total_commands_integrated > 0:
            if logger:
                logger.verbose_detail(f"Integrated {total_commands_integrated} command(s)")

        if total_hooks_integrated > 0:
            if logger:
                logger.verbose_detail(f"Integrated {total_hooks_integrated} hook(s)")

        if total_instructions_integrated > 0:
            if logger:
                logger.verbose_detail(f"Integrated {total_instructions_integrated} instruction(s)")

        # Summary is now emitted by the caller via logger.install_summary()
        if not logger:
            _rich_success(f"Installed {installed_count} APM dependencies")

        if unpinned_count:
            noun = "dependency has" if unpinned_count == 1 else "dependencies have"
            diagnostics.info(
                f"{unpinned_count} {noun} no pinned version "
                f"-- pin with #tag or #sha to prevent drift"
            )

        return InstallResult(installed_count, total_prompts_integrated, total_agents_integrated, diagnostics)

    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")




