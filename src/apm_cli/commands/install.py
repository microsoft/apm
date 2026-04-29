"""APM install command and dependency installation engine."""

import builtins
import dataclasses
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

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
from ..core.target_detection import TargetParamType
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning
from ..utils.diagnostics import DiagnosticCollector


# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed

from ..utils.path_security import safe_rmtree

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,
    _validate_package_exists,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
# _integrate_package_primitives and _integrate_local_content live in
# apm_cli.install.services (P1 -- DI seam).  Re-exports below preserve
# the existing import contract for tests and external callers.
from apm_cli.install.phases.local_content import (
    _copy_local_package,
    _has_local_apm_content,
    _project_has_root_primitives,
)
from apm_cli.install.errors import AuthenticationError, DirectDependencyError, PolicyViolationError
from apm_cli.install.insecure_policy import (
    _InsecureDependencyInfo,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,
    _format_insecure_dependency_warning,
    _format_insecure_dependency_requirements,
    _guard_transitive_insecure_dependencies,
    _get_insecure_dependency_url,
    _normalize_allow_insecure_host,
    _warn_insecure_dependencies,
    InsecureDependencyPolicyError,
)

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan

from ._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _rich_blank_line,
    _update_gitignore_for_apm_modules,
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

def _restore_manifest_from_snapshot(
    manifest_path: "Path",
    snapshot: bytes,
) -> None:
    """Atomically restore ``apm.yml`` from a raw-bytes snapshot.

    Uses temp-file + ``os.replace`` to avoid torn writes, mirroring the
    W1 cache atomic-write pattern (``discovery.py``).
    """
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix="apm-restore-", dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(snapshot)
        os.replace(tmp_name, str(manifest_path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _maybe_rollback_manifest(
    manifest_path: "Path",
    snapshot: "bytes | None",
    logger: "InstallLogger",
) -> None:
    """Restore ``apm.yml`` from *snapshot* if one was captured, then log.

    No-op when *snapshot* is ``None`` (i.e. the command was not
    ``apm install <pkg>`` or the manifest did not exist before mutation).
    """
    if snapshot is None:
        return
    try:
        _restore_manifest_from_snapshot(manifest_path, snapshot)
        logger.progress("apm.yml restored to its previous state.")
    except Exception:
        # Best-effort: if the restore itself fails, warn but don't mask
        # the original exception that triggered the rollback.
        logger.warning("Failed to restore apm.yml to its previous state.")

# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict


# ---------------------------------------------------------------------------
# InstallContext -- parameter bundle for the APM install pipeline
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class InstallContext:
    """Bundles install command state to reduce function signatures.

    Created by :func:`install` after argument parsing and scope resolution,
    then threaded through :func:`_install_apm_packages` and
    :func:`_post_install_summary` to avoid long parameter lists.
    """

    scope: Any  # InstallScope
    manifest_path: "Path"
    manifest_display: str
    apm_dir: "Path"
    project_root: "Path"
    logger: Any  # InstallLogger
    auth_resolver: Any  # AuthResolver
    verbose: bool
    force: bool
    dry_run: bool
    update: bool
    dev: bool
    runtime: Optional[str]
    exclude: Optional[str]
    target: Optional[str]
    parallel_downloads: int
    allow_insecure: bool
    allow_insecure_hosts: tuple
    protocol_pref: Any  # ProtocolPreference
    allow_protocol_fallback: bool
    trust_transitive_mcp: bool
    no_policy: bool
    install_mode: Any  # InstallMode
    packages: tuple  # Original Click packages
    only_packages: Optional[builtins.list] = None
    manifest_snapshot: Optional[bytes] = None
    snapshot_manifest_path: Optional["Path"] = None


# ---------------------------------------------------------------------------
# Argv `--` boundary helpers (W3 --mcp flag)
# ---------------------------------------------------------------------------
#
# Click's ``nargs=-1`` silently swallows the ``--`` separator and merges
# everything after it into the positional argument tuple.  For
# ``apm install --mcp foo -- npx -y srv`` we cannot distinguish that from
# ``apm install --mcp foo npx -y srv`` once Click is done parsing.
#
# We therefore inspect ``sys.argv`` ourselves to detect the boundary and
# extract the post-``--`` portion as the stdio command argv.  ``--`` IS
# present in ``sys.argv`` even though Click strips it from the parsed
# arguments.  The pre-``--`` portion is used to flag conflicts (E1).
#
# ``_get_invocation_argv`` exists as a tiny seam so tests using
# ``CliRunner`` (which does not modify ``sys.argv``) can patch it without
# resorting to ``monkeypatch.setattr('sys.argv', ...)``.


def _get_invocation_argv():
    """Return the process invocation argv. Wrapped for test injection."""
    return sys.argv


def _split_argv_at_double_dash(argv):
    """Return ``(clean_argv, command_argv_tuple)``.

    If ``--`` is not present, ``command_argv_tuple`` is ``()``.
    """
    if "--" not in argv:
        return argv, ()
    idx = argv.index("--")
    return argv[:idx], builtins.tuple(argv[idx + 1:])

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
# Package validation helpers (extracted from _validate_and_add_packages_to_apm_yml)
# ---------------------------------------------------------------------------


def _check_package_conflicts(current_deps):
    """Build identity set from existing deps for duplicate detection.

    Parses each entry in *current_deps* (string or dict form) through
    :class:`DependencyReference` and collects identity strings.

    Returns:
        ``set`` of identity strings for existing dependencies.
    """
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
    return existing_identities


def _resolve_package_references(
    packages,
    existing_identities,
    *,
    auth_resolver=None,
    logger=None,
    scope=None,
    allow_insecure=False,
):
    """Validate, canonicalize, and resolve package references.

    Handles marketplace refs, canonical parsing, insecure-URL guards,
    local-at-user-scope rejection, and accessibility checks.

    *existing_identities* is mutated (new identities are added to prevent
    duplicates within the same batch).

    Returns:
        Tuple of ``(valid_outcomes, invalid_outcomes, validated_packages,
        marketplace_provenance, apm_yml_entries)``.
    """
    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}
    _apm_yml_entries = {}  # canonical -> apm.yml entry (str or dict for HTTP deps)
    validated_packages = []

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
                plugin_name, marketplace_name, version_spec = mkt_ref
                try:
                    warning_handler = None
                    if logger:
                        warning_handler = lambda msg: logger.warning(msg)
                        logger.verbose_detail(
                            f"    Resolving {plugin_name}@{marketplace_name} via marketplace..."
                        )
                    canonical_str, resolved_plugin = resolve_marketplace_plugin(
                        plugin_name,
                        marketplace_name,
                        version_spec=version_spec,
                        auth_resolver=auth_resolver,
                        warning_handler=warning_handler,
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

        if dep_ref.is_insecure:
            if not allow_insecure:
                # The reason string embeds the full URL already, so skip
                # logger.validation_fail (which prepends "{package} -- ") to
                # avoid rendering the URL twice. Use logger.error directly.
                reason = _format_insecure_dependency_requirements(
                    _get_insecure_dependency_url(dep_ref)
                )
                invalid_outcomes.append((package, reason))
                if logger:
                    logger.error(reason)
                continue
            dep_ref.allow_insecure = True
            _apm_yml_entries[canonical] = dep_ref.to_apm_yml_entry()

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
        if _validate_package_exists(package, verbose=verbose, auth_resolver=auth_resolver, logger=logger):
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

    return (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
    )


def _merge_packages_into_yml(
    validated_packages,
    apm_yml_entries,
    current_deps,
    data,
    dep_section,
    apm_yml_path,
    *,
    dev=False,
    logger=None,
):
    """Append *validated_packages* to the dependency list and write apm.yml.

    Mutates *current_deps* in place and persists the updated manifest to
    *apm_yml_path*.
    """
    dep_label = "devDependencies" if dev else "apm.yml"
    for package in validated_packages:
        current_deps.append(apm_yml_entries.get(package, package))
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


def _validate_and_add_packages_to_apm_yml(packages, dry_run=False, dev=False, logger=None, manifest_path=None, auth_resolver=None, scope=None, allow_insecure=False):
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

    # Detect duplicates against existing deps
    existing_identities = _check_package_conflicts(current_deps)

    # Validate and canonicalize all package references
    (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
    ) = _resolve_package_references(
        packages,
        existing_identities,
        auth_resolver=auth_resolver,
        logger=logger,
        scope=scope,
        allow_insecure=allow_insecure,
    )

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

    # Persist validated packages to apm.yml
    _merge_packages_into_yml(
        validated_packages,
        _apm_yml_entries,
        current_deps,
        data,
        dep_section,
        apm_yml_path,
        dev=dev,
        logger=logger,
    )

    return validated_packages, outcome


# ---------------------------------------------------------------------------
# MCP CLI helpers (W3 --mcp flag)
# ---------------------------------------------------------------------------

# F7 / F5 install-time MCP warnings live in apm_cli/install/mcp_warnings.py
# per LOC budget. Re-bind module-level names for back-compat with tests
# that still patch ``apm_cli.commands.install._warn_*``.
from ..install.mcp_warnings import (
    warn_ssrf_url as _warn_ssrf_url,
    warn_shell_metachars as _warn_shell_metachars,
    _SHELL_METACHAR_TOKENS,
    _METADATA_HOSTS,
    _is_internal_or_metadata_host,
)

# --registry helpers live in apm_cli/install/mcp_registry.py per LOC budget.
from ..install.mcp_registry import (
    validate_registry_url as _validate_registry_url,
    resolve_registry_url as _resolve_registry_url,
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,
)


def _parse_kv_pairs(pairs, *, flag_name):
    """Parse a tuple of ``KEY=VALUE`` strings into a dict.

    Empty input returns ``{}``.  Raises :class:`click.UsageError` (exit
    code 2) on a missing ``=`` separator or empty key.
    """
    result: builtins.dict = {}
    for raw in pairs or ():
        if "=" not in raw:
            raise click.UsageError(
                f"Invalid {flag_name} '{raw}': expected KEY=VALUE"
            )
        key, _, value = raw.partition("=")
        if not key:
            raise click.UsageError(
                f"Invalid {flag_name} '{raw}': key cannot be empty"
            )
        result[key] = value
    return result


def _parse_env_pairs(pairs):
    """Parse ``--env KEY=VAL`` repetitions into a dict."""
    return _parse_kv_pairs(pairs, flag_name="--env")


def _parse_header_pairs(pairs):
    """Parse ``--header KEY=VAL`` repetitions into a dict."""
    return _parse_kv_pairs(pairs, flag_name="--header")


def _build_mcp_entry(name, *, transport, url, env, headers, version, command_argv,
                     registry_url=None):
    """Pure builder. Return ``(entry, is_self_defined)``.

    Routing:
    - ``command_argv`` non-empty -> stdio self-defined dict.
    - ``url`` set -> remote self-defined dict (transport defaults to http).
    - else -> registry shorthand (bare string when no overlays, dict when
      ``version`` / ``transport`` / ``registry_url`` is set; the URL is
      then persisted to the entry's ``registry:`` field for reproducible
      installs). ``registry_url`` is incompatible with self-defined
      entries; the CLI layer enforces that via E15.

    Round-trips through :class:`MCPDependency.from_dict` (or
    :meth:`from_string`) for the validation chokepoint.  Validation
    failures surface as :class:`ValueError` from the model.
    """
    from ..models.dependency.mcp import MCPDependency

    if command_argv:
        # Self-defined stdio
        argv = builtins.list(command_argv)
        entry: builtins.dict = {
            "name": name,
            "registry": False,
            "transport": "stdio",
            "command": argv[0],
        }
        if len(argv) > 1:
            entry["args"] = argv[1:]
        if env:
            entry["env"] = builtins.dict(env)
        MCPDependency.from_dict(entry)
        return entry, True

    if url:
        # Self-defined remote
        chosen_transport = transport or "http"
        entry = {
            "name": name,
            "registry": False,
            "transport": chosen_transport,
            "url": url,
        }
        if headers:
            entry["headers"] = builtins.dict(headers)
        MCPDependency.from_dict(entry)
        return entry, True

    # Registry shorthand
    if version:
        entry = {"name": name, "version": version}
        if transport:
            entry["transport"] = transport
        if registry_url:
            entry["registry"] = registry_url
        MCPDependency.from_dict(entry)
        return entry, False

    if transport:
        entry = {"name": name, "transport": transport}
        if registry_url:
            entry["registry"] = registry_url
        MCPDependency.from_dict(entry)
        return entry, False

    if registry_url:
        # No other overlays but a custom registry URL -- promote to dict
        # form so the URL is captured in apm.yml.
        entry = {"name": name, "registry": registry_url}
        MCPDependency.from_dict(entry)
        return entry, False

    # Bare string registry shorthand -- no overlays at all.
    MCPDependency.from_string(name)
    return name, False


def _diff_entry(old, new) -> builtins.list:
    """Return a short list of ``key: old -> new`` strings for human display."""
    if isinstance(old, str) and isinstance(new, str):
        if old == new:
            return []
        return [f"  {old} -> {new}"]
    old_d = {"name": old} if isinstance(old, str) else (old or {})
    new_d = {"name": new} if isinstance(new, str) else (new or {})
    keys = builtins.list(old_d.keys()) + [k for k in new_d.keys() if k not in old_d]
    diff: builtins.list = []
    for k in keys:
        ov = old_d.get(k, "<absent>")
        nv = new_d.get(k, "<absent>")
        if ov != nv:
            diff.append(f"  {k}: {ov!r} -> {nv!r}")
    return diff


def _add_mcp_to_apm_yml(name, entry, *, dev=False, force=False, project_root=None,
                        manifest_path=None, logger=None):
    """Persist ``entry`` to ``apm.yml`` under ``dependencies.mcp`` (or
    ``devDependencies.mcp`` when ``dev=True``).

    Idempotency policy (W3 R3, security F8):
    - Existing entry + ``--force``: replace silently, return
      ``("replaced", diff)``.
    - Existing entry + interactive TTY: prompt, return
      ``("replaced", diff)`` or ``("skipped", diff)``.
    - Existing entry + non-TTY (CI): raise :class:`click.UsageError` so
      the CLI exits with code 2.
    - New entry: append, return ``("added", None)``.
    """
    from ..utils.yaml_io import dump_yaml, load_yaml

    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)
    if not apm_yml_path.exists():
        raise click.UsageError(
            f"{apm_yml_path}: no apm.yml found. Run 'apm init' first."
        )
    data = load_yaml(apm_yml_path) or {}

    section_name = "devDependencies" if dev else "dependencies"
    if section_name not in data or not isinstance(data[section_name], builtins.dict):
        data[section_name] = {}
    if "mcp" not in data[section_name] or data[section_name]["mcp"] is None:
        data[section_name]["mcp"] = []
    mcp_list = data[section_name]["mcp"]
    if not isinstance(mcp_list, builtins.list):
        raise click.UsageError(
            f"{apm_yml_path}: '{section_name}.mcp' must be a list"
        )

    existing_idx = None
    existing_entry = None
    for i, item in enumerate(mcp_list):
        item_name = item if isinstance(item, str) else (
            item.get("name") if isinstance(item, builtins.dict) else None
        )
        if item_name == name:
            existing_idx = i
            existing_entry = item
            break

    status = "added"
    diff = None
    if existing_idx is not None:
        diff = _diff_entry(existing_entry, entry)
        if not diff:
            return "skipped", []
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        if force:
            mcp_list[existing_idx] = entry
            status = "replaced"
        elif is_tty:
            if logger:
                logger.warning(
                    f"MCP server '{name}' already exists. Replacement diff:"
                )
                for line in diff:
                    logger.verbose_detail(line)
            else:
                _rich_warning(
                    f"MCP server '{name}' already exists. Replacement diff:"
                )
                for line in diff:
                    _rich_echo(line, color="dim")
            if not click.confirm(f"Replace MCP server '{name}'?", default=False):
                return "skipped", diff
            mcp_list[existing_idx] = entry
            status = "replaced"
        else:
            raise click.UsageError(
                f"MCP server '{name}' already exists in {apm_yml_path}. "
                f"Use --force to replace (non-interactive)."
            )
    else:
        mcp_list.append(entry)

    data[section_name]["mcp"] = mcp_list
    dump_yaml(data, apm_yml_path)
    return status, diff


# Mapping for E10: which flags require --mcp.  Keyed by attribute-style
# name so we can read directly from the Click handler locals.
_MCP_REQUIRED_FLAGS = (
    ("transport", "--transport"),
    ("url", "--url"),
    ("env", "--env"),
    ("header", "--header"),
    ("mcp_version", "--mcp-version"),
)


def _validate_mcp_conflicts(
    *,
    mcp_name,
    packages,
    pre_dash_packages,
    transport,
    url,
    env,
    headers,
    mcp_version,
    command_argv,
    global_,
    only,
    update,
    use_ssh,
    use_https,
    allow_protocol_fallback,
    registry_url=None,
):
    """Apply conflict matrix E1-E15.  Raises ``click.UsageError`` on hit."""
    # E10: flags require --mcp -- run first so users get the right hint.
    if mcp_name is None:
        flag_values = {
            "transport": transport,
            "url": url,
            "env": env,
            "header": headers,
            "mcp_version": mcp_version,
            "registry": registry_url,
        }
        for attr, label in (*_MCP_REQUIRED_FLAGS, ("registry", "--registry")):
            if flag_values.get(attr):
                raise click.UsageError(f"{label} requires --mcp")
        if command_argv:
            # post-`--` stdio command without --mcp: silently allowed today
            # (legacy install behaviour).  Do not error.
            pass
        return

    # E7/E8: NAME shape.
    if mcp_name == "":
        raise click.UsageError("MCP name cannot be empty")
    if mcp_name.startswith("-"):
        raise click.UsageError(
            f"MCP name cannot start with '-'; did you forget a value for --mcp?"
        )

    # E1: positional packages mixed with --mcp.
    if pre_dash_packages:
        raise click.UsageError(
            "cannot mix --mcp with positional packages"
        )

    # E2: --global not supported for MCP entries.
    if global_:
        raise click.UsageError(
            "MCP servers are project-scoped; --global is not supported for MCP entries"
        )

    # E3: --only apm conflicts with --mcp.
    if only == "apm":
        raise click.UsageError("cannot use --only apm with --mcp")

    # E4: transport selection flags do not apply.
    if use_ssh or use_https or allow_protocol_fallback:
        raise click.UsageError(
            "transport selection flags (--ssh/--https/--allow-protocol-fallback) "
            "don't apply to MCP entries"
        )

    # E5: --update is for refreshing, not adding.
    if update:
        raise click.UsageError("use 'apm update' instead to update MCP entries")

    # E9: --header without --url.
    if headers and not url:
        raise click.UsageError("--header requires --url")

    # E11: --url with stdio command.
    if url and command_argv:
        raise click.UsageError("cannot specify both --url and a stdio command")

    # E12: --transport stdio with --url.
    if transport == "stdio" and url:
        raise click.UsageError("stdio transport doesn't accept --url")

    # E13: remote transports with stdio command.
    if transport in ("http", "sse", "streamable-http") and command_argv:
        raise click.UsageError("remote transports don't accept stdio command")

    # E14: --env with --url and no command.
    if env and url and not command_argv:
        raise click.UsageError(
            "--env applies to stdio MCPs; use --header for remote"
        )

    # E15: --registry only applies to registry-resolved entries.
    if registry_url and (url or command_argv):
        raise click.UsageError(
            "--registry only applies to registry-resolved MCP servers; "
            "remove --url or the post-`--` stdio command, or drop --registry"
        )


def _run_mcp_install(
    *,
    mcp_name,
    transport,
    url,
    env_pairs,
    header_pairs,
    mcp_version,
    command_argv,
    dev,
    force,
    runtime,
    exclude,
    verbose,
    logger,
    manifest_path,
    apm_dir,
    scope,
    registry_url=None,
):
    """Execute the --mcp install path. ``registry_url`` is the validated
    --registry value; the caller resolved precedence vs MCP_REGISTRY_URL."""
    from ..models.dependency.mcp import MCPDependency

    env = _parse_env_pairs(env_pairs)
    headers = _parse_header_pairs(header_pairs)

    # Build entry (validates through MCPDependency).  Convert ValueError
    # to UsageError so the CLI exits 2 with the model wording.
    try:
        entry, _is_self_defined = _build_mcp_entry(
            mcp_name,
            transport=transport,
            url=url,
            env=env,
            headers=headers,
            version=mcp_version,
            command_argv=command_argv,
            registry_url=registry_url,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc))

    # F5 + F7 warnings -- do not block.
    _warn_ssrf_url(url, logger)
    _warn_shell_metachars(env, logger, command=entry.get("command"))

    # Write to apm.yml.
    status, _diff = _add_mcp_to_apm_yml(
        mcp_name,
        entry,
        dev=dev,
        force=force,
        manifest_path=manifest_path,
        logger=logger,
    )

    if status == "skipped":
        logger.progress(f"MCP server '{mcp_name}' unchanged")
        return

    # Build MCPDependency for install.  ``entry`` may be a bare string.
    if isinstance(entry, str):
        dep = MCPDependency.from_string(entry)
    else:
        dep = MCPDependency.from_dict(entry)

    # Install just this MCP via the integrator and update lockfile.
    # ``registry_env_override`` exports MCP_REGISTRY_URL for THIS call so
    # MCPServerOperations() (constructed deep inside MCPIntegrator.install)
    # picks up the override; prior env restored on exit.
    if APM_DEPS_AVAILABLE:
        from ..install.mcp_registry import registry_env_override

        if registry_url and logger and verbose:
            logger.verbose_detail(f"Registry: {registry_url}")
        with registry_env_override(registry_url):
            try:
                _existing_lock = LockFile.read(get_lockfile_path(apm_dir))
                old_servers = builtins.set(_existing_lock.mcp_servers) if _existing_lock else builtins.set()
                old_configs = builtins.dict(_existing_lock.mcp_configs) if _existing_lock else {}
                MCPIntegrator.install(
                    [dep], runtime, exclude, verbose,
                    stored_mcp_configs=old_configs,
                    scope=scope,
                )
                new_names = MCPIntegrator.get_server_names([dep])
                new_configs = MCPIntegrator.get_server_configs([dep])
                merged_names = old_servers | new_names
                merged_configs = builtins.dict(old_configs)
                merged_configs.update(new_configs)
                MCPIntegrator.update_lockfile(merged_names, mcp_configs=merged_configs)
            except Exception as exc:  # pragma: no cover -- defensive
                logger.warning(f"MCP server written to apm.yml but integration failed: {exc}")

    verb = "Replaced" if status == "replaced" else "Added"
    logger.success(f"{verb} MCP server '{mcp_name}'", symbol="check")
    if isinstance(entry, builtins.dict):
        chosen_transport = entry.get("transport") or "registry"
    else:
        chosen_transport = "registry"
    logger.tree_item(f"  transport: {chosen_transport}")
    logger.tree_item(f"  apm.yml: {manifest_path}")


