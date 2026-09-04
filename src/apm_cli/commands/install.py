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

from apm_cli.agent_plugins.errors import (
    AgentPluginError,
    enforce_agent_plugin_deployment_boundary,
)
from apm_cli.copilot_plugins.settings import CopilotSettingsCollisionError
from apm_cli.install.argv import (
    _get_invocation_argv,
    _split_argv_at_double_dash,
)
from apm_cli.install.artifactory_resolver import _resolve_artifactory_boundary
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    InstallFailureAlreadyRendered,
    PolicyViolationError,
    RequiredIntegrationError,
)
from apm_cli.install.errors import (
    frozen_install_tip as _frozen_install_tip,
)
from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand
from apm_cli.install.helpers.ref_reuse import is_git_semver_resolution_eligible
from apm_cli.install.manifest_helpers import (
    _merge_packages_into_yml,
    _prepare_dry_run_manifest_path,
    _prepare_user_scope_for_install,
    _report_bootstrap_manifest,
)

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.install.plan import UpdatePlan

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.finalization import close_install_contexts
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan  # noqa: F401
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _format_insecure_dependency_requirements,
    _format_insecure_dependency_warning,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _InsecureDependencyInfo,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
)
from apm_cli.install.locking import serialized_lifecycle_unless

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.mcp.entry import _build_mcp_entry  # noqa: F401
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml  # noqa: F401
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    apply_cli_skill_pin,
    cli_skill_subset,
    dependency_reference_to_yaml_entry,
    persist_dependency_list_if_changed,
    propagate_existing_registry_source,
    resolve_parsed_dependency_reference,
    try_update_existing_dependency_entry,
    user_scope_rejection_reason,
)
from apm_cli.install.package_selection import only_packages_from_validation

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
from apm_cli.install.phases.local_content import (
    _copy_local_package,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _has_local_apm_content,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _project_has_root_primitives,
)

# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed  # noqa: F401

# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
from apm_cli.install.services import (
    _integrate_local_content,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _integrate_package_primitives,  # noqa: F401 -- re-exported; tests import/patch from apm_cli.commands.install
)
from apm_cli.install.transaction import InstallTransaction

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays co-located (module lookup keeps @patch working).
from apm_cli.install.validation import (
    _generic_host_ambiguous_subpath_hint as _ambiguous_subpath_hint,
)
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,  # noqa: F401 -- re-exported; test_architecture_invariants checks importability
    _validate_package_exists,
)
from apm_cli.models.results import InstallDisposition, InstallResult
from apm_cli.utils.diagnostics import DiagnosticCollector  # noqa: F401

from ..constants import (
    APM_YML_FILENAME,
    InstallMode,
)
from ..core.auth import AuthResolver
from ..core.command_logger import InstallLogger, _ValidationOutcome
from ..core.project_name import (
    resolve_bootstrap_project_name as _resolve_bootstrap_project_name,
)
from ..core.target_catalog import target_all_exclusion_help, target_help_fragment
from ..core.target_detection import TargetParamType, manifest_targets_from_target_option
from ..install.mcp.args import parse_env_pairs as _parse_mcp_env_pairs
from ..install.mcp.args import parse_header_pairs as _parse_mcp_header_pairs

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ..install.mcp.command import (
    run_mcp_install as _run_mcp_install,
)
from ..install.mcp.command import (
    run_mcp_policy_preflight as _run_mcp_policy_preflight,
)
from ..install.mcp.conflicts import (
    validate_mcp_conflicts as _validate_mcp_conflicts,
)
from ..install.mcp.registry import resolve_registry_url as _resolve_registry_url
from ..install.mcp.registry import validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry
from ..install.mcp.registry import validate_registry_url as _validate_registry_url
from ..utils.console import (  # noqa: F401 -- _rich_success re-exported; tests patch commands.install._rich_success
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
)
from ._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
)

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
    runtime: str | None
    exclude: str | None
    target: str | None
    parallel_downloads: int
    allow_insecure: bool
    allow_insecure_hosts: tuple
    protocol_pref: Any  # ProtocolPreference
    allow_protocol_fallback: bool
    trust_transitive_mcp: bool
    no_policy: bool
    install_mode: Any  # InstallMode
    packages: tuple  # Original Click packages
    transaction: Any = None  # InstallTransaction; default preserves direct-call compatibility
    refresh: bool = False
    only_packages: builtins.list | None = None
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: "Callable[[UpdatePlan], bool] | None" = None
    skill_subset: "builtins.tuple[str, ...] | None" = None
    skill_subset_from_cli: bool = False
    audit_override: str | None = None
    install_result: InstallResult | None = None
    target_decision: "EffectiveTargetDecision | None" = None
    trust_bin: bool | None = None
    exec_allow_map: builtins.dict[str, builtins.dict[str, bool]] | None = None
    exec_allow_resolved: bool = False
    create_config: bool = True


# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ..deps.apm_resolver import APMDependencyResolver
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ..integration.mcp_integrator import (
        MCPIntegrator,  # noqa: F401 -- re-exported; tests patch commands.install.MCPIntegrator
    )
    from ..models.apm_package import APMPackage, DependencyReference

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
    current_deps,
    existing_identities,
    *,
    auth_resolver=None,
    logger=None,
    scope=None,
    allow_insecure=False,
    skill_subset=None,
    skill_subset_from_cli=False,
    default_registry=None,
    dry_run=False,
    updated_packages_out=None,
):
    """Validate, canonicalize, and resolve package references.

    Handles marketplace refs, canonical parsing, insecure-URL guards,
    local-at-user-scope rejection, and accessibility checks.

    *existing_identities* is mutated (new identities are added to prevent
    duplicates within the same batch).

    Returns the validation outcomes, manifest entries, and change state.
    """
    from ..install.registry_wiring import should_skip_github_probe_for_dep, validate_registry_ref

    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}
    _apm_yml_entries = {}  # canonical -> apm.yml entry (str or dict for HTTP deps)
    validated_packages = []
    dependencies_changed = False
    manifest_identities = builtins.set(existing_identities)

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
                    # #1326: dependency-confusion fail-closed gate.
                    # Bare ``owner/repo`` on *.ghe.com falls back to
                    # github.com -- refuse before outbound validation so
                    # no probe reaches a potentially attacker-controlled URL.
                    # Escape hatch: host-qualify ``repo:`` in marketplace.json.
                    _risk = resolution.cross_repo_misconfig_risk
                    if _risk is not None:
                        _lead = (
                            f"refused (dependency-confusion risk #1326): bare"
                            f" `repo: {_risk.bare_repo_field}` on enterprise"
                            f" marketplace '{_risk.marketplace_host}' is ambiguous."
                            f" Host-qualify the plugin `repo` field in"
                            f" marketplace.json to one of:"
                        )
                        reason = "\n".join(
                            [
                                _lead,
                                f"  - '{_risk.suggested_qualified_repo}' (enterprise dep on this marketplace)",
                                f"  - 'github.com/{_risk.bare_repo_field}' (declared cross-host dep on public github.com)",
                            ]
                        )
                        invalid_outcomes.append((package, reason))
                        if logger:
                            logger.validation_fail(package, reason)
                        continue
                    marketplace_provenance = resolution.provenance(marketplace_name, plugin_name)
                    package = canonical_str
                    marketplace_dep_ref = getattr(resolution, "dependency_reference", None)
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
            dep_ref, direct_virtual_resolved = resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=_try_resolve_gitlab_direct_shorthand,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
                auth_resolver=auth_resolver,
                verbose=bool(logger and logger.verbose),
                logger=logger,
            )
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
            existing_source_is_registry = propagate_existing_registry_source(
                dep_ref,
                current_deps,
                identity,
                dependency_reference_cls=DependencyReference,
                logger=logger,
            )
            apply_cli_skill_pin(
                dep_ref,
                skill_subset,
                skill_subset_from_cli,
                current_deps,
                _apm_yml_entries,
                dependency_reference_cls=DependencyReference,
                logger=logger,
            )
            if marketplace_dep_ref is not None or direct_virtual_resolved:
                _apm_yml_entries.setdefault(canonical, dependency_reference_to_yaml_entry(dep_ref))
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

        if skill_subset and canonical not in _apm_yml_entries:
            _apm_yml_entries[canonical] = dep_ref.to_apm_yml_entry()
        _apm_yml_entries.setdefault(canonical, dep_ref.to_apm_yml_entry())

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        verbose = bool(logger and logger.verbose)
        # Registry routing is authoritative: it validates its own version
        # selectors before git semver eligibility can defer the raw-ref probe.
        if existing_source_is_registry or should_skip_github_probe_for_dep(
            dep_ref, default_registry
        ):
            ref_ok, ref_err = validate_registry_ref(dep_ref)
            if not ref_ok:
                invalid_outcomes.append((package, ref_err))
                if logger:
                    logger.validation_fail(package, ref_err)
                continue
            package_accessible = True
        elif is_git_semver_resolution_eligible(dep_ref):
            package_accessible = True
        else:
            package_accessible = _validate_package_exists(
                package,
                verbose=verbose,
                auth_resolver=auth_resolver,
                logger=logger,
                dep_ref=dep_ref,
            )
        if package_accessible:
            updates_existing_entry, update_error = try_update_existing_dependency_entry(
                current_deps,
                already_in_deps=identity in manifest_identities,
                apm_yml_entries=_apm_yml_entries,
                canonical=canonical,
                dep_ref=dep_ref,
                identity=identity,
                dependency_reference_cls=DependencyReference,
                logger=logger,
            )
            if update_error:
                invalid_outcomes.append((package, update_error))
                if logger:
                    logger.validation_fail(package, update_error)
                continue
            valid_outcomes.append((canonical, already_in_deps))
            if logger:
                logger.validation_pass(canonical, already_in_deps, updates_existing_entry, dry_run)
            if not already_in_deps:
                validated_packages.append(canonical)
                existing_identities.add(identity)
            dependencies_changed = dependencies_changed or updates_existing_entry
            if updates_existing_entry and updated_packages_out is not None:
                updated_packages_out.append(canonical)
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = _local_path_failure_reason(dep_ref) or _ambiguous_subpath_hint(dep_ref)
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

    return (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
        dependencies_changed,
    )


