"""APM install command and dependency installation engine."""

import builtins  # noqa: I001
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
from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand  # noqa: F401

# Re-export _pre_deploy_security_scan for bare-name call sites + test imports.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan  # noqa: F401
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,  # noqa: F401 -- RULE B: apm_packages.py uses _m._check_insecure_dependencies
    _collect_insecure_dependency_infos,  # noqa: F401 -- test_architecture_invariants checks importability
    _format_insecure_dependency_warning,  # noqa: F401 -- test_architecture_invariants checks importability
    _guard_transitive_insecure_dependencies,  # noqa: F401 -- test_architecture_invariants checks importability
    _InsecureDependencyInfo,  # noqa: F401 -- test_architecture_invariants checks importability
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy names.
from apm_cli.install.mcp.entry import _build_mcp_entry  # noqa: F401
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml  # noqa: F401
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    persist_dependency_list_if_changed,  # noqa: F401 -- RULE A: re-exported for @patch('apm_cli.commands.install.*')
    resolve_parsed_dependency_reference,  # noqa: F401 -- RULE A: re-exported for @patch('apm_cli.commands.install.*')
    update_existing_dependency_entry_if_needed,  # noqa: F401 -- RULE A: re-exported for @patch('apm_cli.commands.install.*')
    user_scope_rejection_reason,  # noqa: F401 -- RULE A: re-exported for @patch('apm_cli.commands.install.*')
)
from apm_cli.install.package_selection import only_packages_from_validation

# Re-export helpers for @patch compatibility and test importability checks.
from apm_cli.install.phases.local_content import (
    _copy_local_package,  # noqa: F401
    _has_local_apm_content,  # noqa: F401
    _project_has_root_primitives,  # noqa: F401 -- RULE B: apm_packages.py uses _m._project_has_root_primitives
)
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed  # noqa: F401
from apm_cli.install.services import (
    _integrate_local_content,  # noqa: F401
    _integrate_package_primitives,  # noqa: F401
)
from apm_cli.install.validation import (
    _local_path_failure_reason,  # noqa: F401 -- RULE B: pkg_resolution.py uses _m._local_path_failure_reason... wait, no it doesn't. Keep for @patch compat.
    _local_path_no_markers_hint,  # noqa: F401
    _validate_package_exists,  # noqa: F401 -- RULE B: pkg_resolution.py uses _m._validate_package_exists
)
from apm_cli.utils.diagnostics import DiagnosticCollector  # noqa: F401

# Re-export manifest rollback helpers so @patch targets keep working.
from apm_cli.install.manifest_rollback import (  # noqa: F401
    _maybe_rollback_manifest,
    _restore_manifest_from_snapshot,
)

# Re-export InstallContext so @patch and imports in tests keep working.
from apm_cli.install.cli_context import InstallContext

# Re-export pkg resolution helpers so @patch('apm_cli.commands.install.*') works.
from apm_cli.install.pkg_resolution import (  # noqa: F401
    _check_package_conflicts,
    _merge_packages_into_yml,
    _resolve_package_references,
    _validate_and_add_packages_to_apm_yml,
)

# install() sub-module re-exports kept here for @patch compatibility.
from apm_cli.install.apm_packages import (  # noqa: F401
    _install_apm_dependencies,
    _install_apm_packages,
    _post_install_summary,
)
from apm_cli.install.install_cmd_phases import (
    _compute_argv_pre_dash,
    _execute_install_and_summary,
    _resolve_protocol_and_fallback,
    _resolve_scope_and_paths,
    _setup_auth_and_check_manifest,
    _try_local_bundle_install,
)
from apm_cli.install.mcp_handler import _McpConnectionParams, _handle_mcp_install

from ..constants import InstallMode
from ..core.auth import AuthResolver  # noqa: F401 -- RULE B: install_cmd_phases.py uses _m.AuthResolver
from ..core.command_logger import InstallLogger, _ValidationOutcome  # noqa: F401
from ..core.target_detection import TargetParamType

# MCP helpers and console utilities.
from ..install.mcp.command import run_mcp_install as _run_mcp_install  # noqa: F401
from ..install.mcp.conflicts import validate_mcp_conflicts as _validate_mcp_conflicts
from ..install.mcp.registry import (
    resolve_registry_url as _resolve_registry_url,  # noqa: F401
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,  # noqa: F401
    validate_registry_url as _validate_registry_url,
)
from ..install.mcp.spec import MCPRequestSpec as _MCPRequestSpec
from ..utils.console import (  # noqa: F401
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
)
from ._helpers import _get_invocation_argv, _split_argv_at_double_dash  # noqa: F401 -- patched by tests


# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict


# ---------------------------------------------------------------------------
# Argv ``--`` boundary helpers (W3 --mcp flag)
# Test seams; _split_argv_at_double_dash separates pre-``--`` packages from
# the post-``--`` stdio-command argv.
# ---------------------------------------------------------------------------


# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ..deps.apm_resolver import APMDependencyResolver  # noqa: I001
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed  # noqa: F401 -- RULE B via apm_packages.py
    from ..integration.mcp_integrator import MCPIntegrator  # noqa: F401
    from ..models.apm_package import APMPackage, DependencyReference  # noqa: F401 -- RULE B

    class _ScopedInstallDependencyResolver(APMDependencyResolver):
        """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

        def __init__(self, *args, install_scope=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._install_scope = install_scope

        def expand_parent_repo_decl(self, parent_dep, child_dep):
            from ..core.scope import InstallScope

            if self._install_scope is InstallScope.USER:
                raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
            return super().expand_parent_repo_decl(parent_dep, child_dep)

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)
    _ScopedInstallDependencyResolver = None  # type: ignore[misc,assignment]


@click.command(
    help="Install APM and MCP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json); auto-creates apm.yml; use --allow-insecure for http:// packages)"
)
@click.argument("packages", nargs=-1)
@click.option(
    "--runtime",
    help=(
        "Target specific runtime only (copilot, claude, codex, cursor, gemini, antigravity, intellij, kiro, opencode, vscode, windsurf)"
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
    help="Update dependencies to latest Git references (deprecated: prefer 'apm update' for an interactive plan, or 'apm update --yes' for CI). Unlike --refresh, --update restructures the entire dependency graph.",
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
    "--trust-canvas-extensions",
    is_flag=True,
    help="[experimental] Deploy canvas extensions provided by dependencies. Canvas extensions are executable Node code and are blocked by default; this flag opts in. With --global the canvas deploys to ~/.copilot/extensions and the flag is always required. Requires the 'canvas' experimental feature.",
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
    help="Target harness(es) to deploy to. Comma-separated for multiple: --target claude,cursor. Repeating the flag (e.g. '-t a -t b') is NOT supported -- only the last value wins; use commas. Highest-priority entry in the resolution chain (--target > apm.yml targets: > auto-detect). Values: copilot, claude, cursor, opencode, codex, gemini, antigravity, windsurf, kiro, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'antigravity' (alias 'agy') deploys to .agents/ (AGENTS.md + rules + skills + hooks.json + mcp_config.json) and is explicit-only -- not part of 'all' or auto-detection. 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf+kiro (excludes agent-skills and antigravity); combine with 'agent-skills' or 'antigravity' to add them. 'copilot-cowork' is also accepted when the copilot-cowork experimental flag is enabled (run 'apm experimental enable copilot-cowork'). 'copilot-app' is also accepted when the copilot-app experimental flag is enabled (run 'apm experimental enable copilot-app'). Note: '--target all' on 'apm compile' is deprecated; use 'apm compile --all' instead.",
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
    help="Install to user scope (~/.apm/) instead of the current project. MCP servers target global-capable runtimes only (Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot).",
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
    "--audit",
    "audit_mode",
    type=click.Choice(["off", "warn", "block"], case_sensitive=False),
    default=None,
    help=(
        "Run 'apm audit' over deployed files during install: off, warn, or block. "
        "Overrides config/policy. Requires 'apm experimental enable external-scanners'. "
        "An org policy 'block' cannot be relaxed below by this flag."
    ),
)
@click.option(
    "--no-audit",
    "no_audit",
    is_flag=True,
    default=False,
    help="Disable the install-time audit for this invocation (equivalent to --audit off).",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Re-fetch all dependencies from upstream and re-resolve all ref pins. Use 'apm update' for interactive upgrade planning.",
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
        "(directory, .zip, or .tar.gz produced by 'apm pack'). Only valid for "
        "local-bundle installs; passing --as without a local bundle path is rejected."
    ),
)
@click.option(
    "--root",
    "root",
    type=click.Path(file_okay=False, resolve_path=True),
    default=None,
    metavar="DIR",
    help=(
        "Install into DIR instead of $PWD: apm_modules/, apm.lock.yaml, "
        ".claude/, .codex/, .agents/, .opencode/ are written under DIR "
        "while sources (apm.yml, .apm/, local-path packages) continue "
        "resolving from $PWD. Mirrors 'pip install --target' / "
        "'npm install --prefix'. Project scope only; not valid with --global."
    ),
)
@click.pass_context
def install(  # noqa: PLR0913
    ctx,
    packages,
    runtime,
    exclude,
    only,
    update,
    dry_run,
    force,
    frozen,
    verbose,
    trust_transitive_mcp,
    trust_canvas_extensions,
    parallel_downloads,
    dev,
    target,
    allow_insecure,
    allow_insecure_hosts,
    global_,
    use_ssh,
    use_https,
    allow_protocol_fallback,
    mcp_name,
    transport,
    url,
    env_pairs,
    header_pairs,
    mcp_version,
    registry_url,
    skill_names,
    no_policy,
    audit_mode,
    no_audit,
    refresh,
    legacy_skill_paths,
    alias,
    root,
):
    """Install APM and MCP dependencies from apm.yml (like npm install).

    Detects AI runtimes from your apm.yml scripts and installs MCP servers for
    all detected runtimes; also installs APM package dependencies from GitHub.
    --only filters by type (apm or mcp).

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1#1.0.0              # Add package to apm.yml and install
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
        apm install ./my-bundle.zip             # Deploy a local bundle (archive)
        apm install ./my-bundle.tar.gz          # Deploy a local bundle (legacy archive)
        apm install ./bundle --as custom-name   # Local bundle with custom log label

    Environment variables:
        APM_PROGRESS    Animated install UI: auto (default; TTY only,
                        off in CI), always (force on -- never set in CI),
                        never (disable; also implied for non-TTY stdout).
    """
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
    # --root: entered manually so the existing try/finally handles __exit__.
    if root and global_:
        raise click.UsageError("--root is not valid with --global (user scope)")
    from ..core.install_audit import resolve_audit_override_from_cli
    from ..install.root_redirect import install_root_redirect

    try:
        audit_override = resolve_audit_override_from_cli(no_audit, audit_mode)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    _root_redirect = install_root_redirect(root, dry_run=dry_run)
    _root_redirect.__enter__()
    try:
        # Create structured logger for install output early so exception
        # handlers can always reference it (avoids UnboundLocalError if
        # scope initialisation below throws).
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)

        # W2-pkg-rollback (#827): init snapshot vars before any branch that might
        # raise, so except handlers can always reference them (no UnboundLocalError).
        _manifest_snapshot: bytes | None = None
        _snapshot_manifest_path: Path | None = None

        # Resolve --legacy-skill-paths: CLI flag wins, then env var fallback.
        if not legacy_skill_paths:
            from ..integration.targets import should_use_legacy_skill_paths

            legacy_skill_paths = should_use_legacy_skill_paths()

        # ----------------------------------------------------------------
        # Local-bundle early-exit (issue #1098).  When the sole positional
        # argument is a filesystem path that detect_local_bundle() recognises
        # as an APM-pack bundle, we skip the dependency-resolution pipeline
        # entirely and deploy the bundle's files directly.  Local bundles
        # are imperative deploys -- they do NOT mutate apm.yml.  The detection
        # + dispatch lives in _try_local_bundle_install(); it returns True when
        # it fully handled the install (caller must return).
        # ----------------------------------------------------------------
        if _try_local_bundle_install(
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
            trust_canvas_extensions,
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
        ):
            # Local bundle install renders its own summary; mark
            # ``summary_rendered = True`` so the finally-block (line ~1423)
            # does not emit a misleading "install interrupted" line on the
            # success path.  See issue #1207 D3.
            summary_rendered = True
            return
        # IM8: --as is only meaningful for local-bundle installs.  If we get
        # here, no local bundle was detected, so reject --as instead of
        # silently ignoring it.
        if alias:
            raise click.UsageError(
                "--as requires a local bundle path (directory, .zip, or .tar.gz "
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
        # --mcp branch (W3): compute argv split; route to MCP path if set.
        # ----------------------------------------------------------------
        command_argv, pre_dash_packages = _compute_argv_pre_dash(packages)

        # Validate --registry (raises UsageError on a bad URL).
        validated_registry_url = _validate_registry_url(registry_url)

        _validate_mcp_conflicts(
            spec=_MCPRequestSpec(
                mcp_name=mcp_name,
                transport=transport,
                url=url,
                mcp_version=mcp_version,
                command_argv=command_argv,
                registry_url=validated_registry_url,
            ),
            packages=packages,
            pre_dash_packages=pre_dash_packages,
            env=env_pairs,
            headers=header_pairs,
            global_=global_,
            only=only,
            update=update,
            any_transport_flag=use_ssh or use_https or allow_protocol_fallback,
        )

        # Normalize --skill: '*' means all (same as absent). Reject with --mcp.
        _skill_subset = None
        if skill_names:
            if mcp_name is not None:
                raise click.UsageError("--skill cannot be combined with --mcp.")
            if not any(s == "*" for s in skill_names):
                _skill_subset = builtins.tuple(skill_names)

        if mcp_name is not None:
            _handle_mcp_install(
                mcp_name=mcp_name,
                mcp_conn=_McpConnectionParams(
                    transport=transport,
                    url=url,
                    env_pairs=env_pairs,
                    header_pairs=header_pairs,
                    mcp_version=mcp_version,
                ),
                command_argv=command_argv,
                dev=dev,
                force=force,
                runtime=runtime,
                exclude=exclude,
                verbose=verbose,
                logger=logger,
                no_policy=no_policy,
                validated_registry_url=validated_registry_url,
            )
            return

        # Resolve transport preference, scope, paths, and auth resolver.
        protocol_pref, allow_protocol_fallback = _resolve_protocol_and_fallback(
            use_ssh, use_https, allow_protocol_fallback
        )
        if protocol_pref is None:
            _rich_error("Options --ssh and --https are mutually exclusive.", symbol="error")
            sys.exit(2)

        scope, manifest_path, apm_dir, manifest_display, project_root = _resolve_scope_and_paths(
            global_, logger
        )

        auth_resolver = _setup_auth_and_check_manifest(
            scope, packages, manifest_path, manifest_display, logger
        )

        # If packages are specified, validate and add them to apm.yml first
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

            _validated_packages, outcome = _validate_and_add_packages_to_apm_yml(
                packages,
                dry_run,
                dev=dev,
                logger=logger,
                manifest_path=manifest_path,
                auth_resolver=auth_resolver,
                scope=scope,
                allow_insecure=allow_insecure,
                skill_subset=_skill_subset,
            )
            # Short-circuit: all packages failed validation -- nothing to install
            if outcome.all_failed:
                return
            # Note: Empty validated_packages is OK if packages are already in apm.yml;
            # only_packages is derived from validation outcomes below.

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
            trust_canvas=trust_canvas_extensions,
            no_policy=no_policy,
            audit_override=audit_override,
            install_mode=InstallMode(only) if only else InstallMode.ALL,
            packages=packages,
            refresh=refresh,
            only_packages=only_packages_from_validation(packages, outcome),
            manifest_snapshot=_manifest_snapshot,
            snapshot_manifest_path=_snapshot_manifest_path,
            legacy_skill_paths=legacy_skill_paths,
            frozen=frozen,
            plan_callback=None,
            skill_subset=_skill_subset,
            skill_subset_from_cli=bool(skill_names),
        )

        _execute_install_and_summary(install_ctx, outcome, frozen, install_started_at)
        summary_rendered = True

    except InsecureDependencyPolicyError:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        sys.exit(1)
    except AuthenticationError as e:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        _rich_info("Tip: run 'apm doctor' to diagnose auth and connectivity.", symbol="info")
        sys.exit(1)
    except DirectDependencyError as e:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        logger.error(str(e))
        sys.exit(1)
    except click.UsageError:
        # Conflict matrix / argv parser raises UsageError -- let Click
        # render with exit code 2 and the standard "Usage: ..." prefix.
        raise
    except Exception as e:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        if logger:
            logger.error(f"Error installing dependencies: {e}")
            if not verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
        else:
            _rich_error(f"Error installing dependencies: {e}")
        sys.exit(1)
    finally:
        # --root: restore cwd + clear the source-root override regardless
        # of how the handler exits (return, sys.exit -> SystemExit,
        # exception). Done first so cwd is back to $PWD before any
        # best-effort summary rendering below.
        _root_redirect.__exit__(None, None, None)
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
