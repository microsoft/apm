"""The one owner of a local dependency's node identity (#4731).

A relative ``local_path`` names a directory; one directory is one dependency.
The install resolver, the ``--frozen`` local-content verdict and the
reachability walk behind ``audit --ci``, ``uninstall`` and ``prune`` all key a
declared local edge through :func:`transitive_local_identity`, so they agree
on one lockfile row per anchored directory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from ..models.dependency.reference import DependencyReference
from ..utils.paths import portable_relpath, relative_posix, spelling_key, stored_spelling
from ..utils.realpath import resolve_path

_T = TypeVar("_T")


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


def stored_directory(anchored: Path, root: Path | None) -> Path:
    """*anchored* spelled as the volume stores it: one directory, one spelling (#5424).

    A case-folding or normalizing volume (macOS, Windows) opens ``F-Pkg`` as
    ``f-pkg`` and an NFD name as its NFC form, but ``Path.resolve`` keeps the
    spelling it was given. Each component below the project *root* takes the
    stored entry (:func:`apm_cli.utils.paths.stored_spelling`): an exact entry
    always wins, so a path spelled as stored keeps its identity, and a folded
    one must be confirmed by ``samefile``, so distinct directories on a
    case-sensitive volume never merge. *anchored* is already resolved (no
    links); a directory outside *root*, or a missing or ambiguous entry,
    keeps *anchored*: nothing outside the project is listed. Cost: one
    directory listing per component below *root*, plus one ``samefile`` per
    folded component.
    """
    if root is None:
        return anchored
    try:
        base = resolve_path(root)
    except (OSError, RuntimeError):
        return anchored
    rest = relative_posix(anchored, base)
    if not rest or rest == "." or (os.sep != "\\" and "\\" in rest):
        # A POSIX name may hold a literal backslash, which is not a separator.
        return anchored
    stored = stored_spelling(base, rest)
    return base / stored if stored is not None and stored != rest else anchored


def _below_root(anchor: str) -> bool:
    """Whether an ``anchored_local_path`` is project-relative (not an out-of-project path)."""
    parts = PurePosixPath(anchor).parts
    return (
        bool(parts)
        and not PurePosixPath(anchor).is_absolute()
        and ".." not in parts
        and ":" not in parts[0]
    )


def locked_spelling(
    ref: DependencyReference, locked: Mapping[str, Any], root: Path | None
) -> DependencyReference:
    """*ref* keyed as a pre-#5424 lock row that names its directory another way.

    Before :func:`stored_directory`, a transitive row kept the declared
    spelling (``vendor/f-pkg`` for a stored ``F-Pkg``). When *ref*'s key is not
    locked, a row whose ``anchored_local_path`` has the same caseless key and
    is the same directory below *root* (``samefile``) names it, so ``install
    --frozen`` accepts such a lock, read-only, until a plain install re-keys
    it. A case-sensitive volume never confirms two spellings. Scans the rows
    only on a miss.
    """
    anchor = ref.anchored_local_path
    if not anchor or root is None or ref.get_unique_key() in locked or not _below_root(anchor):
        return ref
    want = spelling_key(anchor)
    for row in locked.values():
        legacy = getattr(row, "anchored_local_path", None)
        if not legacy or legacy == anchor or spelling_key(legacy) != want:
            continue
        if not _below_root(legacy):
            continue
        try:
            if os.path.samefile(root / legacy, root / anchor):
                return replace(ref, anchored_local_path=legacy)
        except OSError:
            continue
    return ref


def root_local_dirs(
    root_refs: Iterable[DependencyReference], consumer_root: Path
) -> dict[Path, DependencyReference]:
    """Map each root-declared local package's resolved directory to its ref."""
    dirs: dict[Path, DependencyReference] = {}
    for ref in root_refs:
        if ref.is_local and ref.local_path and ref.replacement is None:
            raw = Path(ref.local_path).expanduser()
            anchored = raw.resolve() if raw.is_absolute() else (consumer_root / raw).resolve()
            dirs.setdefault(stored_directory(anchored, consumer_root), ref)
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
    Otherwise the edge is keyed by its anchored directory, spelled as the
    volume stores it (:func:`stored_directory`, #5424).
    """
    anchored = stored_directory(anchored, root)
    root_ref = root_local_dirs.get(anchored)
    if root_ref is not None:
        return replace(sub_dep, repo_url=root_ref.repo_url, local_path=root_ref.local_path)
    return replace(
        sub_dep,
        declaring_parent=declaring_parent,
        anchored_local_path=portable_anchor_identity(anchored, root),
    )


def same_stored_leaf(first: Path, ranked: Path) -> bool:
    """Whether two declared spellings of a package directory are one directory entry.

    On a case-folding or normalizing volume ``shared/F-Pkg`` and ``shared/f-pkg``
    are one stored entry; on a case-sensitive volume ``vendor/f``, a symlink to
    ``vendor/F``, is its own entry (an alias, #5386). An exact entry always wins
    (:func:`apm_cli.utils.paths.stored_spelling`), so only a folded spelling
    maps to another one's entry.
    """
    stored = stored_spelling(first.parent, first.name)
    return stored is not None and stored == stored_spelling(ranked.parent, ranked.name)


def recorded_declaring_edge(
    first: _T,
    ranked: _T,
    local_path: Callable[[_T], str | None],
    same_entry: Callable[[_T, _T], bool] | None = None,
) -> _T:
    """The declaring edge a local diamond node records: the one rule (#5386).

    *first* is the shallowest edge the walk met, *ranked* the recorded parent's
    (nearest, lowest key: the parent ``resolved_by`` names). The node takes the
    ranked parent's edge, so ``resolved_by`` and ``declaring_parent`` name one
    parent whatever the manifest order. An edge that names another package
    directory than the first edge (an alias spelling) keeps the first edge:
    install has already copied the package to the first edge's slot. Install,
    ``apm prune`` and ``apm uninstall`` all record a diamond through this rule.
    A leaf that differs only in case or Unicode normalization is no alias when
    *same_entry* confirms the volume stores both spellings as one entry (#5457);
    without *same_entry* the leaves must match exactly.
    """
    first_path, ranked_path = local_path(first), local_path(ranked)
    if not first_path or not ranked_path:
        return first
    first_leaf, ranked_leaf = Path(first_path).name, Path(ranked_path).name
    if first_leaf == ranked_leaf:
        return ranked
    if (
        same_entry is not None
        and spelling_key(first_leaf) == spelling_key(ranked_leaf)
        and same_entry(first, ranked)
    ):
        return ranked
    return first


def _edges_one_entry(
    first_parent: Any, ranked_parent: Any
) -> Callable[[DependencyReference, DependencyReference], bool] | None:
    """:func:`same_stored_leaf` on two edges, each anchored at its parent's source dir."""
    bases = [
        getattr(getattr(p, "package", None), "source_path", None)
        for p in (first_parent, ranked_parent)
    ]
    if None in bases:
        return None
    first_base, ranked_base = bases

    def one_entry(first: DependencyReference, ranked: DependencyReference) -> bool:
        return same_stored_leaf(
            first_base / Path(first.local_path or "").expanduser(),
            ranked_base / Path(ranked.local_path or "").expanduser(),
        )

    return one_entry


def with_declaring_edge(
    node_ref: DependencyReference,
    edge_ref: DependencyReference,
    first_parent: Any = None,
    ranked_parent: Any = None,
) -> DependencyReference:
    """*node_ref* (its first edge) carrying *edge_ref*, its recorded parent's edge,
    when :func:`recorded_declaring_edge` picks that edge.

    The anchored directory, and so the node identity, is the same for every such edge.
    *first_parent* and *ranked_parent* are the declaring nodes; their package
    source directories let a case- or normalization-only respelling count as one
    entry (#5457). Without them the leaves must match exactly.
    """
    same_entry = _edges_one_entry(first_parent, ranked_parent)
    if (
        not node_ref.is_local
        or not edge_ref.declaring_parent
        or node_ref.anchored_local_path != edge_ref.anchored_local_path
        or recorded_declaring_edge(node_ref, edge_ref, lambda ref: ref.local_path, same_entry)
        is node_ref
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
