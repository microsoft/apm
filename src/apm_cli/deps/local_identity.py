"""The one owner of a local dependency's node identity (#4731).

A relative ``local_path`` names a directory; one directory is one dependency.
The install resolver, the ``--frozen`` local-content verdict and the
reachability walk behind ``audit --ci``, ``uninstall`` and ``prune`` all key a
declared local edge through :func:`transitive_local_identity`, so they agree
on one lockfile row per anchored directory.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from ..models.dependency.reference import DependencyReference
from ..utils.paths import portable_relpath


def portable_anchor_identity(anchored: Path, base: Path | None) -> str:
    """Return a machine-independent identity string for a resolved local dep.

    ``anchored`` is the absolute, resolved on-disk location of a local
    package. The returned value is the package's stable identity only --
    it feeds the ``local:`` cycle key, the lockfile
    ``dependencies[].anchored_local_path`` field, and (via that same owner
    identity) the deployment-ledger owner rows. It is never re-opened as a
    filesystem path.

    A lockfile is a committed, cross-machine artifact, so this identity
    MUST NOT carry a developer's home directory or any absolute prefix.
    Packages inside ``base`` (the project root) are recorded relative to
    it in forward-slash POSIX form -- matching how directly-declared local
    owners already serialize -- which keeps a regenerated lockfile
    deterministic across machines and CI. Packages outside ``base`` (or
    when ``base`` is unknown) fall back to the resolved absolute POSIX
    path: such out-of-project local deps are inherently non-committable,
    and the fallback preserves a unique identity without inventing one.
    """
    if base is None:
        return anchored.resolve().as_posix()
    return portable_relpath(anchored, base)


def root_local_dirs(
    root_refs: Iterable[DependencyReference], consumer_root: Path
) -> dict[Path, DependencyReference]:
    """Map each root-declared local package's resolved directory to its ref."""
    dirs: dict[Path, DependencyReference] = {}
    for ref in root_refs:
        if ref.is_local and ref.local_path and ref.replacement is None:
            raw = Path(ref.local_path).expanduser()
            anchored = raw.resolve() if raw.is_absolute() else (consumer_root / raw).resolve()
            dirs.setdefault(anchored, ref)
    return dirs


def transitive_local_identity(
    sub_dep: DependencyReference,
    *,
    declaring_parent: str,
    anchored: Path,
    root: Path | None,
    root_local_dirs: dict[Path, DependencyReference],
) -> DependencyReference:
    """Return the node identity of a local edge a package declares.

    When the root manifest also declares the anchored directory, the edge
    takes the root declaration's identity and spelling, however the declaring
    package spells its relative path (``../lib`` vs ``./packages/lib``).
    Otherwise the edge is keyed by its anchored directory.
    """
    root_ref = root_local_dirs.get(anchored)
    if root_ref is not None:
        return replace(sub_dep, repo_url=root_ref.repo_url, local_path=root_ref.local_path)
    return replace(
        sub_dep,
        declaring_parent=declaring_parent,
        anchored_local_path=portable_anchor_identity(anchored, root),
    )


def with_declaring_edge(
    node_ref: DependencyReference, edge_ref: DependencyReference
) -> DependencyReference:
    """*node_ref* carrying the local declaration of *edge_ref*, its recorded parent's edge.

    A diamond's node keeps the first edge the walk met; once the resolver picks
    the recorded parent by a stable rule (#5386), ``declaring_parent`` and the
    ``local_path`` spelled relative to it must come from that parent's edge too,
    so ``resolved_by`` and ``declaring_parent`` name one parent. The anchored
    directory, and so the node identity, is the same for every such edge.

    The package was already copied to the slot the first edge names (its
    anchored identity and ``local_path`` directory name); an edge naming
    another directory (an alias spelling) leaves *node_ref* as it is.
    """
    if (
        not (node_ref.is_local and node_ref.local_path and edge_ref.local_path)
        or not edge_ref.declaring_parent
        or node_ref.anchored_local_path != edge_ref.anchored_local_path
        or Path(node_ref.local_path).name != Path(edge_ref.local_path).name
    ):
        return node_ref
    reparented = replace(
        node_ref, declaring_parent=edge_ref.declaring_parent, local_path=edge_ref.local_path
    )
    # Keep the non-field state download callbacks annotate (``resolved_reference``).
    for name, value in vars(node_ref).items():
        if name not in vars(reparented):
            setattr(reparented, name, value)
    return reparented
