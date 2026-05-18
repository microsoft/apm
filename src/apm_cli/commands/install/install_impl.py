"""APM install command and dependency installation engine."""

import builtins
import contextlib
import os
import sys
import time
from pathlib import Path

import click

from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,
)
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,  # noqa: F401
)

from ...constants import (
    InstallMode,
)
from ...core.command_logger import InstallLogger
from ...install.mcp.conflicts import (
    validate_mcp_conflicts as _validate_mcp_conflicts,
)
from ...install.mcp.registry import (
    validate_registry_url as _validate_registry_url,
)
from ...utils.console import _rich_echo, _rich_error, _rich_info  # noqa: F401
from ._local_bundle_router import _route_local_bundle_or_raise

set = builtins.set
list = builtins.list
dict = builtins.dict
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
from ._command_context import InstallContext


def install(ctx: click.Context, **params: object) -> None:
    """Install APM and MCP dependencies from apm.yml (like npm install).

    Detects AI runtimes from your apm.yml scripts and installs MCP servers for
    all detected runtimes; also installs APM package dependencies from GitHub.
    --only filters by type (apm or mcp).

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1                    # Add package to apm.yml and install
        apm install --exclude codex             # Install for all except Codex CLI
        apm install --only=apm                  # Install only APM dependencies
        apm install --update                    # Update dependencies to latest Git refs
        apm install --dry-run                   # Show what would be installed
        apm install -g org/pkg1                 # Install to user scope (~/.apm/)
        apm install --allow-insecure http://...  # HTTP URL (needs allow_insecure)
        apm install --skill my-skill org/bundle  # Install one skill from bundle
        apm install --mcp io.github.github/github-mcp-server   # MCP registry
        apm install --mcp api --url https://example.com/mcp    # MCP remote
        apm install --mcp fetch -- npx -y @mcp/server-fetch    # MCP stdio
        apm install ./build/my-bundle           # Deploy a local bundle (directory)
        apm install ./my-bundle.tar.gz          # Deploy a local bundle (archive)
        apm install ./bundle --as custom-name   # Local bundle with custom log label

    Environment variables:
        APM_PROGRESS    Animated install UI: auto (default; TTY only,
                        off in CI), always (force on -- never set in CI),
                        never (disable; also implied for non-TTY stdout).
    """

    packages = params["packages"]
    runtime = params["runtime"]
    exclude = params["exclude"]
    only = params["only"]
    update = params["update"]
    dry_run = params["dry_run"]
    force = params["force"]
    frozen = params["frozen"]
    verbose = params["verbose"]
    trust_transitive_mcp = params["trust_transitive_mcp"]
    parallel_downloads = params["parallel_downloads"]
    dev = params["dev"]
    target = params["target"]
    allow_insecure = params["allow_insecure"]
    allow_insecure_hosts = params["allow_insecure_hosts"]
    global_ = params["global_"]
    use_ssh = params["use_ssh"]
    use_https = params["use_https"]
    allow_protocol_fallback = params["allow_protocol_fallback"]
    mcp_name = params["mcp_name"]
    transport = params["transport"]
    url = params["url"]
    env_pairs = params["env_pairs"]
    header_pairs = params["header_pairs"]
    mcp_version = params["mcp_version"]
    registry_url = params["registry_url"]
    skill_names = params["skill_names"]
    no_policy = params["no_policy"]
    refresh = params["refresh"]
    legacy_skill_paths = params["legacy_skill_paths"]
    alias = params["alias"]
    install_api = sys.modules[__package__]
    # C1 #856: defaults BEFORE try so the finally clause never sees an
    # UnboundLocalError if InstallLogger(...) raises during construction.
    _apm_verbose_prev = os.environ.get("APM_VERBOSE")
    # F5 (#1116): elapsed wall time covers EVERY exit path. Captured
    # before logger construction so `finally` can render a timing line
    # even if logger init itself raised.
    install_started_at = time.perf_counter()
    summary_rendered = False
    logger = None
    if frozen and update:
        raise click.UsageError(
            "--frozen and --update are mutually exclusive. "
            "Use 'apm update' to refresh refs, then 'apm install --frozen' in CI."
        )
    try:
        # Create structured logger for install output early so exception
        # handlers can always reference it (avoids UnboundLocalError if
        # scope initialisation below throws).
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)

        # W2-pkg-rollback (#827): snapshot bytes captured BEFORE
        # _validate_and_add_packages_to_apm_yml mutates apm.yml. Initialised
        # to None here -- BEFORE any branch that might raise (e.g. the local
        # bundle early-exit path below) -- so the `except` handlers at the
        # bottom of this function can always reference both names without
        # UnboundLocalError. The bug this prevents: an exception raised in
        # the local-bundle branch (e.g. a click.Abort from integrity-verify
        # failure on Windows) would otherwise be masked by an
        # UnboundLocalError inside the handler that calls
        # install_api._maybe_rollback_manifest(_snapshot_manifest_path, ...).
        _manifest_snapshot: bytes | None = None
        _snapshot_manifest_path: Path | None = None

        # Resolve --legacy-skill-paths: CLI flag wins, then env var fallback.
        if not legacy_skill_paths:
            from ...integration.targets import should_use_legacy_skill_paths

            legacy_skill_paths = should_use_legacy_skill_paths()

        # Local-bundle early-exit (issue #1098) and --as validation (IM8).
        # Delegates to _route_local_bundle_or_raise in _local_bundle_router.py.
        # Returns True if a bundle was detected + installed; raises UsageError
        # for unrecognised tarballs or --as without a bundle path.
        if _route_local_bundle_or_raise(
            packages=packages,
            mcp_name=mcp_name,
            target=target,
            global_=global_,
            force=force,
            dry_run=dry_run,
            verbose=verbose,
            alias=alias,
            logger=logger,
            legacy_skill_paths=legacy_skill_paths,
            update=update,
            only=only,
            runtime=runtime,
            exclude=exclude,
            dev=dev,
            use_ssh=use_ssh,
            use_https=use_https,
            allow_protocol_fallback=allow_protocol_fallback,
            registry_url=registry_url,
            skill_names=skill_names,
            parallel_downloads=parallel_downloads,
            allow_insecure=allow_insecure,
            allow_insecure_hosts=allow_insecure_hosts,
            no_policy=no_policy,
        ):
            # Local bundle install renders its own summary; mark
            # summary_rendered so the finally-block does not emit a
            # misleading "install interrupted" line.  See issue #1207 D3.
            summary_rendered = True
            return
        # HACK(#852): surface --verbose to deeper auth layers via env var until
        # AuthResolver gains a first-class verbose channel. Restored in finally
        # below to keep the mutation scoped to this command invocation.
        if verbose:
            os.environ["APM_VERBOSE"] = "1"

        # W2-pkg-rollback (#827): snapshot bytes captured BEFORE
        # _validate_and_add_packages_to_apm_yml mutates apm.yml.
        # NOTE: variables are initialised at the top of the try block
        # (above the local-bundle early-exit) so exception handlers can
        # always reference them without UnboundLocalError.

        # ----------------------------------------------------------------
        # --mcp branch (W3): when --mcp is set, route to the dedicated
        # MCP-add path.  We compute the post-`--` argv here BEFORE Click's
        # silent handling: see _split_argv_at_double_dash().
        # ----------------------------------------------------------------
        install_pkg = sys.modules[__package__]
        _, command_argv = install_pkg._split_argv_at_double_dash(install_pkg._get_invocation_argv())
        # `packages` from Click already includes the post-`--` items; the
        # pre-`--` portion is what the user typed as positional packages.
        if command_argv:
            split_idx = len(packages) - len(command_argv)
            split_idx = max(split_idx, 0)
            pre_dash_packages = builtins.tuple(packages[:split_idx])
        else:
            pre_dash_packages = builtins.tuple(packages)

        # Validate --registry (raises UsageError on a bad URL).
        validated_registry_url = _validate_registry_url(registry_url)

        _validate_mcp_conflicts(
            mcp_name=mcp_name,
            packages=packages,
            pre_dash_packages=pre_dash_packages,
            transport=transport,
            url=url,
            env=env_pairs,
            headers=header_pairs,
            mcp_version=mcp_version,
            command_argv=command_argv,
            global_=global_,
            only=only,
            update=update,
            use_ssh=use_ssh,
            use_https=use_https,
            allow_protocol_fallback=allow_protocol_fallback,
            registry_url=validated_registry_url,
        )

        # Normalize --skill: '*' means all (same as absent). Reject with --mcp.
        _skill_subset = None
        if skill_names:
            if mcp_name is not None:
                raise click.UsageError("--skill cannot be combined with --mcp.")
            if not any(s == "*" for s in skill_names):
                _skill_subset = builtins.tuple(skill_names)

        if mcp_name is not None:
            install_api._handle_mcp_install(
                mcp_name=mcp_name,
                transport=transport,
                url=url,
                env_pairs=env_pairs,
                header_pairs=header_pairs,
                mcp_version=mcp_version,
                command_argv=command_argv,
                dev=dev,
                force=force,
                runtime=runtime,
                exclude=exclude,
                verbose=verbose,
                dry_run=dry_run,
                logger=logger,
                no_policy=no_policy,
                validated_registry_url=validated_registry_url,
            )
            return

        from ._install_setup import _resolve_protocol_pref, _setup_scope_and_paths  # noqa: E402

        protocol_pref, allow_protocol_fallback = _resolve_protocol_pref(
            use_ssh, use_https, allow_protocol_fallback
        )
        (
            scope,
            manifest_path,
            apm_dir,
            manifest_display,
            project_root,
            auth_resolver,
            apm_yml_exists,
        ) = _setup_scope_and_paths(global_, packages, logger)

        # If packages are specified, validate and add them to apm.yml first
        validated_packages = []
        outcome = None
        if packages:
            # -- W2-pkg-rollback (#827): snapshot raw bytes BEFORE mutation --
            # _validate_and_add_packages_to_apm_yml does a YAML round-trip
            # (load + dump) which may alter whitespace, key ordering, or
            # trailing newlines.  We snapshot the raw bytes so rollback is
            # byte-exact -- no YAML drift.
            if manifest_path.exists():
                _manifest_snapshot = manifest_path.read_bytes()
                _snapshot_manifest_path = manifest_path

            validated_packages, outcome = install_api._validate_and_add_packages_to_apm_yml(
                packages,
                dry_run=dry_run,
                dev=dev,
                logger=logger,
                manifest_path=manifest_path,
                auth_resolver=auth_resolver,
                scope=scope,
                allow_insecure=allow_insecure,
            )
            # Short-circuit: all packages failed validation -- nothing to install
            if outcome.all_failed:
                return
            # Note: Empty validated_packages is OK if packages are already in apm.yml
            # We'll proceed with installation from apm.yml to ensure everything is synced

        # Build install context
        install_ctx = InstallContext(
            scope=scope,
            manifest_path=manifest_path,
            manifest_display=manifest_display,
            apm_dir=apm_dir,
            project_root=project_root,
            logger=logger,
            auth_resolver=auth_resolver,
            verbose=verbose,
            force=force,
            dry_run=dry_run,
            update=update,
            dev=dev,
            runtime=runtime,
            exclude=exclude,
            target=target,
            parallel_downloads=parallel_downloads,
            allow_insecure=allow_insecure,
            allow_insecure_hosts=allow_insecure_hosts,
            protocol_pref=protocol_pref,
            allow_protocol_fallback=allow_protocol_fallback,
            trust_transitive_mcp=trust_transitive_mcp,
            no_policy=no_policy,
            install_mode=InstallMode(only) if only else InstallMode.ALL,
            packages=packages,
            refresh=refresh,
            only_packages=builtins.list(validated_packages) if packages else None,
            manifest_snapshot=_manifest_snapshot,
            snapshot_manifest_path=_snapshot_manifest_path,
            legacy_skill_paths=legacy_skill_paths,
            frozen=frozen,
            plan_callback=None,
        )

        apm_count, mcp_count, apm_diagnostics = install_api._install_apm_packages(
            install_ctx,
            outcome,
        )

        install_api._post_install_summary(
            logger=logger,
            apm_count=apm_count,
            mcp_count=mcp_count,
            apm_diagnostics=apm_diagnostics,
            force=force,
            elapsed_seconds=time.perf_counter() - install_started_at,
        )
        summary_rendered = True

        if frozen and apm_count > 0:
            # --frozen verifies LOCKFILE STRUCTURE (every apm.yml dep
            # has a lock entry), not on-disk content integrity. Make
            # the scope explicit so a CI pipeline that skips
            # 'apm audit' on the assumption that --frozen covers SHA
            # verification is corrected at the moment of use.
            _rich_info(
                "Lockfile presence verified. Run 'apm audit' for on-disk content integrity.",
                symbol="info",
            )

    except InsecureDependencyPolicyError:
        install_api._maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        sys.exit(1)
    except AuthenticationError as e:
        install_api._maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        sys.exit(1)
    except DirectDependencyError as e:
        install_api._maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        logger.error(str(e))
        sys.exit(1)
    except click.UsageError:
        # Conflict matrix / argv parser raises UsageError -- let Click
        # render with exit code 2 and the standard "Usage: ..." prefix.
        raise
    except Exception as e:
        install_api._maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        if logger:
            logger.error(f"Error installing dependencies: {e}")
            if not verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
        else:
            _rich_error(f"Error installing dependencies: {e}")
        sys.exit(1)
    finally:
        # F5 (#1116): render minimal elapsed-time line on exit paths that
        # did not already render the full install summary. Best-effort:
        # never let a render failure mask the original exception/exit.
        if not summary_rendered and logger is not None:
            with contextlib.suppress(Exception):
                logger.install_interrupted(elapsed_seconds=time.perf_counter() - install_started_at)
        # HACK(#852) cleanup: restore APM_VERBOSE so it stays scoped to this call.
        if _apm_verbose_prev is None:
            os.environ.pop("APM_VERBOSE", None)
        else:
            os.environ["APM_VERBOSE"] = _apm_verbose_prev
