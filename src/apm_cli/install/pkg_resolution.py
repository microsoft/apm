"""Package validation and manifest update helpers for the APM install command."""

import builtins
import sys

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.install.artifactory_resolver import _resolve_artifactory_boundary
from apm_cli.install.insecure_policy import (
    _format_insecure_dependency_requirements,
    _get_insecure_dependency_url,
)
from apm_cli.install.package_resolution import (
    dependency_reference_to_yaml_entry,
    update_existing_dependency_entry_if_needed,
)
from apm_cli.install.validation import _local_path_failure_reason


def _check_package_conflicts(current_deps):
    """Build identity set from existing deps for duplicate detection.

    Parses each entry in *current_deps* (string or dict form) through
    :class:`DependencyReference` and collects identity strings.

    Returns:
        ``set`` of identity strings for existing dependencies.
    """
    # RULE B: DependencyReference is patched at apm_cli.commands.install.* in tests.
    import apm_cli.commands.install as _m

    DependencyReference = _m.DependencyReference

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
    # RULE B: DependencyReference and _validate_package_exists are patched at
    # apm_cli.commands.install.* in tests that call this function directly.
    import apm_cli.commands.install as _m

    DependencyReference = _m.DependencyReference
    _vpe = _m._validate_package_exists

    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}
    _apm_yml_entries = {}  # canonical -> apm.yml entry (str or dict for HTTP deps)
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
        if "/" not in package and not DependencyReference.is_local_path(package):
            try:
                from apm_cli.marketplace.resolver import (
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
            dep_ref, direct_virtual_resolved = _m.resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=_m._try_resolve_gitlab_direct_shorthand,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
                auth_resolver=auth_resolver,
                verbose=bool(logger and logger.verbose),
                logger=logger,
            )
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
            # Attach --skill filter so to_apm_yml_entry() emits the dict form
            if skill_subset:
                # Normalize: strip whitespace, drop empty strings, deduplicate
                # (preserve order) so invalid or redundant names can't persist.
                _seen: builtins.set[str] = builtins.set()
                _normalized: builtins.list[str] = []
                for _s in skill_subset:
                    _s = _s.strip()
                    if _s and _s not in _seen:
                        _seen.add(_s)
                        _normalized.append(_s)
                dep_ref.skill_subset = _normalized
            if marketplace_dep_ref is not None or direct_virtual_resolved:
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

        scope_reject = _m.user_scope_rejection_reason(dep_ref, scope)
        if scope_reject:
            invalid_outcomes.append((package, scope_reject))
            if logger:
                logger.validation_fail(package, scope_reject)
            continue

        # Ensure structured entry is used for apm.yml persistence when skill
        # filter is active (normal non-marketplace/non-insecure path doesn't
        # set _apm_yml_entries; _merge_packages_into_yml falls back to the
        # plain canonical string without this).
        if skill_subset and canonical not in _apm_yml_entries:
            _apm_yml_entries[canonical] = dep_ref.to_apm_yml_entry()

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        verbose = bool(logger and logger.verbose)
        if _vpe(
            package,
            verbose=verbose,
            auth_resolver=auth_resolver,
            logger=logger,
            dep_ref=dep_ref,
        ):
            updates_existing_entry = update_existing_dependency_entry_if_needed(
                current_deps,
                already_in_deps=already_in_deps,
                apm_yml_entries=_apm_yml_entries,
                canonical=canonical,
                dep_ref=dep_ref,
                identity=identity,
                dependency_reference_cls=DependencyReference,
                logger=logger,
            )
            valid_outcomes.append((canonical, already_in_deps))
            if logger:
                logger.validation_pass(canonical, already_in_deps, updates_existing_entry)

            if not already_in_deps:
                validated_packages.append(canonical)
                existing_identities.add(identity)  # prevent duplicates within batch
            dependencies_changed = dependencies_changed or updates_existing_entry
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = _local_path_failure_reason(dep_ref)
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
    # RULE B: _rich_error is patched at apm_cli.commands.install.* in tests.
    import apm_cli.commands.install as _m

    dep_label = "devDependencies" if dev else "apm.yml"
    for package in validated_packages:
        current_deps.append(apm_yml_entries.get(package, package))
        if logger:
            logger.verbose_detail(f"Added {package} to {dep_label}")

    # Update dependencies
    data[dep_section]["apm"] = current_deps

    # Write back to apm.yml
    try:
        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(
                f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)"
            )
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _m._rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
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
    skill_subset=None,
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

    # RULE B: _check_package_conflicts, _resolve_package_references, _merge_packages_into_yml,
    # and _rich_error are all patched at apm_cli.commands.install.* in tests.
    import apm_cli.commands.install as _m

    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)

    # Read current apm.yml
    try:
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path) or {}
    except Exception as e:
        if logger:
            logger.error(f"Failed to read {APM_YML_FILENAME}: {e}")
        else:
            _m._rich_error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    # Ensure dependencies structure exists
    dep_section = "devDependencies" if dev else "dependencies"
    if dep_section not in data:
        data[dep_section] = {}
    if "apm" not in data[dep_section]:
        data[dep_section]["apm"] = []

    current_deps = data[dep_section]["apm"] or []

    # Detect duplicates against existing deps
    existing_identities = _m._check_package_conflicts(current_deps)

    # Validate and canonicalize all package references
    (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
        dependencies_changed,
    ) = _m._resolve_package_references(
        packages,
        current_deps,
        existing_identities,
        auth_resolver=auth_resolver,
        logger=logger,
        scope=scope,
        allow_insecure=allow_insecure,
        skill_subset=skill_subset,
    )

    outcome = _m._ValidationOutcome(
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
        persist_dependency_list_if_changed = _m.persist_dependency_list_if_changed
        persist_dependency_list_if_changed(
            dependencies_changed=dependencies_changed,
            data=data,
            dep_section=dep_section,
            current_deps=current_deps,
            apm_yml_path=apm_yml_path,
            apm_yml_filename=APM_YML_FILENAME,
            logger=logger,
            rich_error=_m._rich_error,
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
    _m._merge_packages_into_yml(
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
