"""Helpers for install-time package reference resolution (structured apm.yml entries).

Extracted from ``apm_cli.commands.install`` to keep the command module smaller.
Call sites pass ``dependency_reference_cls`` and GitLab resolver callables so
tests that patch ``apm_cli.commands.install.DependencyReference`` and
``_try_resolve_gitlab_direct_shorthand`` keep working.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any

from apm_cli.install.gitlab_resolver import _GITLAB_DIRECT_SHORTHAND_UNRESOLVED

GIT_PARENT_USER_SCOPE_ERROR = (
    "git: parent dependencies are not supported at user scope. "
    "Use project scope or specify explicit git URL."
)


def dependency_reference_to_yaml_entry(dep_ref: Any) -> dict:
    """Serialize a structured dependency reference for ``apm.yml`` storage."""
    entry = {"git": dep_ref.to_github_url()}
    if dep_ref.virtual_path:
        entry["path"] = dep_ref.virtual_path
    if dep_ref.reference:
        entry["ref"] = dep_ref.reference
    if dep_ref.alias:
        entry["alias"] = dep_ref.alias
    return entry


def resolve_parsed_dependency_reference(
    package: str,
    marketplace_dep_ref: Any | None,
    *,
    dependency_reference_cls: Any,
    try_resolve_gitlab_direct_shorthand: Callable[..., Any],
    auth_resolver: Any,
    verbose: bool,
) -> tuple[Any, bool]:
    """Parse or probe *package* into a ``DependencyReference``.

    Returns ``(dep_ref, direct_gitlab_virtual_resolved)`` where the second flag
    is True when GitLab direct shorthand probing produced a virtual path entry.

    Raises:
        ValueError: When GitLab shorthand probing is required but fails to resolve.
    """
    dep_ref = (
        marketplace_dep_ref
        if marketplace_dep_ref is not None
        else dependency_reference_cls.parse(package)
    )
    if (
        marketplace_dep_ref is None
        and dependency_reference_cls.needs_gitlab_direct_shorthand_probing(package, dep_ref)
    ):
        resolved = try_resolve_gitlab_direct_shorthand(
            package,
            auth_resolver,
            verbose=verbose,
        )
        if resolved is None:
            raise ValueError(_GITLAB_DIRECT_SHORTHAND_UNRESOLVED)
        dep_ref = resolved
        direct_gitlab_virtual_resolved = bool(dep_ref.is_virtual and dep_ref.virtual_path)
        return dep_ref, direct_gitlab_virtual_resolved
    return dep_ref, False


def user_scope_rejection_reason(dep_ref: Any, scope: Any) -> str | None:
    """Return a validation-fail reason if *dep_ref* is invalid at user scope.

    Per #937, only relative local paths are rejected at user scope -- absolute
    local paths are unambiguous and flow through the same _copy_local_package
    code path as project scope.
    """
    if scope is None:
        return None
    from pathlib import Path

    from apm_cli.core.scope import InstallScope

    if dep_ref.is_local and scope is InstallScope.USER:
        local_path = dep_ref.local_path or ""
        # Match the rest of the install pipeline (sources.py, phases/resolve.py)
        # which expanduser()s local paths before consuming them: `~/pkg` is
        # absolute after expansion and must NOT be rejected here.
        if not Path(local_path).expanduser().is_absolute():
            return (
                "relative local paths are not supported at user scope (--global). "
                "Use an absolute path or a remote reference (owner/repo) instead"
            )
    if dep_ref.is_parent_repo_inheritance and scope is InstallScope.USER:
        return GIT_PARENT_USER_SCOPE_ERROR
    return None


def merge_structured_entry_into_current_deps(
    current_deps: builtins.list,
    structured_entry: dict,
    identity: str,
    canonical: str,
    *,
    dependency_reference_cls: Any,
    logger: Any = None,
) -> None:
    """Replace or append *structured_entry* in *current_deps* by *identity*."""
    replaced = False
    for idx, dep_entry in enumerate(current_deps):
        try:
            if isinstance(dep_entry, builtins.str):
                existing_ref = dependency_reference_cls.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                existing_ref = dependency_reference_cls.parse_from_dict(dep_entry)
            else:
                continue
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if existing_ref.get_identity() == identity:
            current_deps[idx] = structured_entry
            replaced = True
            if logger:
                logger.verbose_detail(
                    f"Updated existing dependency entry to structured git+path form: {canonical}"
                )
            break
    if not replaced:
        current_deps.append(structured_entry)


def persist_dependency_list_if_changed(
    *,
    dependencies_changed: bool,
    data: dict,
    dep_section: str,
    current_deps: builtins.list,
    apm_yml_path: Any,
    apm_yml_filename: str,
    logger: Any = None,
    rich_error: Callable[[str], None],
    sys_exit: Callable[[int], None],
) -> None:
    """Write *apm.yml* when *current_deps* was updated without new packages."""
    if not dependencies_changed:
        return
    data[dep_section]["apm"] = current_deps
    try:
        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(f"Updated {apm_yml_filename} to preserve marketplace subdirectory entry")
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {apm_yml_filename}: {e}")
        else:
            rich_error(f"Failed to write {apm_yml_filename}: {e}")
        sys_exit(1)
