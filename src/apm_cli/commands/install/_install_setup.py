"""Install scope/path setup helpers extracted from install_impl.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...constants import APM_YML_FILENAME
from ...core.auth import AuthResolver
from ...deps.transport_selection import (
    ProtocolPreference,
    is_fallback_allowed,
    protocol_pref_from_env,
)
from ...utils.console import _rich_error
from .._helpers import _create_minimal_apm_yml, _get_default_config

if TYPE_CHECKING:
    from ...core.command_logger import InstallLogger
    from ...core.scope import InstallScope


def _resolve_protocol_pref(
    use_ssh: bool,
    use_https: bool,
    allow_protocol_fallback: bool,
) -> tuple:
    """Validate and resolve protocol preference flags.

    Returns ``(protocol_pref, allow_protocol_fallback)``.
    Calls ``sys.exit(2)`` when ``--ssh`` and ``--https`` are both set.
    """
    if use_ssh and use_https:
        _rich_error("Options --ssh and --https are mutually exclusive.", symbol="error")
        sys.exit(2)
    if use_ssh:
        protocol_pref = ProtocolPreference.SSH
    elif use_https:
        protocol_pref = ProtocolPreference.HTTPS
    else:
        protocol_pref = protocol_pref_from_env()
    # CLI flag OR env var enables fallback.
    allow_protocol_fallback = allow_protocol_fallback or is_fallback_allowed()
    return protocol_pref, allow_protocol_fallback


def _setup_scope_and_paths(
    global_: bool,
    packages: list,
    logger: InstallLogger,
) -> tuple:
    """Resolve install scope, filesystem paths, and shared auth resolver.

    Returns
    -------
    ``(scope, manifest_path, apm_dir, manifest_display, project_root,
      auth_resolver, apm_yml_exists)``

    May call ``sys.exit(1)`` when no apm.yml and no packages are provided.
    """
    from ...core.scope import (
        InstallScope,
        ensure_user_dirs,
        get_apm_dir,
        get_deploy_root,
        get_manifest_path,
        warn_unsupported_user_scope,
    )

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
    project_root = get_deploy_root(scope)

    # Create shared auth resolver for all downloads in this CLI invocation
    # to ensure credentials are cached and reused (prevents duplicate auth popups)
    auth_resolver = AuthResolver()
    # F2/F3 #856: thread the InstallLogger into AuthResolver so the verbose
    # auth-source line and the deferred stale-PAT [!] warning route through
    # CommandLogger / DiagnosticCollector instead of stderr/inline writes.
    auth_resolver.set_logger(logger)

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

    return (
        scope,
        manifest_path,
        apm_dir,
        manifest_display,
        project_root,
        auth_resolver,
        apm_yml_exists,
    )
