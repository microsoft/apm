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
        prefix="apm-restore-",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(snapshot)
        os.replace(tmp_name, str(manifest_path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
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


def _check_package_conflicts(current_deps):
    """Build identity set from existing deps for duplicate detection.

    Parses each entry in *current_deps* (string or dict form) through
    :class:`sys.modules[__package__].DependencyReference` and collects identity strings.

    Returns:
        ``set`` of identity strings for existing dependencies.
    """
    existing_identities = builtins.set()
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, str):
                ref = sys.modules[__package__].DependencyReference.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                ref = sys.modules[__package__].DependencyReference.parse_from_dict(dep_entry)
            else:
                continue
            existing_identities.add(ref.get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return existing_identities


def _resolve_package_references(
    packages,
    current_deps,
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
        marketplace_provenance, apm_yml_entries, dependencies_changed)``.
    """
    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}
    _apm_yml_entries = {}  # canonical -> apm.yml entry (str or dict for HTTP deps)
    # #1305: canonical -> (marketplace_name, plugin_name, CrossRepoMisconfigRisk)
    # for cross-repo dict ``type: github`` sources on enterprise marketplaces
    # whose bare ``repo`` would mis-route auth at ``github.com``. Recorded
    # before validation runs so the validation-fail branch can emit an
    # actionable hint -- ``_marketplace_provenance`` is only written on
    # validation success and cannot be relied on at the failure boundary.
    _misconfig_risks = {}
    validated_packages = []
    dependencies_changed = False

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
        marketplace_dep_ref = None
        if "/" not in package and not sys.modules[__package__].DependencyReference.is_local_path(
            package
        ):
            try:
                from ...marketplace.resolver import (
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

                        def warning_handler(msg):
                            return logger.warning(msg)

                        logger.verbose_detail(
                            f"    Resolving {plugin_name}@{marketplace_name} via marketplace..."
                        )
                    resolution = resolve_marketplace_plugin(
                        plugin_name,
                        marketplace_name,
                        version_spec=version_spec,
                        auth_resolver=auth_resolver,
                        warning_handler=warning_handler,
                    )
                    canonical_str, _resolved_plugin = resolution
                    if logger:
                        logger.verbose_detail(f"    Resolved to: {canonical_str}")
                    marketplace_provenance = {
                        "discovered_via": marketplace_name,
                        "marketplace_plugin_name": plugin_name,
                    }
                    package = canonical_str
                    marketplace_dep_ref = getattr(resolution, "dependency_reference", None)
                    _risk = getattr(resolution, "cross_repo_misconfig_risk", None)
                    if _risk is not None:
                        _misconfig_risks[canonical_str] = (
                            marketplace_name,
                            plugin_name,
                            _risk,
                        )
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
            dep_ref, direct_gitlab_virtual_resolved = resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref,
                dependency_reference_cls=sys.modules[__package__].DependencyReference,
                try_resolve_gitlab_direct_shorthand=sys.modules[
                    __package__
                ]._try_resolve_gitlab_direct_shorthand,
                auth_resolver=auth_resolver,
                verbose=bool(logger and logger.verbose),
            )
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
            if marketplace_dep_ref is not None or direct_gitlab_virtual_resolved:
                _apm_yml_entries[canonical] = dependency_reference_to_yaml_entry(dep_ref)
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

        scope_reject = user_scope_rejection_reason(dep_ref, scope)
        if scope_reject:
            invalid_outcomes.append((package, scope_reject))
            if logger:
                logger.validation_fail(package, scope_reject)
            continue

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        verbose = bool(logger and logger.verbose)
        if sys.modules[__package__]._validate_package_exists(
            package,
            verbose=verbose,
            auth_resolver=auth_resolver,
            logger=logger,
            dep_ref=dep_ref,
        ):
            valid_outcomes.append((canonical, already_in_deps))
            if logger:
                logger.validation_pass(canonical, already_present=already_in_deps)

            if not already_in_deps:
                validated_packages.append(canonical)
                existing_identities.add(identity)  # prevent duplicates within batch
            elif canonical in _apm_yml_entries:
                structured_entry = _apm_yml_entries[canonical]
                merge_structured_entry_into_current_deps(
                    current_deps,
                    structured_entry,
                    identity,
                    canonical,
                    dependency_reference_cls=sys.modules[__package__].DependencyReference,
                    logger=logger,
                )
                dependencies_changed = True
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = sys.modules[__package__]._local_path_failure_reason(dep_ref)
            if not reason:
                # Round-4 panel fix (devx-ux): name the four-step probe
                # chain explicitly when the validator exhausted it
                # (virtual subdirectory + explicit ref). Generic "not
                # accessible" hides the failure mode for the precise
                # case where the most diagnostics are available.
                is_subdir_ref_chain = (
                    dep_ref.is_virtual
                    and dep_ref.is_virtual_subdirectory()
                    and bool(dep_ref.reference)
                )
                if is_subdir_ref_chain:
                    reason = (
                        "all probes failed (marker-file, Contents API, "
                        "git ls-remote, shallow-fetch) -- verify the path "
                        "and ref exist and that your credentials have "
                        "read access"
                    )
                    if not verbose:
                        reason += " (run with --verbose for the full probe log)"
                else:
                    reason = "not accessible or doesn't exist"
                    if not verbose:
                        reason += " -- run with --verbose for auth details"
            invalid_outcomes.append((package, reason))
            if logger:
                logger.validation_fail(package, reason)
            # #1305: when a cross-repo dict ``type: github`` source on an
            # enterprise marketplace fails validation, the failure is most
            # likely the silent auth mis-route (bare canonical fell back to
            # ``github.com``). Surface the host-qualify hint inline so the
            # operator can correct ``marketplace.json`` without rerunning
            # under ``--verbose`` to decode the auth trace. ``logger.warning``
            # is used (not ``info``) per the PR #1292 panel review's explicit
            # guidance for this exact follow-up: a misconfiguration that
            # voids ``apm install`` should be at warning level, not buried
            # in info-level ambient output. The second clause acknowledges
            # the legitimate cross-host alternative so operators whose
            # github.com dep failed for a transient reason (rate limit,
            # network, expired PAT) are not misdirected into adding an
            # enterprise host prefix that would break a working config.
            _risk_entry = _misconfig_risks.get(package)
            if _risk_entry is not None and logger:
                _mp_name, _plugin_name, _risk = _risk_entry
                logger.warning(
                    f"'{_plugin_name}@{_mp_name}' is registered on "
                    f"'{_risk.marketplace_host}' but the plugin's bare "
                    f"`repo: {_risk.bare_repo_field}` resolved to "
                    "'github.com'. If you meant the enterprise host, set "
                    "the plugin's `repo` field to "
                    f"'{_risk.suggested_qualified_repo}' in marketplace.json. "
                    "If this is intentionally a github.com dependency, "
                    "verify your github.com credentials and that the "
                    "repository is accessible."
                )

    return (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
        dependencies_changed,
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
        from ...utils.yaml_io import dump_yaml

        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(
                f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)"
            )
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)


def _validate_and_add_packages_to_apm_yml(
    packages,
    dry_run=False,
    dev=False,
    logger=None,
    manifest_path=None,
    auth_resolver=None,
    scope=None,
    allow_insecure=False,
):
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
    from pathlib import Path

    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)

    # Read current apm.yml
    try:
        from ...utils.yaml_io import load_yaml

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
        dependencies_changed,
    ) = _resolve_package_references(
        packages,
        current_deps,
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
        persist_dependency_list_if_changed(
            dependencies_changed=dependencies_changed,
            data=data,
            dep_section=dep_section,
            current_deps=current_deps,
            apm_yml_path=apm_yml_path,
            apm_yml_filename=APM_YML_FILENAME,
            logger=logger,
            rich_error=_rich_error,
            sys_exit=sys.exit,
        )
        return [], outcome

    if dry_run:
        if logger:
            logger.progress(f"Dry run: Would add {len(validated_packages)} package(s) to apm.yml")
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