def _validate_and_add_packages_to_apm_yml(
    packages,
    dry_run=False,
    dev=False,
    logger=None,
    manifest_path=None,
    auth_resolver=None,
    scope=None,
    allow_insecure=False,
    skill_subset=None,
    skill_subset_from_cli=False,
    create_config=True,
    manifest_display=None,
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
        from ..utils.yaml_io import load_yaml_roundtrip

        data = load_yaml_roundtrip(apm_yml_path) or {}
    except Exception as e:
        (logger or InstallLogger()).error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    from ..install.registry_wiring import get_effective_default_registry

    _default_registry_for_cli = get_effective_default_registry(data, create_config=create_config)

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
    updated_packages = []
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
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        default_registry=_default_registry_for_cli,
        dry_run=dry_run,
        updated_packages_out=updated_packages,
    )

    prospective_package = None
    if dry_run and valid_outcomes:
        prospective_dependencies = list(current_deps)
        prospective_dependencies.extend(
            _apm_yml_entries.get(package, package) for package in validated_packages
        )
        data[dep_section]["apm"] = prospective_dependencies
        prospective_package = APMPackage.from_mapping(
            data,
            package_path=apm_yml_path.parent,
            source_path=apm_yml_path.parent,
            manifest_path=apm_yml_path,
            create_config=create_config,
        )

    outcome = _ValidationOutcome(
        valid=valid_outcomes,
        invalid=invalid_outcomes,
        marketplace_provenance=_marketplace_provenance or None,
        prospective_package=prospective_package,
        updated_packages=tuple(updated_packages),
    )

    if logger:
        should_continue = logger.validation_summary(outcome)
        if not should_continue:
            return [], outcome

    if not validated_packages:
        if dry_run:
            if logger:
                if dependencies_changed:
                    logger.progress(
                        "Dry run: Would update dependency entries in "
                        f"{manifest_display or APM_YML_FILENAME}"
                    )
                else:
                    logger.progress("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them.
        if not dry_run:
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
            package_label = "package" if len(validated_packages) == 1 else "packages"
            logger.progress(
                "Dry run: Would add "
                f"{len(validated_packages)} {package_label} to "
                f"{manifest_display or APM_YML_FILENAME}"
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

# F7 / F5 install-time MCP warnings live in apm_cli/install/mcp/warnings.py
# per LOC budget. Re-bind module-level names for back-compat with tests
# that still patch ``apm_cli.commands.install._warn_*``.


def _handle_mcp_install(  # noqa: PLR0913
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
    target,
    exclude,
    verbose,
    logger,
    no_policy,
    validated_registry_url,
    scope,
):
    """Resolve and execute the direct ``--mcp`` install path."""
    from ..core.scope import get_apm_dir, get_deploy_root, get_manifest_path, is_user_scope

    # Apply CLI > env > apm config > default precedence. Only dry runs announce
    # here; real installs announce the endpoint they query in the integrator.
    resolved_registry_url, registry_source = _resolve_registry_url(
        validated_registry_url,
        logger=logger,
        announce=logger.dry_run,
        create_config=not logger.dry_run,
    )
    integration_registry_url = resolved_registry_url
    mcp_manifest_path = get_manifest_path(scope)
    mcp_apm_dir = get_apm_dir(scope)
    from ..core.target_detection import resolve_manifest_target_decision

    target_decision = resolve_manifest_target_decision(
        Path.cwd(),
        manifest_path=mcp_manifest_path,
        explicit_target=target or runtime,
        user_scope=is_user_scope(scope),
        create_config=not logger.dry_run,
    )
    if is_user_scope(scope):
        from ..core.target_detection import EffectiveTargetDecision
        from ..integration.mcp_integrator_install import (
            discover_user_scope_mcp_runtimes,
            filter_excluded_mcp_runtimes,
            partition_user_scope_runtimes,
            unavailable_user_scope_targets_message,
        )

        scoped_runtime_targets = target_decision.runtime_targets_for_scope(user_scope=True)
        if scoped_runtime_targets is None:
            supported_runtimes, skipped_runtimes = discover_user_scope_mcp_runtimes(
                get_deploy_root(scope), exclude=exclude
            )
        else:
            scoped_runtime_targets = filter_excluded_mcp_runtimes(
                list(scoped_runtime_targets), exclude
            )
            supported_runtimes, skipped_runtimes = partition_user_scope_runtimes(
                list(scoped_runtime_targets)
            )
        if skipped_runtimes and supported_runtimes:
            logger.warning(
                "Skipped workspace-only runtimes at user scope: "
                f"{', '.join(sorted(skipped_runtimes))} -- omit --global to install these"
            )
        if not supported_runtimes:
            if exclude:
                raise click.UsageError(
                    f"All selected MCP runtimes were removed by --exclude {exclude}; "
                    "choose another target or remove the exclusion"
                )
            raise click.UsageError(
                unavailable_user_scope_targets_message(
                    target_decision, scoped_runtime_targets, skipped_runtimes
                )
            )
        target_decision = EffectiveTargetDecision(supported_runtimes, target_decision.source)
    _run_mcp_policy_preflight(
        mcp_name=mcp_name,
        transport=transport,
        url=url,
        command_argv=command_argv,
        no_policy=no_policy,
        logger=logger,
        target_decision=target_decision,
    )

    if logger.dry_run:
        _validate_mcp_dry_run_entry(
            mcp_name,
            transport=transport,
            url=url,
            env=_parse_mcp_env_pairs(env_pairs),
            headers=_parse_mcp_header_pairs(header_pairs),
            version=mcp_version,
            command_argv=command_argv,
            registry_url=resolved_registry_url,
        )

    initial_manifest_config = None
    if is_user_scope(scope) and not mcp_manifest_path.exists():
        project_name = _resolve_bootstrap_project_name(Path.home().name)
        initial_manifest_config = _get_default_config(project_name)
        if target is not None or runtime is not None:
            initial_manifest_config["targets"] = supported_runtimes
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
        target=target_decision.value,
        target_decision=target_decision,
        exclude=exclude,
        logger=logger,
        apm_dir=mcp_apm_dir,
        scope=scope,
        registry_url=integration_registry_url,
        registry_allow_http=registry_source == "flag",
        registry_source=registry_source,
        initial_manifest_config=initial_manifest_config,
    )


@click.command(
    help="Install APM, MCP, and LSP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json); auto-creates apm.yml; use --allow-insecure for http:// packages)"
)
@click.argument("packages", nargs=-1)
@click.option(
    "--runtime",
    help=(
        f"Target a specific runtime only. {target_help_fragment('install')} "
        "(legacy alias for --target, single value only; prefer --target)"
    ),
)
@click.option("--exclude", help="Exclude one runtime from the resolved MCP/LSP target set")
@click.option(
    "--only",
    type=click.Choice(["apm", "mcp"]),
    help="Install only APM packages or MCP/LSP service dependencies",
)
@click.option(
    "--update",
    is_flag=True,
    help="Update dependencies to latest Git references (deprecated: prefer 'apm update' for an interactive plan, or 'apm update --yes' for CI). Unlike --refresh, --update restructures the entire dependency graph.",
)
@click.option("--dry-run", is_flag=True, help="Show what would change without writing files")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-authored files on collision and deploy despite critical security findings (does NOT refresh refs; use 'apm update' for that)",
)
@click.option(
    "--frozen",
    is_flag=True,
    help=(
        "Refuse to install when apm.lock.yaml is missing or out of sync with "
        "apm.yml, including MCP config state (CI-safe; mutually exclusive with "
        "--update, --mcp, and positional packages). Use 'apm audit' for on-disk "
        "integrity."
    ),
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
    help=f"Target harness(es) to deploy to. Use commas for multiple targets; repeating the flag "
    f"keeps only the last value (use commas instead). {target_help_fragment('install')} "
    "IntelliJ-specific integration is MCP-only; file primitives use the Copilot profile. "
    f"{target_all_exclusion_help()}. Combine explicit-only targets when needed. "
    "Experimental targets require their feature flags. "
    "Target resolution: --runtime/--target > apm.yml targets: > apm config set target ... > "
    "auto-detect (only when no higher-priority selection exists). With nothing to detect, install "
    "exits 2 with a teaching message. For 'apm compile', use '--all'; '--target all' "
    "is deprecated.",
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
    help="Install to user scope (~/.apm/) instead of the current project. Direct MCP installs create or update ~/.apm/apm.yml. Mixed selections warn and skip workspace-only runtimes; selections with no global-capable runtime exit 2 before changing the user manifest, lockfile, or runtime configuration. Supported runtimes include Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Hermes, Kiro, Windsurf, and JetBrains Copilot.",
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
        "targets as --runtime/--target > apm.yml targets: > apm config set target ... > auto-detect "
        "(only when no higher-priority selection exists); writes only for active targets."
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
        "Precedence: this flag, MCP_REGISTRY_URL, apm config, then the public "
        "default. Captured in apm.yml on the entry's "
        "'registry:' field for auditability. Not valid with --url "
        "or a stdio command (self-defined entries)."
    ),
)
@click.option(
    "--skill",
    "skill_names",
    multiple=True,
    metavar="NAME",
    help="Install only named skill(s) from a SKILL_BUNDLE. Repeatable. Persisted in apm.yml and apm.lock so bare 'apm install' is deterministic. Additive across installs: a later --skill X adds X to the existing pin (union) rather than replacing it. Use --skill '*' (quote the asterisk in your shell) to reset to all skills; to drop a single skill, edit the skills: list in apm.yml then re-run apm install.",
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
    "--trust-bin/--no-trust-bin",
    "trust_bin",
    default=None,
    help=(
        "Allow or skip bin/ executable deployment for this invocation. "
        "--trust-bin deploys bin/ executables (suppresses the trust warning). "
        "--no-trust-bin skips bin/ deployment even when policy allows it. "
        "Default: deploys bin/ but emits a warning; non-interactive contexts "
        "skip bin/ by default. Use 'apm approve' for persistent per-package trust."
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
@serialized_lifecycle_unless("dry_run")
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
    trust_bin,
    root,
):
    """Install APM and MCP dependencies from apm.yml (like npm install).

    Detects AI runtimes from your apm.yml scripts and installs MCP servers for
    all detected runtimes; also installs APM package dependencies from GitHub.
    --only filters to APM packages or MCP/LSP service dependencies.

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1#1.0.0              # Add package to apm.yml and install
        apm install --exclude codex             # Install for all except Codex CLI
        apm install --only=apm                  # Install only APM packages
        apm install --update                    # Update dependencies to latest Git refs
        apm install --dry-run                   # Show what would change
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
    command_result: InstallResult | None = None
    transaction: InstallTransaction | None = None
    dry_run_manifest_tmp = None
    from ..install.service import InstallService

    try:
        if mcp_name is not None:
            InstallService.reject_frozen_mutation(frozen, "--mcp")
        elif packages:
            InstallService.reject_frozen_mutation(frozen, "positional packages")
    except FrozenInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if frozen and update:
        raise click.UsageError(
            "--frozen and --update are mutually exclusive. "
            "Use 'apm update' to refresh refs, then 'apm install --frozen' in CI."
        )
    if root and global_:
        raise click.UsageError("--root is not valid with --global (user scope)")
    from ..core.install_audit import resolve_audit_override_from_cli
    from ..install.root_redirect import install_root_redirect

    try:
        audit_override = resolve_audit_override_from_cli(no_audit, audit_mode)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    try:
        InstallService.reject_missing_frozen_root(frozen, root)
    except FrozenInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    _source_root = Path.cwd()
    _root_redirect = install_root_redirect(root, dry_run=dry_run)
    _root_redirect.__enter__()
    try:
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)
        # Resolve --legacy-skill-paths: CLI flag wins, then env var fallback.
        if not legacy_skill_paths:
            from ..integration.targets import should_use_legacy_skill_paths

            legacy_skill_paths = should_use_legacy_skill_paths()

        create_user_config = not dry_run

        if len(packages) == 1 and not mcp_name and (_probe := Path(packages[0])).exists():
            from ..bundle.local_bundle import detect_local_bundle as _detect_lb

            try:
                _bundle_info = _detect_lb(_probe)
            except AgentPluginError:
                raise
            except ValueError as exc:
                raise click.UsageError(f"Bundle security check failed: {exc}") from exc
            if _bundle_info is not None:
                enforce_agent_plugin_deployment_boundary(bundle_info=_bundle_info)
                from ..install.local_bundle_handler import (
                    effective_bundle_allow_map as _effective_bundle_allow_map,
                )
                from ..install.local_bundle_handler import install_local_bundle as _install_lb

                _bundle_project_root = _source_root
                _allow_execs_for_bundle = _effective_bundle_allow_map(
                    _bundle_project_root,
                    no_policy=no_policy,
                    logger=logger,
                    migrate_user_legacy=not dry_run,
                )
                _install_lb(
                    bundle_info=_bundle_info,
                    bundle_arg=packages[0],
                    target=target,
                    global_=global_,
                    force=force,
                    dry_run=dry_run,
                    verbose=verbose,
                    no_policy=no_policy,
                    alias=alias,
                    logger=logger,
                    legacy_skill_paths=legacy_skill_paths,
                    allow_executables=_allow_execs_for_bundle,
                    create_config=create_user_config,
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
                    },
                )
                # The local-bundle handler renders its own success summary.
                summary_rendered = True
                return
            # IM7: path exists but isn't a recognised bundle.  For archive
            # extensions (.zip / .tar.gz / .tgz) raise a targeted UsageError
            # instead of falling through to the registry clone path.
            # For bare directories we still fall through, because
            # ``apm install ./packages/source-pkg`` is a supported local-path
            # install that goes through the dependency-resolver pipeline.
            _suffix = _probe.name.lower()
            if _probe.is_file() and _suffix.endswith((".zip", ".tar.gz", ".tgz")):
                # Distinguish legacy --format apm bundles (apm.lock.yaml
                # present, plugin.json absent) from arbitrary tarballs so
                # the error message guides the user to the right next step.
                from ..bundle.local_bundle import _looks_like_legacy_apm_bundle

                if _looks_like_legacy_apm_bundle(_probe):
                    raise click.UsageError(
                        f"'{packages[0]}' was packed with '--format apm' (legacy format). "
                        "'apm install <bundle>' requires the plugin format. "
                        "Repack with 'apm pack --claude-plugin --archive', "
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
                "--as requires a local bundle path (directory, .zip, or .tar.gz "
                "produced by 'apm pack'). It has no effect on registry installs."
            )
        # HACK(#852): surface --verbose to deeper auth layers via env var until
        # AuthResolver gains a first-class verbose channel. Restored in finally
        # below to keep the mutation scoped to this command invocation.
        if verbose:
            os.environ["APM_VERBOSE"] = "1"

        # --mcp branch (W3): when --mcp is set, route to the dedicated
        # MCP-add path.  We compute the post-`--` argv here BEFORE Click's
        # silent handling: see _split_argv_at_double_dash().
        _, command_argv = _split_argv_at_double_dash(_get_invocation_argv())
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
        from ..core.scope import InstallScope

        scope = InstallScope.USER if global_ else InstallScope.PROJECT

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
            only=only,
            update=update,
            any_transport_flag=use_ssh or use_https or allow_protocol_fallback,
            registry_url=validated_registry_url,
        )
        # Normalize --skill: '*' means all (same as absent). Reject with --mcp.
        if skill_names and mcp_name is not None:
            raise click.UsageError("--skill cannot be combined with --mcp.")
        _skill_subset = cli_skill_subset(skill_names)

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
                target=target,
                exclude=exclude,
                verbose=verbose,
                logger=logger,
                no_policy=no_policy,
                validated_registry_url=validated_registry_url,
                scope=scope,
            )
            summary_rendered = True
            return

        # Resolve transport selection inputs.
        from ..deps.transport_selection import (
            ProtocolPreference,
        )

        if use_ssh and use_https:
            _rich_error("Options --ssh and --https are mutually exclusive.", symbol="error")
            sys.exit(2)
        if use_ssh:
            protocol_pref = ProtocolPreference.SSH
        elif use_https:
            protocol_pref = ProtocolPreference.HTTPS
        else:
            # Precedence: APM_GIT_PROTOCOL env var > apm config ssh > git insteadOf
            from ..config import get_apm_protocol_pref as _get_apm_protocol_pref

            _pref_str = _get_apm_protocol_pref(create_config=not dry_run)
            protocol_pref = ProtocolPreference.from_str(_pref_str)
        # CLI flag > env var (APM_ALLOW_PROTOCOL_FALLBACK) > apm config > default.
        # get_apm_allow_protocol_fallback() already encodes env > config > False.
        from ..config import get_apm_allow_protocol_fallback as _get_apm_apf

        allow_protocol_fallback = allow_protocol_fallback or _get_apm_apf(create_config=not dry_run)

        # Resolve scope
        from ..core.scope import (
            ensure_user_dirs,
            get_apm_dir,
            get_manifest_path,
            get_modules_dir,
            warn_unsupported_user_scope,
        )

        if scope is InstallScope.USER:
            _prepare_user_scope_for_install(
                dry_run=dry_run,
                logger=logger,
                ensure_user_dirs=ensure_user_dirs,
                warn_unsupported_user_scope=warn_unsupported_user_scope,
            )

        # Scope-aware paths
        manifest_path = get_manifest_path(scope)
        apm_dir = get_apm_dir(scope)
        # Display name for messages (short for project scope, full for user scope)
        manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME
        manifest_path, dry_run_manifest_tmp = _prepare_dry_run_manifest_path(
            manifest_path,
            dry_run=dry_run,
            user_scope=scope is InstallScope.USER,
            has_packages=bool(packages),
        )

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

        # Capture manifest state before this attempt can auto-create apm.yml.
        apm_yml_exists = manifest_path.exists()
        transaction = InstallTransaction(
            manifest_path=manifest_path,
            apm_modules_dir=get_modules_dir(scope),
            validation=None,
            logger=logger,
            acquire_lock=not dry_run,
        )

        if not apm_yml_exists and packages:
            derived_project_name = (
                Path.cwd().name if scope is InstallScope.PROJECT else Path.home().name
            )
            project_name = _resolve_bootstrap_project_name(derived_project_name)
            if project_name != derived_project_name:
                logger.verbose_detail(
                    f'Using default project name "{project_name}" because '
                    f"derived name {derived_project_name!a} is invalid"
                )
            config = _get_default_config(project_name)
            if manifest_targets := manifest_targets_from_target_option(target):
                config["targets"] = manifest_targets
            _create_minimal_apm_yml(config, target_path=manifest_path)
            _report_bootstrap_manifest(
                logger=logger,
                manifest_display=manifest_display,
                manifest_targets=manifest_targets,
                dry_run=dry_run,
                user_scope=scope is InstallScope.USER,
            )

        if not apm_yml_exists and not packages:
            logger.error(f"No {manifest_display} found")
            if scope is InstallScope.USER:
                logger.progress("Run 'apm install -g <org/repo>' to auto-create + install")
            else:
                logger.progress("Run 'apm init' to create one, or:")
                logger.progress("  apm install <org/repo> to auto-create + install")
            sys.exit(1)

        outcome = None
        if packages:
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
                skill_subset_from_cli=bool(skill_names),
                create_config=create_user_config,
                manifest_display=manifest_display,
            )
            transaction.record_validation(outcome)
            command_result = transaction.validation_result()
            if command_result is not None:
                summary_rendered = True
            # Note: Empty validated_packages is OK if packages are already in apm.yml;
            # only_packages is derived from validation outcomes below.

        if command_result is None:
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
                audit_override=audit_override,
                install_mode=InstallMode(only) if only else InstallMode.ALL,
                packages=packages,
                transaction=transaction,
                refresh=refresh,
                only_packages=only_packages_from_validation(packages, outcome),
                legacy_skill_paths=legacy_skill_paths,
                frozen=frozen,
                plan_callback=None,
                skill_subset=_skill_subset,
                skill_subset_from_cli=bool(skill_names),
                trust_bin=trust_bin,
                create_config=create_user_config,
            )

            apm_count, mcp_count, lsp_count, apm_diagnostics = _install_apm_packages(
                install_ctx,
                outcome,
            )

            from apm_cli.install.outcome import finalize_install_result

            command_result = install_ctx.install_result or InstallResult(
                installed_count=apm_count,
                diagnostics=apm_diagnostics,
            )
            command_result.installed_count = apm_count
            command_result.diagnostics = apm_diagnostics
            if dry_run and command_result.disposition is not InstallDisposition.FAILED:
                command_result.disposition = InstallDisposition.DRY_RUN
            command_result = finalize_install_result(
                command_result,
                force=force,
            )
            command_result = transaction.complete(command_result)

            command_result = _post_install_summary(
                logger=logger,
                apm_count=apm_count,
                mcp_count=mcp_count,
                lsp_count=lsp_count,
                apm_diagnostics=apm_diagnostics,
                force=force,
                elapsed_seconds=time.perf_counter() - install_started_at,
                result=command_result,
            )
            summary_rendered = True

            if frozen and apm_count > 0:
                # --frozen verifies lockfile structure, not content integrity.
                logger.info(
                    "Lockfile presence verified. Run 'apm audit' for on-disk content integrity.",
                    symbol="info",
                )

    except AgentPluginError as e:
        logger.error(str(e))
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except InsecureDependencyPolicyError as e:
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except InstallFailureAlreadyRendered as e:
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except AuthenticationError as e:
        logger.error(str(e))
        if e.diagnostic_context:
            logger.error_detail(e.diagnostic_context)
        logger.info("Tip: run 'apm doctor' to diagnose auth and connectivity.")
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except FrozenInstallError as e:
        logger.error(str(e))
        for reason in e.reasons:
            logger.error_detail(reason)
        logger.info(_frozen_install_tip(e))
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except DirectDependencyError as e:
        logger.error(str(e))
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    except click.UsageError:
        # Conflict matrix / argv parser raises UsageError -- let Click
        # render with exit code 2 and the standard "Usage: ..." prefix.
        raise
    except Exception as e:
        (logger or InstallLogger(verbose=verbose, dry_run=dry_run)).exception(
            f"Error installing dependencies: {e}"
        )
        command_result = (
            transaction.fail(e)
            if transaction is not None
            else InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=e)
        )
    finally:
        # Restore cwd before summary rendering, regardless of the exit path.
        # Always close the transaction even if root restoration fails.
        close_install_contexts(_root_redirect, transaction)
        if dry_run_manifest_tmp is not None:
            dry_run_manifest_tmp.cleanup()
        # F5 (#1116): render minimal elapsed-time line on exit paths that
        # did not already render the full install summary. Best-effort:
        # never let a render failure mask the original exception/exit.
        if not summary_rendered and logger is not None:
            with contextlib.suppress(Exception):
                elapsed = time.perf_counter() - install_started_at
                if (
                    command_result is not None
                    and command_result.disposition is InstallDisposition.FAILED
                ):
                    logger.install_failed(elapsed_seconds=elapsed)
                else:
                    logger.install_interrupted(elapsed_seconds=elapsed)
        # HACK(#852) cleanup: restore APM_VERBOSE so it stays scoped to this call.
        if _apm_verbose_prev is None:
            os.environ.pop("APM_VERBOSE", None)
        else:
            os.environ["APM_VERBOSE"] = _apm_verbose_prev

    if command_result is not None:
        from apm_cli.install.outcome import apply_install_command_outcome

        outcome = apply_install_command_outcome(command_result)
        ctx.exit(outcome.exit_code)


def _install_apm_packages(ctx, outcome):
    """Execute the APM + transitive MCP installation pipeline.

    Parses ``apm.yml``, installs APM dependencies, collects and installs
    transitive MCP servers, and handles lockfile updates.
    Args:
        ctx: :class:`InstallContext` with configuration and environment.
        outcome: ``_ValidationOutcome`` from package validation (may be
            ``None`` when no explicit packages were passed).
    Returns:
        Tuple of ``(apm_count, mcp_count, lsp_count, apm_diagnostics)``.
    """
    logger = ctx.logger

    logger.resolution_start(
        to_install_count=len(ctx.only_packages or []) if ctx.packages else 0,
        lockfile_count=0,  # Refined later inside _install_apm_dependencies
        update_count=len(outcome.updated_packages) if outcome is not None else 0,
    )

    try:
        apm_package = APMPackage.from_apm_yml(
            ctx.manifest_path,
            create_config=ctx.create_config,
        )
        if ctx.dry_run and outcome is not None and outcome.prospective_package is not None:
            apm_package = outcome.prospective_package
    except click.UsageError:
        raise
    except Exception as e:
        logger.error(f"Failed to parse {ctx.manifest_display}: {e}")
        raise InstallFailureAlreadyRendered("Failed to parse install manifest") from e

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    prod_mcp_deps = apm_package.get_mcp_dependencies()
    dev_mcp_deps = apm_package.get_dev_mcp_dependencies()
    mcp_deps = apm_package.get_all_mcp_dependencies()
    if not isinstance(mcp_deps, builtins.list):
        logger.verbose_detail("MCP dependencies were not a list; defaulting to empty")
        mcp_deps = []

    logger.verbose_detail(
        f"Parsed {APM_YML_FILENAME}: {len(apm_deps)} APM deps, "
        f"{len(prod_mcp_deps)} MCP deps"
        + (f", {len(dev_apm_deps)} dev APM deps" if dev_apm_deps else "")
        + (f", {len(dev_mcp_deps)} dev MCP deps" if dev_mcp_deps else "")
    )

    has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)

    all_apm_deps = list(apm_deps) + list(dev_apm_deps)

    if ctx.frozen is True:
        from apm_cli.install.request import InstallRequest
        from apm_cli.install.service import InstallService

        InstallService.enforce_frozen(
            InstallRequest(
                apm_package=apm_package,
                frozen=True,
                scope=ctx.scope,
                trust_transitive_mcp=ctx.trust_transitive_mcp,
            )
        )

    # Determine what to install based on install mode
    should_install_apm = ctx.install_mode != InstallMode.MCP
    should_install_mcp = ctx.install_mode != InstallMode.APM

    if ctx.dry_run:
        prospective_plan = ProspectiveInstallPlan.from_apm_package(
            apm_package,
            should_install_apm=should_install_apm,
            should_install_mcp=should_install_mcp,
            only_packages=ctx.only_packages,
            updated_packages=outcome.updated_packages if outcome is not None else (),
        )
        prospective_plan = prospective_plan.with_allowed_lsp_dependencies(apm_package, logger)
        logger.record_dry_run_apm_updates(len(prospective_plan.updated_apm_identities))
        _check_insecure_dependencies(
            prospective_plan.selected_apm_dependencies,
            ctx.allow_insecure,
            logger,
        )
        from apm_cli.install.template import preflight_agent_plugin_dry_run

        if should_install_apm:
            preflight_agent_plugin_dry_run(
                ctx,
                list(prospective_plan.selected_apm_dependencies),
                apm_package=apm_package,
            )
        from apm_cli.policy.install_preflight import run_policy_preflight as _dr_preflight

        _dr_preflight(
            project_root=ctx.project_root,
            apm_deps=list(prospective_plan.selected_apm_dependencies),
            mcp_deps=list(prospective_plan.selected_mcp_dependencies) or None,
            no_policy=ctx.no_policy,
            logger=logger,
            dry_run=True,
        )

        from apm_cli.install.presentation.dry_run import render_and_exit

        render_and_exit(
            logger=logger,
            plan=prospective_plan,
            update=ctx.update,
            apm_dir=ctx.apm_dir,
        )
        return (*prospective_plan.dependency_counts, None)

    _check_insecure_dependencies(all_apm_deps, ctx.allow_insecure, logger)

    # Install APM dependencies first (if requested)
    apm_count = 0

    # Migrate legacy apm.lock -> apm.lock.yaml if needed (one-time, transparent)
    migrate_lockfile_if_needed(ctx.apm_dir)

    # Capture old MCP servers and configs from lockfile BEFORE
    # _install_apm_dependencies regenerates it (which drops the fields).
    # We always read this -- even when --only=apm -- so we can restore the
    # field after the lockfile is regenerated by the APM install step.
    old_mcp_servers: builtins.set = builtins.set()
    old_mcp_configs: builtins.dict = {}
    old_mcp_provenance: builtins.dict = {}
    old_mcp_target_servers: builtins.dict = {}
    old_mcp_target_servers_present = True
    _lock_path = get_lockfile_path(ctx.apm_dir)
    _existing_lock = LockFile.read(_lock_path)
    if _existing_lock:
        old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
        old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)
        old_mcp_provenance = builtins.dict(_existing_lock.mcp_config_provenance)
        old_mcp_target_servers = builtins.dict(_existing_lock.mcp_target_servers)
        old_mcp_target_servers_present = _existing_lock._mcp_target_servers_present

    # Enter the APM install path when there are deps, local .apm/ primitives
    # (#714), OR orphan deps in the lockfile to clean up (manifest emptied).
    from apm_cli.core.scope import get_deploy_root as _get_deploy_root
    from apm_cli.deps.lockfile import _SELF_KEY as _LOCK_SELF_KEY

    _cli_project_root = _get_deploy_root(ctx.scope)
    _has_orphan_deps_in_lock = bool(
        _existing_lock
        and not has_any_apm_deps
        and any(k != _LOCK_SELF_KEY for k in _existing_lock.dependencies)
    )
    apm_diagnostics = None
    install_result = None
    if should_install_apm and (
        has_any_apm_deps
        or _project_has_root_primitives(_cli_project_root)
        or _has_orphan_deps_in_lock
    ):
        if not APM_DEPS_AVAILABLE:
            logger.error("APM dependency system not available")
            logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
            raise InstallFailureAlreadyRendered("APM dependency system not available")

        try:
            # If specific packages were requested, only install those
            # Otherwise install all from apm.yml.
            # `only_packages` was computed above so the dry-run preview
            # and the actual install share one canonical list.
            install_result = _install_apm_dependencies(
                apm_package,
                ctx.update,
                ctx.verbose,
                ctx.only_packages,
                force=ctx.force,
                parallel_downloads=ctx.parallel_downloads,
                logger=logger,
                scope=ctx.scope,
                auth_resolver=ctx.auth_resolver,
                target=ctx.target or ctx.runtime,
                allow_insecure=ctx.allow_insecure,
                allow_insecure_hosts=ctx.allow_insecure_hosts,
                marketplace_provenance=(
                    outcome.marketplace_provenance if ctx.packages and outcome else None
                ),
                protocol_pref=ctx.protocol_pref,
                allow_protocol_fallback=ctx.allow_protocol_fallback,
                no_policy=ctx.no_policy,
                audit_override=ctx.audit_override,
                legacy_skill_paths=ctx.legacy_skill_paths,
                trust_transitive_mcp=ctx.trust_transitive_mcp,
                frozen=ctx.frozen,
                plan_callback=ctx.plan_callback,
                skill_subset=ctx.skill_subset,
                skill_subset_from_cli=ctx.skill_subset_from_cli,
                refresh=ctx.refresh,
                trust_bin=ctx.trust_bin,
                transaction=ctx.transaction,
            )
            if not isinstance(install_result, InstallResult):
                install_result = InstallResult(
                    installed_count=int(getattr(install_result, "installed_count", 0)),
                    diagnostics=getattr(install_result, "diagnostics", None),
                )
            apm_count = install_result.installed_count
            apm_diagnostics = install_result.diagnostics
            ctx.target_decision = install_result.target_decision
            ctx.exec_allow_map = install_result.exec_allow_map
            ctx.exec_allow_resolved = install_result.exec_allow_resolved
            if install_result.disposition not in {
                InstallDisposition.SUCCESS,
                InstallDisposition.PARTIAL_SUCCESS,
            }:
                ctx.install_result = install_result
                return apm_count, 0, 0, apm_diagnostics
        except InsecureDependencyPolicyError:
            raise
        except AuthenticationError as e:
            # #1015: render auth diagnostics on the DEFAULT path (not --verbose).
            logger.error(str(e))
            if e.diagnostic_context:
                logger.error_detail(e.diagnostic_context)
            logger.info("Tip: run 'apm doctor' to diagnose auth and connectivity.")
            raise InstallFailureAlreadyRendered(str(e)) from e
        except FrozenInstallError as e:
            logger.error(str(e))
            for reason in e.reasons:
                logger.error_detail(reason)
            logger.info(_frozen_install_tip(e))
            raise InstallFailureAlreadyRendered(str(e)) from e
        except InstallFailureAlreadyRendered:
            raise
        except click.UsageError:
            raise
        except CopilotSettingsCollisionError as e:
            # Dependencies installed cleanly; only the Copilot settings merge
            # collided. Surface the already-actionable message verbatim (no
            # "Failed to install..." prefix) and keep the raw decoder detail,
            # when present, behind --verbose.
            logger.error(str(e))
            detail = getattr(e, "detail", None)
            if detail:
                logger.verbose_detail(detail)
            raise InstallFailureAlreadyRendered(str(e)) from e
        except Exception as e:
            # #832: surface PolicyViolationError verbatim (no double-nesting).
            msg = (
                str(e)
                if isinstance(e, PolicyViolationError)
                else f"Failed to install APM dependencies: {e}"
            )
            logger.error(msg)
            if not ctx.verbose:
                logger.progress("Run with --verbose for detailed diagnostics")
            raise InstallFailureAlreadyRendered(msg) from e
    elif should_install_apm and not has_any_apm_deps:
        logger.verbose_detail("No APM dependencies found in apm.yml")

    if ctx.update:
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()

    from apm_cli.install.service_integration import run_service_integrations
    from apm_cli.policy.install_preflight import PolicyBlockError

    try:
        service_result = run_service_integrations(
            ctx,
            apm_package=apm_package,
            mcp_deps=mcp_deps,
            lock_path=_lock_path,
            existing_lock=_existing_lock,
            old_mcp_servers=old_mcp_servers,
            old_mcp_configs=old_mcp_configs,
            old_mcp_provenance=old_mcp_provenance,
            old_mcp_target_servers=old_mcp_target_servers,
            old_mcp_target_servers_present=old_mcp_target_servers_present,
            diagnostics=apm_diagnostics,
            explicit_target=ctx.target or ctx.runtime,
            target_decision=ctx.target_decision,
        )
    except PolicyBlockError:
        logger.error(
            "MCP server(s) blocked by org policy. "
            "APM packages remain installed; MCP configs were NOT written."
        )
        logger.render_summary()
        raise InstallFailureAlreadyRendered("MCP server(s) blocked by org policy") from None
    except RequiredIntegrationError as exc:
        logger.error(str(exc))
        logger.render_summary()
        raise InstallFailureAlreadyRendered(str(exc)) from exc

    ctx.target_decision = service_result.target_decision
    mcp_count = service_result.mcp_count
    lsp_count = service_result.lsp_count

    # Local .apm/ integration already ran inside the package pipeline.

    from apm_cli.install.outcome import finalize_install_result

    command_result = install_result or InstallResult()
    command_result.installed_count = apm_count
    command_result.diagnostics = apm_diagnostics
    ctx.install_result = finalize_install_result(command_result, force=ctx.force)
    return apm_count, mcp_count, lsp_count, apm_diagnostics


def _post_install_summary(
    *,
    logger,
    apm_count,
    mcp_count,
    lsp_count=0,
    apm_diagnostics,
    force,
    elapsed_seconds=None,
    result=None,
):
    """Thin shim forwarding to :func:`apm_cli.install.summary.render_post_install_summary`.

    Kept as a module-level alias so existing tests that
    ``@patch("apm_cli.commands.install._post_install_summary")`` continue
    to work after the extraction (microsoft/apm#1116, F5).
    """
    from apm_cli.install.summary import render_post_install_summary

    return render_post_install_summary(
        logger=logger,
        apm_count=apm_count,
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        apm_diagnostics=apm_diagnostics,
        force=force,
        elapsed_seconds=elapsed_seconds,
        result=result,
    )


# ---------------------------------------------------------------------------
# Install engine
# ---------------------------------------------------------------------------


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
    only_packages: "builtins.list | None" = None,
    **options: Any,
) -> InstallResult:
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
        **options,
    )
    return InstallService().run(request)
