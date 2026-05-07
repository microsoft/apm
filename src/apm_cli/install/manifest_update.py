"""Manifest update helpers for the install command."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.core.command_logger import _ValidationOutcome
from apm_cli.install.gitlab_resolver import (
    _try_resolve_gitlab_direct_shorthand as _default_try_resolve_gitlab_direct_shorthand,
)
from apm_cli.install.insecure_policy import (
    _format_insecure_dependency_requirements,
    _get_insecure_dependency_url,
)
from apm_cli.install.package_resolution import dependency_reference_to_yaml_entry
from apm_cli.install.validation import (
    _local_path_failure_reason as _default_local_path_failure_reason,
)
from apm_cli.install.validation import (
    _validate_package_exists as _default_validate_package_exists,
)
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.console import _rich_error


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


def _resolve_package_references_impl(
    packages,
    existing_identities,
    *,
    auth_resolver=None,
    logger=None,
    scope=None,
    allow_insecure=False,
    dependency_reference_cls=DependencyReference,
    validate_package_exists=_default_validate_package_exists,
    local_path_failure_reason=_default_local_path_failure_reason,
    try_resolve_gitlab_direct_shorthand=_default_try_resolve_gitlab_direct_shorthand,
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
    _misconfig_risks = {}
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
        marketplace_dep_ref = None
        if "/" not in package and not dependency_reference_cls.is_local_path(package):
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
                        warning_handler = lambda msg: logger.warning(msg)  # noqa: E731
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
                    canonical_str, resolved_plugin = resolution  # noqa: RUF059
                    if logger:
                        logger.verbose_detail(f"    Resolved to: {canonical_str}")
                    marketplace_provenance = {
                        "discovered_via": marketplace_name,
                        "marketplace_plugin_name": plugin_name,
                    }
                    package = canonical_str
                    marketplace_dep_ref = getattr(resolution, "dependency_reference", None)
                    risk = getattr(resolution, "cross_repo_misconfig_risk", None)
                    if risk is not None:
                        _misconfig_risks[canonical_str] = (
                            marketplace_name,
                            plugin_name,
                            risk,
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
            dep_ref = marketplace_dep_ref or dependency_reference_cls.parse(package)
            if dependency_reference_cls.needs_gitlab_direct_shorthand_probing(package, dep_ref):
                resolved = try_resolve_gitlab_direct_shorthand(
                    package,
                    auth_resolver,
                    verbose=bool(logger and logger.verbose),
                )
                if resolved is None:
                    raise ValueError(
                        "Direct GitLab host/path did not resolve to a reachable repository "
                        "with an installable package path. Use an explicit 'git' URL with "
                        "a 'path' field for a deeper project or subdirectory."
                    )
                dep_ref = resolved
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
            if marketplace_dep_ref is not None or (dep_ref.is_virtual and dep_ref.virtual_path):
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

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        verbose = bool(logger and logger.verbose)
        if validate_package_exists(
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
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = local_path_failure_reason(dep_ref)
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
            risk_entry = _misconfig_risks.get(package)
            if risk_entry is not None and logger:
                marketplace_name, plugin_name, risk = risk_entry
                logger.warning(
                    f"'{plugin_name}@{marketplace_name}' is registered on "
                    f"'{risk.marketplace_host}' but the plugin's bare "
                    f"`repo: {risk.bare_repo_field}` resolved to "
                    "'github.com'. If you meant the enterprise host, set "
                    f"the plugin's `repo` field to '{risk.suggested_qualified_repo}' "
                    "in marketplace.json. If this is intentionally a github.com "
                    "dependency, verify your github.com credentials and that the "
                    "repository is accessible."
                )

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

    migrated_count = 0
    for index, dep_entry in enumerate(current_deps):
        if isinstance(dep_entry, str) and dep_entry in apm_yml_entries:
            replacement = apm_yml_entries[dep_entry]
            if replacement != dep_entry:
                current_deps[index] = replacement
                migrated_count += 1

    # Update dependencies
    data[dep_section]["apm"] = current_deps

    # Write back to apm.yml
    try:
        from ..utils.yaml_io import dump_yaml

        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(
                f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)"
            )
            if migrated_count:
                logger.verbose_detail(
                    f"Migrated {migrated_count} existing package reference(s) to object form"
                )
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)


def _validate_and_add_packages_to_apm_yml_impl(
    packages,
    dry_run=False,
    dev=False,
    logger=None,
    manifest_path=None,
    auth_resolver=None,
    scope=None,
    allow_insecure=False,
    dependency_reference_cls=DependencyReference,
    validate_package_exists=_default_validate_package_exists,
    local_path_failure_reason=_default_local_path_failure_reason,
    try_resolve_gitlab_direct_shorthand=_default_try_resolve_gitlab_direct_shorthand,
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
    ) = _resolve_package_references_impl(
        packages,
        existing_identities,
        auth_resolver=auth_resolver,
        logger=logger,
        scope=scope,
        allow_insecure=allow_insecure,
        dependency_reference_cls=dependency_reference_cls,
        validate_package_exists=validate_package_exists,
        local_path_failure_reason=local_path_failure_reason,
        try_resolve_gitlab_direct_shorthand=try_resolve_gitlab_direct_shorthand,
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
        if _apm_yml_entries and not dry_run:
            _merge_packages_into_yml(
                [],
                _apm_yml_entries,
                current_deps,
                data,
                dep_section,
                apm_yml_path,
                dev=dev,
                logger=logger,
            )
        if dry_run:
            if logger:
                logger.progress("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them
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
