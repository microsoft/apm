"""Remote identity and commit of a remote package's same-repository dependency (#5525).

A remote package may list files of its own repository (``./prompts/x.prompt.md``).
The resolver expands each into a virtual dependency on the parent's repository.
That child must come from the same snapshot as its parent: it is fetched at the
commit the parent resolved at, so the commit its lock row records is the commit
its bytes came from. Only when no full parent commit is known does the child
keep the parent's selector, and its row then stays unpinned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .revision_pins import is_full_revision_pin

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference
    from .lockfile import LockFile


def inherit_remote_parent_fields(
    parent_dep: DependencyReference,
    child_dep: DependencyReference,
    *,
    virtual_path: str | None,
    reference: str | None,
    is_virtual: bool,
) -> DependencyReference:
    """Return *child_dep* with remote identity inherited from *parent_dep*."""
    return replace(
        child_dep,
        repo_url=parent_dep.repo_url,
        host=parent_dep.host,
        host_type=parent_dep.host_type,
        port=parent_dep.port,
        explicit_scheme=parent_dep.explicit_scheme,
        ssh_user=parent_dep.ssh_user,
        ado_organization=parent_dep.ado_organization,
        ado_project=parent_dep.ado_project,
        ado_repo=parent_dep.ado_repo,
        artifactory_prefix=parent_dep.artifactory_prefix,
        is_insecure=parent_dep.is_insecure,
        allow_insecure=parent_dep.allow_insecure,
        source=parent_dep.source,
        registry_name=None,
        reference=reference,
        virtual_path=virtual_path,
        is_virtual=is_virtual,
        is_parent_repo_inheritance=False,
        is_local=False,
        local_path=None,
    )


def _full_commit(resolved: Any) -> str | None:
    commit = getattr(resolved, "resolved_commit", None)
    return commit.strip().lower() if is_full_revision_pin(commit) else None


def same_repo_child_reference(
    parent_dep: DependencyReference,
    existing_lockfile: LockFile | None,
    *,
    refresh: Callable[[DependencyReference], Any] | None = None,
) -> str | None:
    """Return the ref a same-repository child of *parent_dep* is fetched at.

    In order: the commit the parent was fetched at in this run; under an
    update that selects the parent (*refresh* given), the commit its selector
    resolves to now, recorded on the parent so its own fetch reuses it; else
    the parent lock row's commit while the parent's selector (a ref, or a
    semver range) is unchanged.
    Otherwise the parent's selector, so the child stays unpinned.
    """
    commit = _full_commit(getattr(parent_dep, "resolved_reference", None))
    if commit:
        return commit
    if refresh is not None:
        if is_full_revision_pin(parent_dep.reference):
            return parent_dep.reference.strip().lower()
        try:
            resolved = refresh(parent_dep)
        except Exception:
            return parent_dep.reference
        commit = _full_commit(resolved)
        if commit:
            parent_dep.resolved_reference = resolved
        return commit or parent_dep.reference
    locked = (
        existing_lockfile.get_dependency(parent_dep.get_unique_key())
        if existing_lockfile is not None
        else None
    )
    if (
        locked is not None
        and locked.source not in {"local", "registry"}
        and (
            parent_dep.reference == locked.resolved_ref
            or (locked.constraint is not None and parent_dep.reference == locked.constraint)
        )
        and is_full_revision_pin(locked.resolved_commit)
    ):
        return locked.resolved_commit.strip().lower()
    return parent_dep.reference
