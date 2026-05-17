"""APM install command and dependency installation engine."""

import builtins
import contextlib
import dataclasses
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

if TYPE_CHECKING:
    from apm_cli.install.plan import UpdatePlan

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan  # noqa: F401
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,  # noqa: F401
    _format_insecure_dependency_requirements,
    _format_insecure_dependency_warning,  # noqa: F401
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,  # noqa: F401
    _InsecureDependencyInfo,  # noqa: F401
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.mcp.entry import _build_mcp_entry  # noqa: F401
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml  # noqa: F401
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    dependency_reference_to_yaml_entry,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
    resolve_parsed_dependency_reference,
    user_scope_rejection_reason,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
from apm_cli.install.phases.local_content import (
    _copy_local_package,  # noqa: F401
    _has_local_apm_content,  # noqa: F401
    _project_has_root_primitives,
)

# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed  # noqa: F401

# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
from apm_cli.install.services import (
    _integrate_local_content,  # noqa: F401
    _integrate_package_primitives,  # noqa: F401
)

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,  # noqa: F401
    _validate_package_exists,
)
from apm_cli.utils.diagnostics import DiagnosticCollector  # noqa: F401

from ...constants import (
    APM_YML_FILENAME,
    InstallMode,
)
from ...core.auth import AuthResolver
from ...core.command_logger import InstallLogger, _ValidationOutcome
from ...core.target_detection import TargetParamType

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ...install.mcp.command import run_mcp_install as _run_mcp_install
from ...install.mcp.conflicts import (
    validate_mcp_conflicts as _validate_mcp_conflicts,
)
from ...install.mcp.registry import (
    resolve_registry_url as _resolve_registry_url,
)
from ...install.mcp.registry import (
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,
)
from ...install.mcp.registry import (
    validate_registry_url as _validate_registry_url,
)
from ...utils.console import _rich_echo, _rich_error, _rich_info, _rich_success  # noqa: F401
from .._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _update_gitignore_for_apm_modules,  # noqa: F401
)

# ---------------------------------------------------------------------------
# Manifest snapshot + rollback (W2-pkg-rollback, #827)
# ---------------------------------------------------------------------------
# When the user runs ``apm install <pkg>``, ``_validate_and_add_packages_to_apm_yml``
# mutates ``apm.yml`` BEFORE the install pipeline runs.  If the pipeline fails
# (policy block, download error, etc.) the failed package would stay in
# ``apm.yml`` forever.  These helpers snapshot the raw bytes before mutation
# and atomically restore on failure.
# ---------------------------------------------------------------------------


# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict

# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ...deps.apm_resolver import APMDependencyResolver
    from ...deps.github_downloader import GitHubPackageDownloader  # noqa: F401
    from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ...integration import AgentIntegrator, PromptIntegrator  # noqa: F401
    from ...integration.mcp_integrator import MCPIntegrator
    from ...models.apm_package import APMPackage, DependencyReference

    class _ScopedInstallDependencyResolver(APMDependencyResolver):
        """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

        def __init__(self, *args, install_scope=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._install_scope = install_scope

        def expand_parent_repo_decl(self, parent_dep, child_dep):
            from ...core.scope import InstallScope

            if self._install_scope is InstallScope.USER:
                raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
            return super().expand_parent_repo_decl(parent_dep, child_dep)

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)
    _ScopedInstallDependencyResolver = None  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Package validation helpers (extracted from _validate_and_add_packages_to_apm_yml)

from ._command_context import InstallContext
from .argv_split import _get_invocation_argv, _split_argv_at_double_dash


@click.command(
    help="Install APM and MCP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json); auto-creates apm.yml; use --allow-insecure for http:// packages)"
)
@click.argument("packages", nargs=-1)
@click.option(
    "--runtime",
    help=(
        "Target specific runtime only (copilot, codex, vscode, cursor, opencode, gemini, claude, windsurf)"
    ),
)
@click.option("--exclude", help="Exclude specific runtime from installation")
@click.option(
    "--only",
    type=click.Choice(["apm", "mcp"]),
    help="Install only specific dependency type",
)
@click.option(
    "--update",
    is_flag=True,
    help="Update dependencies to latest Git references (deprecated: prefer 'apm update' for an interactive plan, or 'apm update --yes' for CI)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be installed without installing")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-authored files on collision and deploy despite critical security findings (does NOT refresh refs; use 'apm update' for that)",
)
@click.option(
    "--frozen",
    is_flag=True,
    help="Refuse to install when apm.lock.yaml is missing or out of sync with apm.yml (CI-safe; mutually exclusive with --update). Structural presence check only; use 'apm audit' for on-disk integrity.",
)
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
    type=TargetParamType(),
    default=None,
    help="Target harness(es) to deploy to. Comma-separated for multiple: --target claude,cursor. Highest-priority entry in the resolution chain (--target > apm.yml targets: > auto-detect). Values: copilot, claude, cursor, opencode, codex, gemini, windsurf, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf (excludes agent-skills); combine with 'agent-skills' for both. 'copilot-cowork' is also accepted when the copilot-cowork experimental flag is enabled (run 'apm experimental enable copilot-cowork'). Note: '--target all' on 'apm compile' is deprecated; use 'apm compile --all' instead.",
)
@click.option(
    "--allow-insecure",
    "allow_insecure",
    is_flag=True,
    default=False,
    help="Allow HTTP (insecure) dependencies. Required when dependencies use http:// URLs.",
)
@click.option(
    "--allow-insecure-host",
    "allow_insecure_hosts",
    multiple=True,
    callback=_allow_insecure_host_callback,
    metavar="HOSTNAME",
    help="Allow transitive HTTP (insecure) dependencies from this hostname. Repeat for multiple hosts.",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Install to user scope (~/.apm/) instead of the current project. MCP servers target global-capable runtimes only (Copilot CLI, Codex CLI).",
)
@click.option(
    "--ssh",
    "use_ssh",
    is_flag=True,
    default=False,
    help="Prefer SSH transport for shorthand (owner/repo) dependencies. Mutually exclusive with --https.",
)
@click.option(
    "--https",
    "use_https",
    is_flag=True,
    default=False,
    help="Prefer HTTPS transport for shorthand (owner/repo) dependencies. Mutually exclusive with --ssh.",
)
@click.option(
    "--allow-protocol-fallback",
    "allow_protocol_fallback",
    is_flag=True,
    default=False,
    help="Restore the legacy permissive cross-protocol fallback chain (escape hatch for migrating users; also: APM_ALLOW_PROTOCOL_FALLBACK=1). Caveat: fallback reuses the same port across schemes; on servers that use different SSH and HTTPS ports, omit this flag and pin the dependency with an explicit ssh:// or https:// URL.",
)
@click.option(
    "--mcp",
    "mcp_name",
    default=None,
    metavar="NAME",
    help=(
        "Add an MCP server entry to apm.yml. Use with --transport, --url, --env, "
        "--header, --mcp-version, or a stdio command after `--`. Resolves active "
        "targets the same way `apm install` does (--target > apm.yml targets: > "
        "auto-detect); writes only for active targets, skips others with [i]."
    ),
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "sse", "streamable-http"]),
    default=None,
    help="MCP transport (stdio, http, sse, streamable-http). Inferred from --url or post-- command when omitted (requires --mcp).",
)
@click.option(
    "--url",
    "url",
    default=None,
    help="MCP server URL for http/sse/streamable-http transports (requires --mcp).",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Environment variable for stdio MCP, repeatable (requires --mcp).",
)
@click.option(
    "--header",
    "header_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="HTTP header for remote MCP, repeatable (requires --mcp and --url).",
)
@click.option(
    "--mcp-version",
    "mcp_version",
    default=None,
    help="Pin MCP registry entry to a specific version (requires --mcp).",
)
@click.option(
    "--registry",
    "registry_url",
    default=None,
    metavar="URL",
    help=(
        "MCP registry URL (http:// or https://) for resolving --mcp NAME. "
        "Overrides the MCP_REGISTRY_URL env var. Default: "
        "https://api.mcp.github.com. Captured in apm.yml on the entry's "
        "'registry:' field for auditability. Not valid with --url "
        "or a stdio command (self-defined entries)."
    ),
)
@click.option(
    "--skill",
    "skill_names",
    multiple=True,
    metavar="NAME",
    help="Install only named skill(s) from a SKILL_BUNDLE. Repeatable. Persisted in apm.yml and apm.lock so bare 'apm install' is deterministic. Use --skill '*' to reset to all skills.",
)
@click.option(
    "--no-policy",
    "no_policy",
    is_flag=True,
    default=False,
    help="Skip org policy enforcement for this invocation. Does NOT bypass apm audit --ci.",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Bypass the persistent cache and re-fetch all dependencies from upstream.",
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Compatibility flag for projects that "
        "need per-client skill layouts."
    ),
)
@click.option(
    "--as",
    "alias",
    default=None,
    metavar="ALIAS",
    help=(
        "Override the log/display label when installing a local bundle "
        "(directory or .tar.gz produced by 'apm pack'). Only valid for "
        "local-bundle installs; passing --as without a local bundle path is rejected."
    ),
)
@click.pass_context
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

        # ----------------------------------------------------------------
        # Local-bundle early-exit (issue #1098).  When the sole positional
        # argument is a filesystem path that detect_local_bundle() recognises
        # as an APM-pack bundle, we skip the dependency-resolution pipeline
        # entirely and deploy the bundle's files directly.  Local bundles
        # are imperative deploys -- they do NOT mutate apm.yml.
        # ----------------------------------------------------------------
        if len(packages) == 1 and not mcp_name and (_probe := Path(packages[0])).exists():
            from ...bundle.local_bundle import detect_local_bundle as _detect_lb
            from ...install.local_bundle_handler import install_local_bundle as _install_lb

            _bundle_info = _detect_lb(_probe)
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
                    # Rejected-flag context for consolidated UsageError:
                    rejected_flags={
                        "--update": update,
                        "--only": only,
                        "--runtime": runtime,
                        "--exclude": exclude,
                        "--dev": dev,
                        "--ssh": use_ssh,
                        "--https": use_https,
                        "--allow-protocol-fallback": allow_protocol_fallback,
                        "--mcp": mcp_name,
                        "--registry": registry_url,
                        "--skill": bool(skill_names),
                        "--parallel-downloads": parallel_downloads != 4,
                        "--allow-insecure": allow_insecure,
                        "--allow-insecure-host": bool(allow_insecure_hosts),
                        "--no-policy": no_policy,
                    },
                )
                # Local bundle install renders its own summary; mark
                # ``summary_rendered = True`` so the finally-block (line ~1423)
                # does not emit a misleading "install interrupted" line on the
                # success path.  See issue #1207 D3.
                summary_rendered = True
                return
            # IM7: path exists but isn't a recognised bundle.  For tarball
            # extensions (.tar.gz / .tgz) the user clearly meant a bundle
            # artifact, so raise a targeted UsageError instead of falling
            # through to the registry path (which would try to clone).
            # For bare directories we still fall through, because
            # ``apm install ./packages/source-pkg`` is a supported local-path
            # install that goes through the dependency-resolver pipeline.
            _suffix = _probe.name.lower()
            if _probe.is_file() and (_suffix.endswith(".tar.gz") or _suffix.endswith(".tgz")):
                # Distinguish legacy --format apm bundles (apm.lock.yaml
                # present, plugin.json absent) from arbitrary tarballs so
                # the error message guides the user to the right next step.
                from ...bundle.local_bundle import _looks_like_legacy_apm_bundle

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
        # IM8: --as is only meaningful for local-bundle installs.  If we get
        # here, no local bundle was detected, so reject --as instead of
        # silently ignoring it.
        if alias:
            raise click.UsageError(
                "--as requires a local bundle path (directory or .tar.gz "
                "produced by 'apm pack'). It has no effect on registry installs."
            )
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

        # Resolve transport selection inputs.
        from ...deps.transport_selection import (
            ProtocolPreference,
            is_fallback_allowed,
            protocol_pref_from_env,
        )

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

        # Resolve scope
        from ...core.scope import (
            InstallScope,
            ensure_user_dirs,
            get_apm_dir,
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
        from ...core.scope import get_deploy_root

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
                dry_run,
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
