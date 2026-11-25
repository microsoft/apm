"""Post-uninstall forward dependency reachability (canonical owner).

Single owner for the question: "given the direct dependencies that
survive an ``apm uninstall`` edit, which locked transitive dependencies
are still genuinely reachable from them, per their own real manifests?"

This exists because :mod:`apm_cli.commands.uninstall.engine`'s backward
orphan-candidate scan (``_build_children_index``, keyed by the
single-valued ``LockedDependency.resolved_by``) cannot tell a genuine
orphan from a shared/diamond transitive dependency that a SURVIVING
package still declares -- ``resolved_by`` only ever records the first
parent that introduced a dependency, never every parent that legitimately
depends on it. Rather than widen that durable, first-wins lockfile field
(large blast radius across :mod:`apm_cli.deps.why_walker`,
:mod:`apm_cli.deps.path_anchoring`, ``ci_checks.py``, ``mcp_integrator.py``
-- all single-parent consumers today), this module recomputes forward
reachability on demand, offline, from the canonical manifest/lock graph.

Completeness contract
----------------------
:func:`compute_forward_reachable_keys` returns a :class:`ReachabilityResult`
whose ``complete`` flag is ``False`` if ANY node touched during the walk
(a survivor, or a candidate orphan whose own on-disk anchor could not be
established) has a manifest that is missing, unreadable, malformed, or
whose resolved location is unsafe (a corrupt/cyclic/ambiguous local
anchor chain, or a path that escapes ``apm_modules/``). An unexplored
subtree is NOT a safe stand-in for "no children" -- it could be exactly
the branch that reaches a candidate orphan, so treating it as a leaf
would silently allow an incorrect deletion. Callers MUST treat
``complete is False`` as "no rescue information is trustworthy this run"
and preserve every candidate orphan rather than partially trust
``reachable`` -- see
:func:`apm_cli.commands.uninstall.engine._compute_actual_orphans`, the
single call site that enforces this policy for both ``apm uninstall`` and
its ``--dry-run`` preview.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.scope import manifest_file
from apm_cli.deps.path_anchoring import (
    LocalResolutionError,
    build_local_parent_index,
    resolve_local_dep_dir,
)
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.materialization import installed_package_path
from apm_cli.models.dependency.replacement import build_replacement_index, parse_replace_block
from apm_cli.models.dependency.subsets import EdgeSelection, NodeSelection, merge_edge_selections

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import DependencyReference


@dataclass(frozen=True)
class ReachabilityResult:
    """Outcome of a forward reachability walk from surviving direct deps.

    Attributes:
        reachable: Lockfile unique keys confirmed forward-reachable from
            ``direct_refs``. Only meaningful when ``complete`` is True.
        complete: False if any node touched during the walk (a survivor,
            or a candidate orphan whose own anchor was needed for
            matching) could not be safely resolved and read. Callers MUST
            NOT partially trust ``reachable`` when this is False.
        unverifiable: ``(identifier, reason)`` pairs describing every node
            that could not be resolved/read, for diagnostics.
        reachable_via: For each key in ``reachable`` that was matched
            during the walk (i.e. every rescued candidate orphan -- not
            necessarily every incidental survivor key that also happens
            to end up in ``reachable``), the ``(parent_repo_url,
            local_path)`` of the surviving node whose manifest edge
            actually led to it: the immediate parent's own
            ``repo_url`` (suitable for a direct write into that entry's
            ``LockedDependency.resolved_by``), and -- for a LOCAL match
            only -- the literal ``local_path`` string declared by THAT
            parent's manifest edge (``None`` for a remote match, which
            has no anchored ``local_path`` concept). Repairing
            ``resolved_by`` alone is not sufficient for local deps: a
            transitive local dep's ``local_path`` is interpreted
            relative to whichever directory ``resolved_by`` currently
            names (see ``apm_cli.deps.path_anchoring``), so a stale
            ``local_path`` captured from a DIFFERENT (now-removed)
            parent could resolve to the wrong directory if only
            ``resolved_by`` were repointed. Callers that persist a
            rescue (see
            ``apm_cli.commands.uninstall.engine._cleanup_transitive_
            orphans``) MUST write back both fields together, never
            ``resolved_by`` alone.
        dev_only: Keys in ``reachable`` reached only through
            ``dev_refs`` -- the install resolver's ``is_dev`` rule, where a
            production path wins. Rescues persist it with ``resolved_by``.
        edge_selections: Every declaring edge walked for each node, recorded
            before the visited dedup (as the install resolver records them),
            keyed by lockfile key when one matches. Feed it to
            :func:`apm_cli.models.dependency.subsets.merge_edge_selections`.
        parent_keys: For each key in ``reachable_via``, the exact lock key of
            that parent. Sibling virtual packages share one ``repo_url``,
            so only this key tells them apart (#4823).
        depths: Shortest depth of every walked node over the whole edge
            graph, direct declarations at 1, as install records it (#4833).
        graph_children: Every declaring edge the walk saw, parent key to
            child keys, for repairs that follow the graph, not the lock.
        local_dirs: The anchored directory of every local node the walk
            reached, by its lock key, so a re-anchored local row takes the
            identity install records for it (#4889).
        declaring_edges: Every parent key that declares a node, in install's
            breadth-first order across production and dev roots (#4999), with
            that edge's ``(parent identity, local_path)`` (#4889).
        visit_order: Each walked key's rank in that breadth-first order.
        local_edge_repos: The ``(repo_url, alias)`` each local edge resolves
            to, by ``(child key, parent key)``; another spelling or alias
            installs elsewhere.
    """

    reachable: frozenset[str]
    complete: bool
    unverifiable: tuple[tuple[str, str], ...]
    reachable_via: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    dev_only: frozenset[str] = frozenset()
    edge_selections: dict[str, tuple[EdgeSelection, ...]] = field(default_factory=dict)
    parent_keys: dict[str, str] = field(default_factory=dict)
    depths: dict[str, int] = field(default_factory=dict)
    graph_children: dict[str, frozenset[str]] = field(default_factory=dict)
    local_dirs: dict[str, Path] = field(default_factory=dict)
    declaring_edges: dict[str, dict[str, tuple[str | None, str | None]]] = field(
        default_factory=dict
    )
    local_edge_repos: dict[tuple[str, str | None], tuple[str, str | None]] = field(
        default_factory=dict
    )
    visit_order: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DeclaredSelections:
    """Merged per-node selections declared by the manifests a walk read.

    ``complete`` False means some manifest could not be read; callers must
    treat every selection as unverified rather than trust a partial merge.
    """

    selections: dict[str, NodeSelection]
    complete: bool
    unverifiable: tuple[tuple[str, str], ...]


class _ParentGraph:
    """Every declaring edge the walk sees, solved like the install resolver.

    Shortest depth comes from a breadth-first pass over the whole edge graph
    from the surviving direct declarations (production and dev alike), so a
    shorter path through a node the walk already expanded still counts. Each
    node's parent is its nearest declaring parent, ties to the lowest parent
    key, as ``APMDependencyResolver._select_nearest_parent`` records it
    (#4804, #4823, #4833). Dev status stays with the walk's own propagation.

    The walk drains production paths before dev ones; install visits every
    root breadth-first, production then dev. :meth:`visit_order` replays that
    order from the roots and each parent's declarations, so a node's first
    declaring edge is the one install keeps (#4999).
    """

    def __init__(self) -> None:
        self._roots: dict[str, None] = {}
        self._parents: dict[str, dict[str, tuple[str, str | None]]] = {}
        self._children: dict[str, dict[str, None]] = {}

    def edge(self, key: str, parent_key: str | None, via: tuple[str | None, str | None]) -> None:
        if parent_key is None:
            self._roots.setdefault(key, None)
            return
        self._parents.setdefault(key, {}).setdefault(parent_key, via)
        self._children.setdefault(parent_key, {}).setdefault(key, None)

    def visit_order(self) -> dict[str, int]:
        """Each node's rank in install's breadth-first visit from the ordered roots."""
        order = {key: rank for rank, key in enumerate(self._roots)}
        queue = deque(order)
        while queue:
            for child in self._children.get(queue.popleft(), ()):
                if child not in order:
                    order[child] = len(order)
                    queue.append(child)
        return order

    def edges(self) -> dict[str, dict[str, tuple[str | None, str | None]]]:
        """Every non-root node's declaring parents, in install's visit order."""
        rank = self.visit_order()
        return {
            key: dict(sorted(parents.items(), key=lambda item: rank.get(item[0], len(rank))))
            for key, parents in self._parents.items()
        }

    def solve(self, lock_keys, reachable_via: dict) -> tuple[dict, dict, dict]:
        """Return ``(depths, parent_keys, children)`` and fill *reachable_via*."""
        depths = dict.fromkeys(self._roots, 1)
        queue = deque(sorted(self._roots))
        while queue:
            node = queue.popleft()
            for child in sorted(self._children.get(node, ())):
                if child not in depths:
                    depths[child] = depths[node] + 1
                    queue.append(child)
        parent_keys: dict[str, str] = {}
        for key, parents in self._parents.items():
            if key in self._roots or key not in depths:
                continue
            nearest = [p for p in parents if depths.get(p) == depths[key] - 1]
            if not nearest:
                continue
            parent = min(nearest)
            parent_keys[key] = parent
            if key in lock_keys:
                reachable_via[key] = parents[parent]
        children = {node: frozenset(kids) for node, kids in self._children.items()}
        return depths, parent_keys, children


# (ref, anchor, parent repo identity, is_dev, chain, parent's exact lock key)
_WalkItem = tuple["DependencyReference", Path | None, str | None, bool, tuple[str, ...], str | None]


def locked_marketplace_form(
    ref: DependencyReference,
    lockfile: LockFile,
    marketplace_index: dict | None = None,
) -> DependencyReference:
    """Return the locked coordinates a marketplace declaration resolved to.

    Offline walks (reachability, ``apm prune``) cannot ask the marketplace
    again; the lockfile's discovery association names the package install
    materialized. The edge's own selection and alias are kept. With no
    locked row the declaration is returned unchanged.
    """
    locked = lockfile.get_dependency(ref.get_unique_key(), marketplace_index=marketplace_index)
    if locked is None:
        return ref
    return locked.to_edge_reference(ref)


def _join_local(anchor: Path, local_path: str) -> Path:
    """Resolve *local_path* against *anchor*; absolute paths bypass it.

    A plain relative-path join, distinct from (and much smaller than) the
    ``resolved_by``-chain walk :func:`resolve_local_dep_dir` performs --
    that chain-walk is the canonical, non-duplicated piece; this is just
    "join a manifest-declared relative path onto a directory we already
    resolved while visiting its declaring parent."
    """
    raw = Path(local_path).expanduser()
    return raw.resolve() if raw.is_absolute() else (anchor / raw).resolve()


def _dir_identity(path: Path, *, strict: bool = False) -> tuple[int, int] | None:
    """Return the ``(device, inode)`` of an existing directory, else None.

    With *strict*, only a missing path is None; any other stat error raises,
    since an unknown identity cannot rule out an aliased spelling.
    """
    import stat as stat_module

    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if strict:
            raise
        return None
    # Files keep path identities: two hard-linked primitive files are distinct.
    if not stat.st_ino or not stat_module.S_ISDIR(stat.st_mode):
        return None
    return stat.st_dev, stat.st_ino


def _build_local_dir_index(
    lockfile: LockFile,
    project_root: Path,
    candidate_orphans: frozenset[str],
    unverifiable: list[tuple[str, str]],
) -> dict[Path, str]:
    """Map each LOCAL candidate orphan's real directory to its lockfile key.

    Only candidate orphans are resolved here (not the whole lockfile) so
    an unrelated, already-settled entry elsewhere in a large lockfile
    cannot spuriously mark this decision incomplete. Any
    :class:`LocalResolutionError` (corrupt / ambiguous / cyclic
    ``resolved_by`` chain) is recorded in *unverifiable*, since we then
    cannot rule out that some survivor reaches that entry.
    """
    index: dict[Path, str] = {}
    parent_index = build_local_parent_index(lockfile)
    resolved_cache: dict[str, Path] = {}
    identities: dict[tuple[int, int], str] = {}
    for key in sorted(candidate_orphans):
        dep = lockfile.get_dependency(key)
        if dep is None or dep.source != "local":
            continue
        try:
            directory = resolve_local_dep_dir(
                dep,
                lockfile,
                project_root,
                parent_index=parent_index,
                resolved_cache=resolved_cache,
            )
            identity = _dir_identity(directory, strict=True)
        except (LocalResolutionError, OSError, RuntimeError, ValueError) as exc:
            # A symlink loop or a denied stat cannot be resolved: the walk is incomplete.
            unverifiable.append((key, str(exc)))
            continue
        other = index.get(directory) or (identities.get(identity) if identity else None)
        if other is not None:
            # Two rows in one directory: a reached edge cannot say which one
            # it needs, so the walk is incomplete and both stay (#4989).
            unverifiable.append((key, f"{key} and {other} resolve to one directory"))
            continue
        index[directory] = key
        if identity is not None:
            identities[identity] = key
    return index


def _local_file_not_a_leaf(local_path: str, anchor: Path) -> str | None:
    """``None`` when install admits *local_path* as a single-file primitive; else why not.

    The one leaf test both offline walks use. The name is checked first with
    ``lstat`` (a regular file, not a link), then install's own admission
    reads it; the verdict needs the bytes it read, so a file swapped for a
    directory between the checks is never admitted (#4989).
    """
    import os
    import stat

    from apm_cli.install.phases.local_content import read_local_package_source
    from apm_cli.models.dependency import DependencyReference

    spelled = Path(local_path).expanduser()
    if not spelled.is_absolute():
        spelled = anchor / spelled
    try:
        if not stat.S_ISREG(os.lstat(spelled).st_mode):
            return f"{local_path} is not a regular file"
        source = read_local_package_source(
            DependencyReference.parse_from_dict({"path": local_path}), anchor
        )
    except (ValueError, OSError) as exc:
        return str(exc)
    if source.content is None:
        return f"{local_path} is no longer a single file"
    return None


def _read_node(manifest_dir: Path, *, local: bool) -> APMPackage | None:
    """Read one node's manifest as install does; ``None`` is a declared-nothing leaf.

    A local Claude plugin is normalized; a directory without ``apm.yml`` that
    is not a leaf format raises ``FileNotFoundError`` (unverifiable).
    """
    package = _normalized_local_plugin(manifest_dir) if local else None
    if package is None:
        if _is_manifestless_leaf(manifest_dir):
            return None
        package = APMPackage.from_apm_yml(manifest_file(manifest_dir), source_path=manifest_dir)
    return package


def _directory_identities(path: Path) -> set[tuple]:
    """Identities two spellings of one directory share: inode, else a normalized path.

    Case-insensitive and normalizing filesystems (macOS, Windows) give one
    directory several spellings; the stat identity sees through them. The path
    fallback keeps the Unicode spelling, since a filesystem that preserves both
    NFC and NFD may hold two different directories.
    """
    import os

    identities: set[tuple] = {("path", os.path.normcase(os.path.abspath(path)))}
    try:
        stat = path.stat()
    except (OSError, RuntimeError):
        return identities
    identities.add(("inode", stat.st_dev, stat.st_ino))
    return identities


def local_sources_reach(
    roots: Sequence[DependencyReference], project_root: Path, targets: set[Path]
) -> bool:
    """Whether the local edges of *roots*' own sources reach a directory in *targets*.

    Offline and without ``apm_modules``: a remote package cannot declare the
    consumer's local directories, so only local edges are followed. Each node
    is read as the main walk reads it; an unverifiable node or unresolvable
    path counts as reaching, because it cannot prove the target unused (#4889).
    """
    wanted = set().union(*(_directory_identities(target) for target in targets))
    queue = [
        (Path(project_root), ref.local_path) for ref in roots if ref.is_local and ref.local_path
    ]
    seen: set[Path] = set()
    while queue:
        anchor, local_path = queue.pop()
        try:
            directory = _join_local(anchor, local_path)
        except (OSError, RuntimeError):
            return True
        if _directory_identities(directory) & wanted:
            return True
        if directory in seen:
            continue
        seen.add(directory)
        try:
            if directory.is_file():
                if _local_file_not_a_leaf(local_path, anchor) is None:
                    continue  # an admitted primitive file declares no dependencies
                return True  # any other file proves nothing
            package = _read_node(directory, local=True)
        except (OSError, ValueError, RuntimeError):
            return True
        if package is None:
            continue
        queue.extend(
            (directory, dep.local_path)
            for dep in package.get_apm_dependencies()
            if dep.is_local and dep.local_path
        )
    return False


def _is_manifestless_leaf(package_dir: Path) -> bool:
    """Return True for an installed package whose format declares no APM deps.

    APM dependencies live only in ``apm.yml``. A materialized skill, skill
    bundle or hook package without one is a leaf; any other directory
    without ``apm.yml`` (missing, empty, or an unnormalized plugin) stays
    unverifiable so callers fail closed.
    """
    from apm_cli.models.validation import PackageType, detect_package_type

    if not package_dir.is_dir() or manifest_file(package_dir).exists():
        return False
    return detect_package_type(package_dir)[0] in {
        PackageType.CLAUDE_SKILL,
        PackageType.SKILL_BUNDLE,
        PackageType.HOOK_PACKAGE,
    }


def _normalized_local_plugin(source_dir: Path) -> APMPackage | None:
    """Return a local Claude plugin's package as install normalizes it, else None.

    A local plugin without ``apm.yml`` declares its edges only in the manifest
    install synthesizes from it (#4321). Normalize a staged copy of the source
    through the canonical package validator, exactly as install does, so
    neither the ``apm_modules`` copy is trusted nor the source written. Other
    local sources return None and are read in place.
    """
    import shutil
    import tempfile

    from apm_cli.models.validation import (
        PackageType,
        detect_package_type,
        validate_apm_package,
    )

    if not source_dir.is_dir() or manifest_file(source_dir).exists():
        return None
    if detect_package_type(source_dir)[0] is not PackageType.MARKETPLACE_PLUGIN:
        return None
    with tempfile.TemporaryDirectory(prefix="apm-local-plugin-") as staging:
        staged = Path(staging) / source_dir.name
        try:
            shutil.copytree(
                source_dir, staged, symlinks=True, ignore=shutil.ignore_patterns(".git")
            )
        except OSError as exc:
            raise ValueError(f"cannot stage local plugin {source_dir}: {exc}") from exc
        validation = validate_apm_package(staged, source_path=source_dir)
    if not validation.is_valid or validation.package is None:
        raise ValueError("; ".join(validation.errors) or f"invalid plugin: {source_dir}")
    return validation.package


def compute_forward_reachable_keys(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    direct_refs: list[DependencyReference],
    candidate_orphans: frozenset[str],
    *,
    dev_refs: Sequence[DependencyReference] = (),
) -> ReachabilityResult:
    """Walk forward from surviving direct deps to find reachable lock keys.

    Args:
        lockfile: The current (in-memory) lockfile. Local anchor chains
            may still reference just-removed packages at this point in
            the uninstall flow; that is safe -- see the module docstring.
        project_root: Anchor for root-declared (direct) local deps.
        apm_modules_dir: Base directory for resolving remote/registry
            install paths (containment-checked internally by
            ``get_install_path``).
        direct_refs: The project's surviving direct dependency refs,
            freshly parsed from the (post-edit, for a real uninstall; or
            pre-edit minus the removed packages, for ``--dry-run``)
            apm.yml.
        candidate_orphans: Lockfile keys the backward orphan-candidate
            scan flagged for possible removal. Narrows how much work this
            walk needs to do (and how much can go wrong) -- see
            ``_build_local_dir_index``.
        dev_refs: Surviving ``devDependencies``. They are walked only after
            every production path, so a key first reached here is dev-only.

    Returns:
        A :class:`ReachabilityResult`. See its docstring and the module
        docstring for the completeness contract.
    """
    unverifiable: list[tuple[str, str]] = []
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.local_identity import root_local_dirs as collect_root_local_dirs
    from apm_cli.deps.local_identity import transitive_local_identity
    from apm_cli.deps.lockfile import lockfile_parent_identity

    replacement_index = build_replacement_index(parse_replace_block(lockfile.replacements))
    local_dir_index = _build_local_dir_index(
        lockfile, project_root, candidate_orphans, unverifiable
    )
    # A case-folding or normalizing filesystem opens one directory under
    # several spellings; its file identity still names one node (#4989).
    local_dir_ids = {
        identity: key
        for path, key in local_dir_index.items()
        if (identity := _dir_identity(path)) is not None
    }
    # A reached edge names its exact lock key first; directories match the rest.
    indexed_keys = frozenset(local_dir_index.values())

    reachable: set[str] = set()
    dev_only: set[str] = set()
    reachable_via: dict[str, tuple[str, str | None]] = {}
    parent_graph = _ParentGraph()
    visited: set[str] = set()  # resolved local dirs (as str) or remote unique keys
    # Each queue entry also carries the repo_url of the node whose manifest
    # edge introduced it (None for the seed direct_refs, which have no
    # parent in this graph) -- this is how a rescued candidate's immediate,
    # currently-valid parent is recovered for the resolved_by/local_path
    # repair (see ReachabilityResult.reachable_via).
    # Production paths drain before any dev path starts, so ``visited``
    # gives a key the production flag whenever any production path exists.
    queue: deque[_WalkItem] = deque(
        (ref, project_root, None, False, (), None) for ref in direct_refs
    )
    dev_queue: deque[_WalkItem] = deque(
        (ref, project_root, None, True, (), None) for ref in dev_refs
    )
    edge_selections: dict[str, list[EdgeSelection]] = {}
    local_dirs: dict[str, Path] = {}
    local_edge_repos: dict[tuple[str, str | None], tuple[str, str | None]] = {}
    expander = APMDependencyResolver(read_only=True, apm_modules_dir=apm_modules_dir)

    marketplace_index = lockfile.marketplace_index()
    try:
        root_local_dirs = collect_root_local_dirs((*direct_refs, *dev_refs), project_root)
    except (OSError, RuntimeError) as exc:
        # A surviving root that cannot be resolved (a symlink loop): nothing is proven.
        return ReachabilityResult(
            reachable=frozenset(),
            complete=False,
            unverifiable=(*unverifiable, ("root local dependencies", str(exc))),
        )

    while queue or dev_queue:
        ref, anchor, parent_repo_url, is_dev, chain, parent_key = (queue or dev_queue).popleft()
        edge: tuple[str, Path] | None = None
        if ref.is_marketplace:
            ref = locked_marketplace_form(ref, lockfile, marketplace_index)
        try:
            ref = replacement_index.bind(
                ref,
                consumer_root=project_root,
                modules_dir=apm_modules_dir,
                parent=parent_repo_url,
                is_dev=is_dev,
            )
        except ValueError as exc:
            unverifiable.append((ref.get_unique_key(), str(exc)))
            continue

        if ref.is_local and ref.local_path:
            if ref.replacement is not None:
                anchor = project_root
            try:
                real_dir = _join_local(anchor, ref.local_path) if anchor else None
            except (OSError, RuntimeError) as exc:
                # A symlink loop cannot be resolved: the subtree is unknown (#4889).
                unverifiable.append((ref.get_unique_key(), str(exc)))
                continue
            if real_dir is None:
                continue
            # The edge as declared: a file is admitted against it, not the node identity.
            edge = (ref.local_path, anchor)
            if ref.replacement is None and parent_repo_url is not None:
                ref = transitive_local_identity(
                    ref,
                    declaring_parent=parent_repo_url,
                    anchored=real_dir,
                    root=project_root,
                    root_local_dirs=root_local_dirs,
                )
            visit_key = ref.get_unique_key() if ref.replacement is not None else str(real_dir)
            exact = ref.get_unique_key()
            matched_key = (
                exact
                if exact in lockfile.dependencies
                and (ref.replacement is not None or exact in indexed_keys)
                else local_dir_index.get(real_dir) or local_dir_ids.get(_dir_identity(real_dir))
            )
            if matched_key is None and ref.get_unique_key() in lockfile.dependencies:
                # A surviving local node keeps its dev status like a remote one (#4889).
                matched_key = ref.get_unique_key()
            edge_selections.setdefault(matched_key or ref.get_unique_key(), []).append(
                EdgeSelection.from_dependency(ref, chain)
            )
            parent_graph.edge(
                matched_key or ref.get_unique_key(),
                parent_key,
                (parent_repo_url, None if ref.replacement is not None else ref.local_path),
            )
            local_edge_repos.setdefault(
                (matched_key or ref.get_unique_key(), parent_key), (ref.repo_url, ref.alias)
            )
            if visit_key in visited:
                continue
            visited.add(visit_key)
            if matched_key is not None:
                reachable.add(matched_key)
                if is_dev:
                    dev_only.add(matched_key)
            manifest_dir = real_dir
            # A surviving local parent is not in the candidate directory
            # index; its own identity is still its lock key (#4823).
            node_key = matched_key or ref.get_unique_key()
            local_dirs.setdefault(node_key, real_dir)
        else:
            locked = lockfile.dependencies.get(ref.get_unique_key())
            # Several edges may alias one node; it is materialized where the
            # locked alias put it, not where this edge's alias would.
            path_ref = (
                replace(ref, alias=locked.alias)
                if locked is not None and locked.alias != ref.alias
                else ref
            )
            try:
                install_path = installed_package_path(path_ref, apm_modules_dir)
            except ValueError as exc:
                # Unresolved marketplace ref or an install path that
                # escapes apm_modules_dir (PathTraversalError, a ValueError
                # subclass) -- either way this node's subtree is unknown.
                unverifiable.append((ref.get_canonical_dependency_string(), str(exc)))
                continue
            visit_key = ref.get_unique_key()
            edge_selections.setdefault(visit_key, []).append(
                EdgeSelection.from_dependency(ref, chain)
            )
            # Remote deps don't use anchored local_path -- only
            # resolved_by needs repairing for these.
            parent_graph.edge(visit_key, parent_key, (parent_repo_url, None))
            if visit_key in visited:
                continue
            visited.add(visit_key)
            if visit_key in lockfile.dependencies:
                reachable.add(visit_key)
                if is_dev:
                    dev_only.add(visit_key)
            manifest_dir = install_path
            node_key = visit_key

        try:
            if edge is not None and manifest_dir.is_file():
                # An admitted primitive file declares no dependencies; any other
                # file is not a package, so its subtree is unknown.
                if (reason := _local_file_not_a_leaf(*edge)) is not None:
                    unverifiable.append((visit_key, reason))
                continue
            package = _read_node(manifest_dir, local=ref.is_local)
            if package is None:
                continue
        except (OSError, ValueError) as exc:
            # Missing or malformed manifest: this subtree's true children
            # are unknown. Do NOT treat it as childless -- record it and
            # let the caller preserve every candidate this run instead of
            # trusting a partial walk.
            unverifiable.append((visit_key, str(exc)))
            continue
        if not ref.is_local and not package.source:
            # Install marks a fetched package with its origin, which is what
            # makes its same-repository ``path:`` children expand as remote.
            package = replace(package, source=ref.repo_url)

        child_chain = (*chain, ref.get_display_name())
        for declared in package.get_apm_dependencies():
            # Expand ``git: parent`` and same-repository ``path:`` declarations
            # exactly as the install resolver does, so each edge lands on the
            # node identity install recorded. A declaration install rejects
            # never becomes a node.
            child_ref = expander.expand_transitive_declaration(ref, package, declared)
            if child_ref is None:
                continue
            # manifest_dir anchors a LOCAL child's own relative local_path;
            # remote children ignore the anchor (they resolve via
            # get_install_path instead), so propagating it unconditionally
            # is safe and keeps this loop uniform for both source types.
            # ref.repo_url is this node's own identity, becoming the
            # recorded immediate parent for each child it declares.
            (dev_queue if is_dev else queue).append(
                (
                    child_ref,
                    manifest_dir,
                    lockfile_parent_identity(ref, lockfile.replacements),
                    is_dev,
                    child_chain,
                    node_key,
                )
            )

    depths, parent_keys, graph_children = parent_graph.solve(lockfile.dependencies, reachable_via)
    return ReachabilityResult(
        reachable=frozenset(reachable),
        complete=not unverifiable,
        unverifiable=tuple(unverifiable),
        reachable_via=reachable_via,
        dev_only=frozenset(dev_only),
        edge_selections={key: tuple(edges) for key, edges in edge_selections.items()},
        parent_keys=parent_keys,
        depths=depths,
        graph_children=graph_children,
        local_dirs=local_dirs,
        declaring_edges=parent_graph.edges(),
        local_edge_repos=local_edge_repos,
        visit_order=parent_graph.visit_order(),
    )


def declared_node_selections(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    direct_refs: Sequence[DependencyReference],
    *,
    dev_refs: Sequence[DependencyReference] = (),
) -> DeclaredSelections:
    """Merge the selection every walked manifest declares for each locked node.

    Walks the same declared graph as install (the project's direct refs, then
    each materialized package's ``apm.yml``) and merges each node's edges
    through the canonical selection-merge owner. ``apm uninstall`` uses it to
    shrink survivors' selections; ``apm audit`` uses it as the expected value
    for every locked dependency.
    """
    walk = compute_forward_reachable_keys(
        lockfile,
        project_root,
        apm_modules_dir,
        list(direct_refs),
        frozenset(dep.get_unique_key() for dep in lockfile.get_package_dependencies()),
        dev_refs=dev_refs,
    )
    return DeclaredSelections(
        selections={
            key: merge_edge_selections(edges) for key, edges in walk.edge_selections.items()
        },
        complete=walk.complete,
        unverifiable=walk.unverifiable,
    )


def retained_lock_keys(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    declared_refs: list[DependencyReference],
    *,
    dev_refs: Sequence[DependencyReference] = (),
    unverified: dict[str, str] | None = None,
    anchored: dict[str, str] | None = None,
    walks: list[ReachabilityResult] | None = None,
) -> frozenset[str]:
    """Return the lock keys a project still needs beyond its declarations.

    The one answer ``apm prune`` and ``apm deps list`` give for which locked
    packages are retained: every recorded remote transitive (``depth > 1``)
    plus any undeclared direct entry or LOCAL transitive that is still
    forward-reachable from a declared package's own manifest (a direct
    dependency hand-removed from apm.yml that another package needs, #3668;
    a local package only a removed package declared, #4989). Local
    transitives match by their exact lock keys and resolved directories.

    An incomplete walk, or a local entry whose directory cannot be resolved,
    fails closed and retains every such entry; each one kept only for that
    reason is named in *unverified* with the reason, for the caller to report.
    The walk, when one ran, is appended to *walks* for parent repairs. A row
    kept because a survivor reached through a symlink alias still resolves
    through it is named in *anchored*; what it declares is retained too.
    *dev_refs* names the dev-only declarations among *declared_refs*; the
    walk visits them after production roots, as install does.
    """
    declared = {ref.get_unique_key() for ref in declared_refs}
    retained: set[str] = set()
    candidates: set[str] = set()
    reasons: dict[str, str] = {}
    parent_index = build_local_parent_index(lockfile)
    resolved_cache: dict[str, Path] = {}
    for dep in lockfile.get_package_dependencies():
        key = dep.get_unique_key()
        if (
            dep.depth is not None
            and dep.depth > 1
            and (dep.replacement is not None or dep.source != "local")
        ):
            # Remote and replaced rows keep the remote transitive policy.
            retained.add(key)
        elif key not in declared:
            candidates.add(key)
        if key not in candidates or not dep.is_local_source:
            continue
        try:
            resolve_local_dep_dir(
                dep,
                lockfile,
                project_root,
                parent_index=parent_index,
                resolved_cache=resolved_cache,
            )
        except (LocalResolutionError, OSError, RuntimeError) as exc:
            # Only this entry's own match is unknown; the walk stays usable.
            candidates.discard(key)
            retained.add(key)
            reasons[key] = str(exc)
    if reasons:
        # A kept local row whose source cannot be resolved has an unknown
        # subtree: any other candidate may be its child, so all stay.
        detail = "; ".join(f"{key}: {reason}" for key, reason in sorted(reasons.items()))
        reasons.update(dict.fromkeys(candidates, f"a kept local source cannot be read ({detail})"))
        retained |= candidates
        candidates = set()
    if candidates:
        # Dev-only roots walk after production, so dev status is install's (#5126).
        dev_keys = {ref.get_unique_key() for ref in dev_refs}
        walk = compute_forward_reachable_keys(
            lockfile,
            project_root,
            apm_modules_dir,
            [ref for ref in declared_refs if ref.get_unique_key() not in dev_keys],
            frozenset(candidates),
            dev_refs=dev_refs,
        )
        if walks is not None:
            walks.append(walk)
        if walk.complete:
            retained |= candidates & walk.reachable
            _retain_alias_anchors(
                lockfile,
                project_root,
                apm_modules_dir,
                walk,
                candidates,
                retained,
                reasons,
                anchored if anchored is not None else {},
            )
        else:
            retained |= candidates
            detail = "; ".join(f"{name}: {reason}" for name, reason in walk.unverifiable)
            reasons.update(dict.fromkeys(candidates - walk.reachable, detail))
    _retain_reached_from(
        lockfile,
        project_root,
        apm_modules_dir,
        [
            dep.to_dependency_ref()
            for dep in lockfile.get_package_dependencies()
            if dep.get_unique_key() in retained
            and dep.replacement is not None
            and dep.is_local_source
        ],
        candidates,
        retained,
        reasons,
    )
    if unverified is not None:
        unverified.update(reasons)
    return frozenset(retained)


def _retain_reached_from(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    roots: list[DependencyReference],
    candidates: set[str],
    retained: set[str],
    reasons: dict[str, str],
) -> None:
    """Retain the pending candidates a walk from *roots* reaches (#4989).

    Roots are rows kept without a surviving declaration: a locally replaced
    transitive under the remote retention policy, or an alias anchor. Their
    manifests still declare children, which stay too. The walk is not used
    for parent or depth repairs; an incomplete walk keeps every pending row.
    """
    pending = candidates - retained
    if not roots or not pending:
        return
    walk = compute_forward_reachable_keys(
        lockfile, project_root, apm_modules_dir, roots, frozenset(pending)
    )
    if walk.complete:
        retained |= pending & walk.reachable
        return
    retained |= pending
    detail = "; ".join(f"{name}: {reason}" for name, reason in walk.unverifiable)
    reasons.update(dict.fromkeys(pending, detail))


def _retain_alias_anchors(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    walk: ReachabilityResult,
    candidates: set[str],
    retained: set[str],
    reasons: dict[str, str],
    anchored: dict[str, str],
) -> None:
    """Keep the parent chain of a survivor prune cannot re-parent, and its children.

    Each chain is walked from its root-declared top so every package it still
    declares stays; when any chain's top is not root-declared, every pending
    row is kept (fail closed).
    """
    from apm_cli.deps.lockfile import lockfile_parent_identity

    anchors = _alias_anchors(lockfile, candidates - retained, walk)
    if not anchors:
        return
    anchored.update(anchors)
    retained |= set(anchors)
    rows = [lockfile.dependencies[key] for key in anchors]
    anchor_ids = {lockfile_parent_identity(row, lockfile.replacements) for row in rows}
    ends = [row for row in rows if row.resolved_by not in anchor_ids]
    tops = [row for row in ends if row.resolved_by is None]
    pending = candidates - retained
    if len(tops) < len(ends):
        retained |= pending
        reasons.update(dict.fromkeys(pending, "an anchored package has no root-declared top"))
        return
    # The tops' walk must still reach every kept anchor below them; one an
    # edited manifest no longer declares has children this walk cannot see.
    below = {key for key in anchors if lockfile.dependencies[key].resolved_by is not None}
    reach = compute_forward_reachable_keys(
        lockfile,
        project_root,
        apm_modules_dir,
        [row.to_dependency_ref() for row in tops],
        frozenset(pending | below),
    )
    if reach.complete and below <= reach.reachable:
        retained |= pending & reach.reachable
        return
    retained |= pending
    detail = "; ".join(f"{name}: {reason}" for name, reason in reach.unverifiable)
    reasons.update(
        dict.fromkeys(pending, detail or "a kept parent no longer declares a kept package")
    )


def parent_identity_counts(lockfile: LockFile) -> Counter:
    """How many lock rows record each parent identity; built once per decision (#5126)."""
    from apm_cli.deps.lockfile import lockfile_parent_identity

    return Counter(
        lockfile_parent_identity(row, lockfile.replacements)
        for row in lockfile.dependencies.values()
    )


def reparent_blocker(
    lockfile: LockFile, walk: ReachabilityResult, key: str, identity_counts: Counter
) -> str | None:
    """Why prune cannot re-derive *key*'s parent from *walk*, or None when it can (#5126).

    *identity_counts* comes from :func:`parent_identity_counts` for *lockfile*.
    """
    depth = walk.depths.get(key)
    nearest = [
        parent
        for parent in walk.declaring_edges.get(key, {})
        if depth is not None and walk.depths.get(parent) == depth - 1
    ]
    return _anchor_reason(
        lockfile, walk, key, lockfile.dependencies[key].repo_url, nearest, identity_counts
    )


def _anchor_reason(lockfile, walk, key, repo_url, nearest, identity_counts) -> str | None:
    """Why a survivor cannot be re-parented by prune, or None when it can.

    Re-parenting records the nearest surviving parent, ties to the lowest key;
    that parent must be the first one the walk saw, or spell the survivor's
    path exactly as the first one does while that first edge's alias is the
    locked one (path, identity and install directory are then one edge
    whichever is recorded, #5386), keep a lock row (an unlocked parent
    leaves the survivor unresolvable) and spell the survivor by its own
    identity (an alias would re-key it).
    """
    if not nearest:
        return f"{repo_url} still resolves through it, and apm.yml now declares {repo_url}"
    parent = min(nearest)
    edges = walk.declaring_edges.get(key, {})
    first_path = (edges.get(nearest[0]) or (None, None))[1]
    first_alias = walk.local_edge_repos.get((key, nearest[0]), (None, None))[1]
    if parent != nearest[0] and (
        (edges.get(parent) or (None, None))[1] != first_path
        # Install keeps the first edge's alias: one the lock lacks would move the copy.
        or first_alias != lockfile.dependencies[key].alias
    ):
        # The repair would pair one parent's path with another's identity (#5126).
        return (
            f"{repo_url} still resolves through it, and several packages at the same "
            f"depth now declare {repo_url}"
        )
    if parent not in lockfile.dependencies:
        return (
            f"{repo_url} still resolves through it, and the package that now declares "
            f"{repo_url} is not in apm.lock.yaml"
        )
    from apm_cli.deps.lockfile import lockfile_parent_identity

    identity = lockfile_parent_identity(lockfile.dependencies[parent], lockfile.replacements)
    if identity_counts[identity] > 1:
        # Recording a parent identity two rows share leaves the survivor ambiguous.
        return (
            f"{repo_url} still resolves through it, and the package that now declares "
            f"{repo_url} shares its identity {identity} with another package"
        )
    declared = walk.local_edge_repos.get((key, parent), (None, None))[0]
    if declared in (None, repo_url):
        return None
    return (
        f"{repo_url} still resolves through it, and the package that still "
        f"needs {repo_url} declares it as {declared}"
    )


def _alias_anchors(lockfile, removed_keys, walk) -> dict[str, str]:
    """Removed rows prune keeps because a survivor cannot be re-parented (#4989).

    When the surviving edge spells a local survivor under another identity (a
    symlink alias), comes from a package with no lock row, or apm.yml now
    declares the survivor itself, install would re-record it; prune does not
    re-key packages, so it keeps the survivor's recorded parent chain instead.
    """
    from apm_cli.deps.lockfile import lockfile_parent_identity

    if walk is None or lockfile is None or not removed_keys:
        return {}
    gone = {
        lockfile_parent_identity(lockfile.dependencies[key], lockfile.replacements): key
        for key in removed_keys
        if key in lockfile.dependencies
    }
    anchors: dict[str, str] = {}
    identity_counts = parent_identity_counts(lockfile)
    for key in sorted(lockfile.dependencies):
        # Every survivor, retained or declared, whose recorded parent goes.
        row = lockfile.dependencies.get(key)
        if key in removed_keys or row.source != "local" or row.replacement is not None:
            continue
        if row.resolved_by not in gone or key not in walk.depths:
            continue
        depth = walk.depths[key]
        nearest = [
            parent
            for parent in walk.declaring_edges.get(key, {})
            if walk.depths.get(parent) == depth - 1
        ]
        reason = _anchor_reason(lockfile, walk, key, row.repo_url, nearest, identity_counts)
        if reason is None:
            continue
        parent = gone.get(row.resolved_by)
        while parent is not None and parent not in anchors:
            anchors[parent] = reason
            parent = gone.get(lockfile.dependencies[parent].resolved_by)
    return anchors