# ---------------------------------------------------------------------------
# install() decomposition: extracted flow helpers
# ---------------------------------------------------------------------------


def _handle_mcp_install(
    *,
    mcp_name,
    transport,
    url,
    env_pairs,
    header_pairs,
    mcp_version,
    command_argv,
    dev,
    force,
    runtime,
    exclude,
    verbose,
    dry_run,
    logger,
    no_policy,
    validated_registry_url,
):
    """Execute the ``--mcp`` install path (MCP server add).

    Resolves registry URL, runs policy preflight, handles dry-run,
    and delegates to :func:`_run_mcp_install` for the actual installation.
    Called from :func:`install` when ``--mcp`` is specified; the caller
    returns immediately after this function completes.
    """
    from ..core.scope import (
        InstallScope, get_apm_dir, get_manifest_path,
    )
    # Apply CLI > env > default precedence; emit override diagnostic.
    resolved_registry_url, _registry_source = _resolve_registry_url(
        validated_registry_url, logger=logger,
    )
    mcp_scope = InstallScope.PROJECT
    mcp_manifest_path = get_manifest_path(mcp_scope)
    mcp_apm_dir = get_apm_dir(mcp_scope)
    # -- W2-mcp-preflight: policy enforcement before MCP install --
    # Build a lightweight MCPDependency for policy evaluation.
    # This mirrors _build_mcp_entry routing but we only need the
    # fields that policy checks inspect (name, transport, registry).
    from ..models.dependency.mcp import MCPDependency as _MCPDep
    from ..policy.install_preflight import (
        PolicyBlockError,
        run_policy_preflight,
    )

    _is_self_defined = bool(url or command_argv)
    _preflight_transport = transport
    if _preflight_transport is None:
        if command_argv:
            _preflight_transport = "stdio"
        elif url:
            _preflight_transport = "http"
    _preflight_dep = _MCPDep(
        name=mcp_name,
        transport=_preflight_transport,
        registry=False if _is_self_defined else None,
        url=url,
    )

    try:
        _pf_result, _pf_active = run_policy_preflight(
            project_root=Path.cwd(),
            mcp_deps=[_preflight_dep],
            no_policy=no_policy,
            logger=logger,
            dry_run=dry_run,
        )
    except PolicyBlockError:
        # Diagnostics already emitted by the helper + logger.
        logger.render_summary()
        sys.exit(1)

    if dry_run:
        # C1: validate eagerly so dry-run rejects what real install would.
        _validate_mcp_dry_run_entry(
            mcp_name, transport=transport, url=url, env=env_pairs,
            headers=header_pairs, version=mcp_version,
            command_argv=command_argv, registry_url=resolved_registry_url,
        )
        logger.dry_run_notice(
            f"would add MCP server '{mcp_name}' to {mcp_manifest_path}"
        )
        return
    _run_mcp_install(
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
        logger=logger,
        manifest_path=mcp_manifest_path,
        apm_dir=mcp_apm_dir,
        scope=mcp_scope,
        registry_url=validated_registry_url,
    )


