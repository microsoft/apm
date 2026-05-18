"""Package accessibility validation helpers.

Extracted from package_resolver to keep that module under 400 lines.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class _PackageAccessCtx:
    """Bundled arguments for :func:`_validate_package_accessibility`."""

    package: Any
    dep_ref: Any
    canonical: Any
    identity: Any
    already_in_deps: bool
    validated_packages: list
    existing_identities: Any
    valid_outcomes: list
    marketplace_provenance: Any
    _marketplace_provenance: dict
    _apm_yml_entries: dict
    current_deps: Any
    misconfig_risk: Any
    auth_resolver: Any
    logger: Any


def _build_inaccessible_reason(dep_ref: Any, verbose: bool) -> str:
    """Build a human-readable reason string for an inaccessible package."""
    reason = sys.modules["apm_cli.commands.install"]._local_path_failure_reason(dep_ref)
    if reason:
        return reason
    is_subdir_ref_chain = (
        dep_ref.is_virtual and dep_ref.is_virtual_subdirectory() and bool(dep_ref.reference)
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
    return reason


def _warn_misconfig_risk(misconfig_risk: Any, logger: Any) -> None:
    """Emit a warning about a suspected enterprise-host mis-route (if applicable)."""
    if misconfig_risk is None or not logger:
        return
    _mp_name, _plugin_name, _risk = misconfig_risk
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


def _record_invalid_package_err(
    package: Any, error: Any, invalid_outcomes: list, logger: Any
) -> None:
    """Append to invalid_outcomes and emit an error log line (no package prefix)."""
    invalid_outcomes.append((package, error))
    if logger:
        logger.error(error)


def _record_invalid_package(package: Any, error: Any, invalid_outcomes: list, logger: Any) -> None:
    """Append to invalid_outcomes and emit a validation_fail log line."""
    invalid_outcomes.append((package, error))
    if logger:
        logger.validation_fail(package, error)


def _validate_package_accessibility(ctx: _PackageAccessCtx):
    """Validate package exists and is accessible.

    Returns tuple of (validation_success, dependencies_changed, error_reason).
    """
    from apm_cli.install.package_resolution import merge_structured_entry_into_current_deps

    package = ctx.package
    dep_ref = ctx.dep_ref
    canonical = ctx.canonical
    identity = ctx.identity
    already_in_deps = ctx.already_in_deps
    validated_packages = ctx.validated_packages
    existing_identities = ctx.existing_identities
    valid_outcomes = ctx.valid_outcomes
    marketplace_provenance = ctx.marketplace_provenance
    _marketplace_provenance = ctx._marketplace_provenance
    _apm_yml_entries = ctx._apm_yml_entries
    current_deps = ctx.current_deps
    misconfig_risk = ctx.misconfig_risk
    auth_resolver = ctx.auth_resolver
    logger = ctx.logger
    dependencies_changed = False

    verbose = bool(logger and logger.verbose)
    if sys.modules["apm_cli.commands.install"]._validate_package_exists(
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
                dependency_reference_cls=sys.modules[
                    "apm_cli.commands.install"
                ].DependencyReference,
                logger=logger,
            )
            dependencies_changed = True
        if marketplace_provenance:
            _marketplace_provenance[identity] = marketplace_provenance
        return True, dependencies_changed, None

    verbose = bool(logger and logger.verbose)
    reason = _build_inaccessible_reason(dep_ref, verbose)
    _warn_misconfig_risk(misconfig_risk, logger)
    return False, dependencies_changed, reason
