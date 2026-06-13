"""Extracted install command phase helpers to reduce install() complexity."""

import builtins
import sys
import time
from pathlib import Path

import click


def _try_local_bundle_install(
    packages,
    mcp_name,
    target,
    global_,
    force,
    dry_run,
    verbose,
    alias,
    logger,
    legacy_skill_paths,
    rejected_flags,
):
    """Detect and install a local bundle; return True if handled (caller should return).

    Raises :class:`click.UsageError` for unrecognised tarballs.  Falls through
    (returns False) when the sole positional arg is not a recognised bundle.
    """
    if not (len(packages) == 1 and not mcp_name and (_probe := Path(packages[0])).exists()):
        return False

    from apm_cli.bundle.local_bundle import detect_local_bundle as _detect_lb
    from apm_cli.install.local_bundle_handler import install_local_bundle as _install_lb

    try:
        _bundle_info = _detect_lb(_probe)
    except ValueError as exc:
        raise click.UsageError(f"Bundle security check failed: {exc}") from exc
    if _bundle_info is not None:
        _install_lb(
            bundle_info=_bundle_info,
            bundle_arg=packages[0],
            target=target,
            global_=global_,
            force=force,
            dry_run=dry_run,
            verbose=verbose,
            alias=alias,
            logger=logger,
            legacy_skill_paths=legacy_skill_paths,
            rejected_flags=rejected_flags,
        )
        return True
    # IM7: path exists but isn't a recognised bundle.  For archive extensions
    # (.zip / .tar.gz / .tgz) the user clearly meant a bundle artifact.
    _suffix = _probe.name.lower()
    if _probe.is_file() and _suffix.endswith((".zip", ".tar.gz", ".tgz")):
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        if _looks_like_legacy_apm_bundle(_probe):
            raise click.UsageError(
                f"'{packages[0]}' was packed with '--format apm' (legacy format). "
                "'apm install <bundle>' requires the plugin format. "
                "Repack with 'apm pack --format plugin --archive', "
                "or use 'apm unpack' to deploy the legacy bundle."
            )
        raise click.UsageError(
            f"'{packages[0]}' is not a valid APM bundle archive "
            "(no plugin.json found at the bundle root). "
            "Use 'apm install org/package' for registry installs, "
            "or repack the source with 'apm pack'."
        )
    return False


def _resolve_protocol_and_fallback(use_ssh, use_https, allow_protocol_fallback):
    """Return ``(protocol_pref, allow_protocol_fallback)`` or ``(None, None)`` on conflict.

    Caller must check for ``(None, None)`` and emit the mutual-exclusion error.
    """
    from apm_cli.config import get_apm_allow_protocol_fallback, get_apm_protocol_pref
    from apm_cli.deps.transport_selection import ProtocolPreference

    if use_ssh and use_https:
        return None, None
    if use_ssh:
        pref = ProtocolPreference.SSH
    elif use_https:
        pref = ProtocolPreference.HTTPS
    else:
        pref = ProtocolPreference.from_str(get_apm_protocol_pref())
    fallback = allow_protocol_fallback or get_apm_allow_protocol_fallback()
    return pref, fallback


def _resolve_scope_and_paths(global_, logger):
    """Resolve install scope, paths, and scope-specific warnings.

    Returns ``(scope, manifest_path, apm_dir, manifest_display, project_root)``.
    """
    from apm_cli.constants import APM_YML_FILENAME
    from apm_cli.core.scope import (
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

    manifest_path = get_manifest_path(scope)
    apm_dir = get_apm_dir(scope)
    manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME
    project_root = get_deploy_root(scope)
    return scope, manifest_path, apm_dir, manifest_display, project_root


def _setup_auth_and_check_manifest(scope, packages, manifest_path, manifest_display, logger):
    """Create shared :class:`AuthResolver`; bootstrap or error-check ``apm.yml``.

    May call ``sys.exit(1)`` when no manifest exists and no packages are given.
    Returns the newly constructed :class:`AuthResolver` instance.
    """
    # RULE B: AuthResolver is patched at apm_cli.commands.install.AuthResolver in tests.
    import apm_cli.commands.install as _m
    from apm_cli.commands._helpers import _create_minimal_apm_yml, _get_default_config
    from apm_cli.core.scope import InstallScope

    auth_resolver = _m.AuthResolver()
    # F2/F3 #856: thread the InstallLogger into AuthResolver so the verbose
    # auth-source line and the deferred stale-PAT [!] warning route through
    # CommandLogger / DiagnosticCollector instead of stderr/inline writes.
    auth_resolver.set_logger(logger)

    apm_yml_exists = manifest_path.exists()

    # Auto-bootstrap: create minimal apm.yml when packages specified but no apm.yml
    if not apm_yml_exists and packages:
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

    return auth_resolver


def _execute_install_and_summary(install_ctx, outcome, frozen, install_started_at):
    """Run the install pipeline and emit the post-install summary.

    Returns ``apm_count`` (number of installed APM packages).
    """
    # RULE B: _install_apm_packages, _post_install_summary, _rich_info are patched
    # at apm_cli.commands.install.* in tests.
    import apm_cli.commands.install as _m

    apm_count, mcp_count, lsp_count, apm_diagnostics = _m._install_apm_packages(
        install_ctx, outcome
    )
    _m._post_install_summary(
        logger=install_ctx.logger,
        apm_count=apm_count,
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        apm_diagnostics=apm_diagnostics,
        force=install_ctx.force,
        elapsed_seconds=time.perf_counter() - install_started_at,
    )
    if frozen and apm_count > 0:
        # --frozen verifies LOCKFILE STRUCTURE (every apm.yml dep has a lock entry),
        # not on-disk content integrity.
        _m._rich_info(
            "Lockfile presence verified. Run 'apm audit' for on-disk content integrity.",
            symbol="info",
        )
    return apm_count


def _compute_argv_pre_dash(packages):
    """Return ``(command_argv, pre_dash_packages)`` by splitting sys.argv at ``--``.

    Uses RULE B so ``@patch("apm_cli.commands.install._split_argv_at_double_dash")``
    and ``@patch("apm_cli.commands.install._get_invocation_argv")`` still work.
    """
    # RULE B: argv helpers are @patch targets in tests.
    import apm_cli.commands.install as _m

    _, command_argv = _m._split_argv_at_double_dash(_m._get_invocation_argv())
    if command_argv:
        split_idx = max(len(packages) - len(command_argv), 0)
        pre_dash_packages = builtins.tuple(packages[:split_idx])
    else:
        pre_dash_packages = builtins.tuple(packages)
    return command_argv, pre_dash_packages