@click.command(
    help="Install APM and MCP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json); auto-creates apm.yml; use --allow-insecure for http:// packages)"
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
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated for multiple, e.g. claude,copilot). Use 'all' for every target. Overrides auto-detection.",
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
    "--global", "-g", "global_",
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
    help="Add an MCP server entry to apm.yml. Use with --transport, --url, --env, --header, --mcp-version, or post-- stdio command.",
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
@click.option("--skill", "skill_names", multiple=True, metavar="NAME", help="Install only named skill(s) from a SKILL_BUNDLE. Repeatable. Persisted in apm.yml and apm.lock so bare 'apm install' is deterministic. Use --skill '*' to reset to all skills.")
@click.option("--no-policy", "no_policy", is_flag=True, default=False, help="Skip org policy enforcement for this invocation. Does NOT bypass apm audit --ci.")
@click.pass_context
def install(ctx, packages, runtime, exclude, only, update, dry_run, force, verbose, trust_transitive_mcp, parallel_downloads, dev, target, allow_insecure, allow_insecure_hosts, global_, use_ssh, use_https, allow_protocol_fallback, mcp_name, transport, url, env_pairs, header_pairs, mcp_version, registry_url, skill_names, no_policy):
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
    """
    # C1 #856: defaults BEFORE try so the finally clause never sees an
    # UnboundLocalError if InstallLogger(...) raises during construction.
    _apm_verbose_prev = os.environ.get("APM_VERBOSE")
    try:
        # Create structured logger for install output early so exception
        # handlers can always reference it (avoids UnboundLocalError if
        # scope initialisation below throws).
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)
        # HACK(#852): surface --verbose to deeper auth layers via env var until
        # AuthResolver gains a first-class verbose channel. Restored in finally
        # below to keep the mutation scoped to this command invocation.
        if verbose:
            os.environ["APM_VERBOSE"] = "1"

        # W2-pkg-rollback (#827): snapshot bytes captured BEFORE
        # _validate_and_add_packages_to_apm_yml mutates apm.yml.
        # Initialised to None here so exception handlers always have it.
        _manifest_snapshot: "bytes | None" = None
        # manifest_path is set later (scope-dependent); keep a stable ref
        # so exception handlers can use it without NameError.
        _snapshot_manifest_path: "Path | None" = None

        # ----------------------------------------------------------------
        # --mcp branch (W3): when --mcp is set, route to the dedicated
        # MCP-add path.  We compute the post-`--` argv here BEFORE Click's
        # silent handling: see _split_argv_at_double_dash().
        # ----------------------------------------------------------------
        _, command_argv = _split_argv_at_double_dash(_get_invocation_argv())
        # `packages` from Click already includes the post-`--` items; the
        # pre-`--` portion is what the user typed as positional packages.
        if command_argv:
            split_idx = len(packages) - len(command_argv)
            if split_idx < 0:
                split_idx = 0
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
            _handle_mcp_install(
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
        from ..deps.transport_selection import (
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

            validated_packages, outcome = _validate_and_add_packages_to_apm_yml(
                packages, dry_run, dev=dev, logger=logger,
                manifest_path=manifest_path, auth_resolver=auth_resolver,
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
            only_packages=builtins.list(validated_packages) if packages else None,
            manifest_snapshot=_manifest_snapshot,
            snapshot_manifest_path=_snapshot_manifest_path,
        )

        apm_count, mcp_count, apm_diagnostics = _install_apm_packages(
            install_ctx, outcome,
        )

        _post_install_summary(
            logger=logger,
            apm_count=apm_count,
            mcp_count=mcp_count,
            apm_diagnostics=apm_diagnostics,
            force=force,
        )

    except InsecureDependencyPolicyError:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        sys.exit(1)
    except AuthenticationError as e:
        _maybe_rollback_manifest(_snapshot_manifest_path, _manifest_snapshot, logger)
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
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
        logger.error(f"Error installing dependencies: {e}")
        if not verbose:
            logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)
    finally:
        # HACK(#852) cleanup: restore APM_VERBOSE so it stays scoped to this call.
        if _apm_verbose_prev is None:
            os.environ.pop("APM_VERBOSE", None)
        else:
            os.environ["APM_VERBOSE"] = _apm_verbose_prev


# ---------------------------------------------------------------------------
# install() decomposition: APM pipeline + post-install summary
# ---------------------------------------------------------------------------


def _install_apm_packages(ctx, outcome):
    """Execute the APM + transitive MCP installation pipeline.

    Parses ``apm.yml``, installs APM dependencies, collects and installs
    transitive MCP servers, and handles lockfile updates.

    Args:
        ctx: :class:`InstallContext` with configuration and environment.
        outcome: ``_ValidationOutcome`` from package validation (may be
            ``None`` when no explicit packages were passed).

    Returns:
        Tuple of ``(apm_count, mcp_count, apm_diagnostics)``.
    """
    logger = ctx.logger

    logger.resolution_start(
        to_install_count=len(ctx.only_packages or []) if ctx.packages else 0,
        lockfile_count=0,  # Refined later inside _install_apm_dependencies
    )

    # Parse apm.yml to get both APM and MCP dependencies
    try:
        apm_package = APMPackage.from_apm_yml(ctx.manifest_path)
    except Exception as e:
        logger.error(f"Failed to parse {ctx.manifest_display}: {e}")
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

    all_apm_deps = list(apm_deps) + list(dev_apm_deps)
    _check_insecure_dependencies(all_apm_deps, ctx.allow_insecure, logger)

    # Determine what to install based on install mode
    should_install_apm = ctx.install_mode != InstallMode.MCP
    should_install_mcp = ctx.install_mode != InstallMode.APM

    # Show what will be installed if dry run
    if ctx.dry_run:
        # -- W2-dry-run (#827): policy preflight in preview mode --
        # Runs discovery + checks against direct manifest deps (not
        # resolved/transitive -- dry-run does not run the resolver).
        # Block-severity violations render as "Would be blocked by
        # policy" without raising.  Documented limitation: transitive
        # deps are NOT evaluated since the resolver does not run.
        from apm_cli.policy.install_preflight import run_policy_preflight as _dr_preflight

        _dr_apm_deps = builtins.list(apm_deps) + builtins.list(dev_apm_deps)
        _dr_preflight(
            project_root=ctx.project_root,
            apm_deps=_dr_apm_deps,
            mcp_deps=mcp_deps if should_install_mcp else None,
            no_policy=ctx.no_policy,
            logger=logger,
            dry_run=True,
        )

        from apm_cli.install.presentation.dry_run import render_and_exit

        render_and_exit(
            logger=logger,
            should_install_apm=should_install_apm,
            apm_deps=apm_deps,
            mcp_deps=mcp_deps,
            dev_apm_deps=dev_apm_deps,
            should_install_mcp=should_install_mcp,
            update=ctx.update,
            only_packages=ctx.only_packages,
            apm_dir=ctx.apm_dir,
        )
        return 0, 0, None  # render_and_exit exits; this line is defensive

    # Install APM dependencies first (if requested)
    apm_count = 0
    prompt_count = 0
    agent_count = 0

    # Migrate legacy apm.lock -> apm.lock.yaml if needed (one-time, transparent)
    migrate_lockfile_if_needed(ctx.apm_dir)

    # Capture old MCP servers and configs from lockfile BEFORE
    # _install_apm_dependencies regenerates it (which drops the fields).
    # We always read this -- even when --only=apm -- so we can restore the
    # field after the lockfile is regenerated by the APM install step.
    old_mcp_servers: builtins.set = builtins.set()
    old_mcp_configs: builtins.dict = {}
    _lock_path = get_lockfile_path(ctx.apm_dir)
    _existing_lock = LockFile.read(_lock_path)
    if _existing_lock:
        old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
        old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)

    # Also enter the APM install path when the project root has local .apm/
    # primitives, even if there are no external APM dependencies (#714).
    from apm_cli.core.scope import InstallScope, get_deploy_root as _get_deploy_root
    _cli_project_root = _get_deploy_root(ctx.scope)

    apm_diagnostics = None
    if should_install_apm and (has_any_apm_deps or _project_has_root_primitives(_cli_project_root)):
        if not APM_DEPS_AVAILABLE:
            logger.error("APM dependency system not available")
            logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
            sys.exit(1)

        try:
            # If specific packages were requested, only install those
            # Otherwise install all from apm.yml.
            # `only_packages` was computed above so the dry-run preview
            # and the actual install share one canonical list.
            install_result = _install_apm_dependencies(
                apm_package, ctx.update, ctx.verbose, ctx.only_packages, force=ctx.force,
                parallel_downloads=ctx.parallel_downloads,
                logger=logger,
                scope=ctx.scope,
                auth_resolver=ctx.auth_resolver,
                target=ctx.target,
                allow_insecure=ctx.allow_insecure,
                allow_insecure_hosts=ctx.allow_insecure_hosts,
                marketplace_provenance=(
                    outcome.marketplace_provenance if ctx.packages and outcome else None
                ),
                protocol_pref=ctx.protocol_pref,
                allow_protocol_fallback=ctx.allow_protocol_fallback,
                no_policy=ctx.no_policy,
            )
            apm_count = install_result.installed_count
            prompt_count = install_result.prompts_integrated
            agent_count = install_result.agents_integrated
            apm_diagnostics = install_result.diagnostics
        except InsecureDependencyPolicyError:
            _maybe_rollback_manifest(ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger)
            sys.exit(1)
        except AuthenticationError as e:
            # #1015: render auth diagnostics on the DEFAULT path (not --verbose).
            _maybe_rollback_manifest(ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger)
            _rich_error(str(e))
            if e.diagnostic_context:
                _rich_echo(e.diagnostic_context)
            sys.exit(1)
        except Exception as e:
            _maybe_rollback_manifest(ctx.snapshot_manifest_path, ctx.manifest_snapshot, logger)
            # #832: surface PolicyViolationError verbatim (no double-nesting).
            msg = str(e) if isinstance(e, PolicyViolationError) else f"Failed to install APM dependencies: {e}"
            logger.error(msg)
            if not ctx.verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
            sys.exit(1)
    elif should_install_apm and not has_any_apm_deps:
        logger.verbose_detail("No APM dependencies found in apm.yml")

    # When --update is used, package files on disk may have changed.
    # Clear the parse cache so transitive MCP collection reads fresh data.
    if ctx.update:
        from apm_cli.models.apm_package import clear_apm_yml_cache
        clear_apm_yml_cache()

    # Collect transitive MCP dependencies from resolved APM packages
    transitive_mcp = []
    from ..core.scope import get_modules_dir
    apm_modules_path = get_modules_dir(ctx.scope)
    if should_install_mcp and apm_modules_path.exists():
        lock_path = get_lockfile_path(ctx.apm_dir)
        transitive_mcp = MCPIntegrator.collect_transitive(
            apm_modules_path, lock_path, ctx.trust_transitive_mcp,
            diagnostics=apm_diagnostics,
        )
        if transitive_mcp:
            logger.verbose_detail(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
            mcp_deps = MCPIntegrator.deduplicate(mcp_deps + transitive_mcp)

    # -- S1/S2 fix (#827-C2/C3): enforce policy on ALL MCP deps ----
    # The pipeline gate phase (policy_gate.py) checks direct APM deps
    # and direct MCP deps from apm.yml.  However, transitive MCP
    # servers (discovered via collect_transitive above) are only known
    # after APM packages are installed.  Run a second preflight
    # against the *merged* MCP set (direct + transitive) BEFORE
    # MCPIntegrator writes runtime configs.  On PolicyBlockError we
    # abort the MCP write but leave already-installed APM packages
    # in place (they were approved by the gate phase).
    if should_install_mcp and mcp_deps:
        from apm_cli.policy.install_preflight import (
            PolicyBlockError as _TransitivePBE,
            run_policy_preflight as _transitive_preflight,
        )

        try:
            _transitive_preflight(
                project_root=ctx.project_root,
                mcp_deps=mcp_deps,
                no_policy=ctx.no_policy,
                logger=logger,
                dry_run=False,
            )
        except _TransitivePBE:
            logger.error(
                "MCP server(s) blocked by org policy. "
                "APM packages remain installed; MCP configs were NOT written."
            )
            logger.render_summary()
            sys.exit(1)

    # Continue with MCP installation (existing logic)
    mcp_count = 0
    new_mcp_servers: builtins.set = builtins.set()
    mcp_apm_config = {
        "target": apm_package.target,
        "scripts": apm_package.scripts or {},
    }
    if should_install_mcp and mcp_deps:
        mcp_count = MCPIntegrator.install(
            mcp_deps, ctx.runtime, ctx.exclude, ctx.verbose,
            stored_mcp_configs=old_mcp_configs,
            apm_config=mcp_apm_config,
            project_root=ctx.project_root,
            user_scope=(ctx.scope is InstallScope.USER),
            explicit_target=ctx.target,
            diagnostics=apm_diagnostics,
            scope=ctx.scope,
        )
        new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps)
        new_mcp_configs = MCPIntegrator.get_server_configs(mcp_deps)

        # Remove stale MCP servers that are no longer needed
        stale_servers = old_mcp_servers - new_mcp_servers
        if stale_servers:
            MCPIntegrator.remove_stale(
                stale_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )

        # Persist the new MCP server set and configs in the lockfile
        MCPIntegrator.update_lockfile(new_mcp_servers, mcp_configs=new_mcp_configs)
    elif should_install_mcp and not mcp_deps:
        # No MCP deps at all -- remove any old APM-managed servers
        if old_mcp_servers:
            MCPIntegrator.remove_stale(
                old_mcp_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )
            MCPIntegrator.update_lockfile(builtins.set(), mcp_configs={})
        logger.verbose_detail("No MCP dependencies found in apm.yml")
    elif not should_install_mcp and old_mcp_servers:
        # --only=apm: APM install regenerated the lockfile and dropped
        # mcp_servers.  Restore the previous set so it is not lost.
        MCPIntegrator.update_lockfile(old_mcp_servers, mcp_configs=old_mcp_configs)

    # Local .apm/ content integration is now handled inside the
    # install pipeline (phases/integrate.py + phases/post_deps_local.py,
    # refactor F3).  The duplicate target resolution, integrator
    # initialization, and inline stale-cleanup block that lived here
    # have been removed.

    return apm_count, mcp_count, apm_diagnostics


def _post_install_summary(*, logger, apm_count, mcp_count, apm_diagnostics, force):
    """Render diagnostics and final install summary.

    Shows diagnostic details (if any), the install summary line, and
    exits with code 1 when critical security findings are present
    (unless *force* is set).
    """
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
    logger.install_summary(
        apm_count=apm_count,
        mcp_count=mcp_count,
        errors=error_count,
        stale_cleaned=logger.stale_cleaned_total,
    )

    # Hard-fail when critical security findings blocked any package.
    # Consistent with apm unpack which also hard-fails on critical.
    # Use --force to override.
    if not force and apm_diagnostics and apm_diagnostics.has_critical_security:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Install engine
# ---------------------------------------------------------------------------


# Re-exports for backward compatibility -- the real implementations live
# in apm_cli.install.services (P1 -- DI seam).  Tests that
# @patch("apm_cli.commands.install._integrate_package_primitives") still
# work because patching this module-level alias rebinds the name where
# call-sites in this module would look it up.  Tests inside this codebase
# now patch the canonical apm_cli.install.services._integrate_package_primitives
# directly to avoid relying on transitive aliasing.
from apm_cli.install.services import (
    integrate_package_primitives,
    integrate_local_content,
    _integrate_package_primitives,
    _integrate_local_content,
)




# ---------------------------------------------------------------------------
# Pipeline entry point -- thin re-export preserving the patch path
# ``apm_cli.commands.install._install_apm_dependencies`` used by tests.
#
# The real implementation lives in ``apm_cli.install.pipeline`` (F2).
# ---------------------------------------------------------------------------
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
    allow_insecure: bool = False,
    allow_insecure_hosts=(),
    marketplace_provenance: dict = None,
    protocol_pref=None,
    allow_protocol_fallback: "Optional[bool]" = None,
    no_policy: bool = False,
    skill_subset: "Optional[builtins.tuple]" = None,
    skill_subset_from_cli: bool = False,
):
    """Thin wrapper -- builds an :class:`InstallRequest` and delegates to
    :class:`apm_cli.install.service.InstallService`.

    Kept here so that ``@patch("apm_cli.commands.install._install_apm_dependencies")``
    continues to intercept calls from the Click handler.  The service
    itself is the typed Application Service entry point for any future
    programmatic callers.
    """
    if not APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService

    request = InstallRequest(
        apm_package=apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target=target,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        no_policy=no_policy,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
    )
    return InstallService().run(request)
