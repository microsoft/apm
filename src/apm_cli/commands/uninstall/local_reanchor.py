"""Keep a still-needed LOCAL dependency by re-anchoring it on uninstall (#4889).

A local lock key embeds where the dependency was declared: a root-declared
``./vendor/e`` and the transitive ``local:vendor/e`` are different
identities. When ``apm uninstall ./vendor/e`` drops the root declaration but
a surviving package still declares ``e``, the row must take the identity
``apm install --no-frozen`` records for it, or ``--frozen`` fails.

Resolution is the offline reachability walk
(:func:`apm_cli.deps.reachability.compute_forward_reachable_keys`): the
install resolver's own read-only expander plus
:func:`apm_cli.deps.local_identity.transitive_local_identity`, without any
download. Its nearest parent, depth and dev status already match a fresh
install (#4804, #4833). An incomplete walk refuses before any write: a
re-anchor never fetches.

The plan re-keys, in one in-memory transition: the lock row (``local_path``
as the new parent declares it, ``declaring_parent`` and
``anchored_local_path``), every reference to the old key (descendants'
``declaring_parent`` and ``resolved_by``, replacement request parents per
req-rpl-003) and the lock's deployment-ledger owners. Receipt bindings are
verified before any write and renamed after the lock commits, carrying
their hash history (:mod:`apm_cli.core.deployment_reanchor`).
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ...utils.paths import spelling_key
from .engine import _dependency_public_label, _removed_lock_key


class LocalReanchorRefused(ValueError):
    """A still-needed local dependency cannot be re-anchored; nothing was written.

    *next_step* replaces the generic advice when the refusal has its own fix.
    """

    def __init__(self, message: str, next_step: str | None = None) -> None:
        super().__init__(message)
        self.next_step = next_step


@dataclass(frozen=True)
class LocalReanchorPlan:
    """Old lock key -> new key, plus the identity fields every re-keyed row takes."""

    renames: dict[str, str] = field(default_factory=dict)
    # (local_path, declaring_parent, anchored_local_path, repo_url) per old key.
    identities: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    # Descendant local rows: (declaring parent key, local_path) of the first edge.
    declaring_parents: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # Nearest parent identity of every re-parented row, ranked by the new keys.
    resolved_by: dict[str, str | None] = field(default_factory=dict)
    # apm.yml and apm.lock.yaml as the command found them, for rollback.
    snapshot: tuple[tuple[Path, str], ...] = ()
    # (current, re-anchored) install directory of each re-keyed root (#4999).
    moves: tuple[tuple[Path, Path], ...] = ()
    # (current install directory, its dependency reference, framed hash) of each
    # moving copy whose hash-excluded files went stale: re-materialized (#5102).
    fresh: tuple[tuple[Path, Any, bool], ...] = ()
    # Lock key -> install's breadth-first visit rank, for request order (#4999).
    visit_order: dict[str, int] = field(default_factory=dict)
    # Deployed path -> the writer a fresh install records once re-ranked (#5048).
    writers: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.renames or self.declaring_parents)


def _nearest_parents(walk, key: str) -> list[str]:
    """The node's shallowest declaring parents, in the order the walk saw their edges."""
    depth = walk.depths.get(key)
    edges = walk.declaring_edges.get(key, {})
    return [
        parent for parent in edges if depth is not None and walk.depths.get(parent) == depth - 1
    ]


def _root_identities(
    lockfile, walk, local_roots, label, modules_dir: Path
) -> dict[str, tuple[str, str, Any, str]]:
    """``(local_path, declaring parent key, anchored dir, repo_url)`` of each root.

    Install keeps the first shallowest declaring edge on the node it creates,
    so that edge, not the nearest-parent tie rule, names the local identity.
    """
    identities: dict[str, tuple[str, str, Any, str]] = {}
    for key in local_roots:
        nearest = _nearest_parents(walk, key)
        real_dir = walk.local_dirs.get(key)
        via = walk.declaring_edges.get(key, {}).get(nearest[0]) if nearest else None
        if via is None or via[1] is None or real_dir is None:
            raise LocalReanchorRefused(
                f"{label(key)} stays, but its new declaring parent could not be determined"
            )
        declared = _winning_edge_installs_here(walk, key, nearest[0], lockfile, label, modules_dir)
        identities[key] = (via[1], nearest[0], real_dir, declared)
    return identities


def _winning_edge_installs_here(walk, key, parent, lockfile, label, modules_dir: Path):
    """Return the winning edge's repo spelling, or refuse when it installs elsewhere.

    Install creates the node from its first shallowest edge; another alias or
    another package spelling there names another directory (#4889, #4999).
    """
    declared, alias = walk.local_edge_repos.get((key, parent), (None, None))
    row = lockfile.get_dependency(key)
    if alias != row.alias:
        raise LocalReanchorRefused(
            f"{label(key)} stays, but the package that still needs it declares it under "
            f"another name ({alias or 'no alias'}), which installs elsewhere"
        )
    if declared != row.repo_url and not _same_install_dir(row, declared, modules_dir):
        # An alias spelling (a symlink) installs as another package directory.
        raise LocalReanchorRefused(
            f"{label(key)} stays, but the package that still needs it declares it as "
            f"{declared or 'another package'}, which installs elsewhere"
        )
    return declared


def _same_install_dir(row, declared: str | None, modules_dir: Path) -> bool:
    """Whether *declared* names *row*'s own package directory (a case-insensitive filesystem)."""
    if declared is None or declared.casefold() != row.repo_url.casefold():
        return False
    ref = row.to_dependency_ref()
    try:
        current = ref.get_install_path(modules_dir)
        spelled = replace(ref, repo_url=declared).get_install_path(modules_dir)
        return current.exists() and spelled.exists() and current.samefile(spelled)
    except (OSError, ValueError):
        return False


def plan_local_reanchor(
    lockfile, decision, project_root, rescued_roots, *, label
) -> LocalReanchorPlan:
    """Return the re-anchor plan for the still-needed local roots, or refuse.

    *rescued_roots* are the lock keys of still-needed packages whose own
    declaration goes; *label* names one for messages. Every row the walk
    re-parents also gets the parent install records, ranked by its new keys.
    """
    from ...deps.local_identity import transitive_local_identity

    local_roots = [
        key
        for key in sorted(rescued_roots)
        if (row := lockfile.get_dependency(key)) is not None and row.source == "local"
    ]
    if not local_roots:
        return LocalReanchorPlan()
    walk = getattr(decision, "reachability", None)
    if walk is None or not walk.complete:
        unverifiable = sorted({key for key, _reason in getattr(walk, "unverifiable", ())})
        detail = f" ({', '.join(unverifiable[:3])})" if unverifiable else ""
        raise LocalReanchorRefused(
            f"{label(local_roots[0])} stays, but the packages that still need it could not "
            f"all be read offline{detail}; re-anchoring it never fetches"
        )
    # The identity install records, from the one owner of local node identity.
    modules_dir = Path(project_root) / "apm_modules"
    nodes = {
        key: transitive_local_identity(
            replace(
                lockfile.get_dependency(key).to_dependency_ref(),
                local_path=local_path,
                repo_url=repo_url,
            ),
            declaring_parent=parent_key,
            anchored=real_dir,
            root=project_root,
            root_local_dirs={},
        )
        for key, (local_path, parent_key, real_dir, repo_url) in _root_identities(
            lockfile, walk, local_roots, label, modules_dir
        ).items()
    }
    renames = {key: node.get_unique_key() for key, node in nodes.items()}
    collisions = sorted(
        new for old, new in renames.items() if new != old and new in lockfile.dependencies
    )
    if collisions:
        raise LocalReanchorRefused(
            f"{label(local_roots[0])} stays, but its new lock identity {collisions[0]} "
            "already belongs to another entry"
        )
    # A re-anchored parent of another re-anchored root is named by its new key.
    identities = {
        key: (
            node.local_path,
            renames.get(node.declaring_parent, node.declaring_parent),
            node.anchored_local_path,
            node.repo_url,
        )
        for key, node in nodes.items()
    }
    moves = _materialization_moves(lockfile, nodes, modules_dir, label)
    fresh = _stale_excluded_copies(lockfile, nodes, moves, walk, modules_dir)
    declaring_parents: dict[str, tuple[str, str | None]] = {}
    resolved_by: dict[str, str | None] = {}
    for key in decision.rescued_parents:
        nearest = _nearest_parents(walk, key)
        if not nearest:
            continue
        edges = walk.declaring_edges[key]
        # The nearest-parent tie goes to the lowest key, as install ranks the new keys.
        resolved_by[key] = edges[min(nearest, key=lambda parent: renames.get(parent, parent))][0]
        row = lockfile.get_dependency(key)
        if row is not None and row.source == "local" and key not in nodes:
            # A re-parented descendant must still install where its row says, from a copy.
            _winning_edge_installs_here(walk, key, nearest[0], lockfile, label, modules_dir)
            copy = row.to_dependency_ref().get_install_path(modules_dir)
            _require_copy(copy, key, label, modules_dir)
            _require_fresh_copy(row, walk.local_dirs.get(key), copy, key, label, modules_dir)
        if row is not None and row.source == "local" and key not in nodes and row.declaring_parent:
            first = nearest[0]
            declaring_parents[key] = (renames.get(first, first), edges[first][1])
    return LocalReanchorPlan(
        renames,
        identities,
        declaring_parents,
        resolved_by,
        moves=moves,
        fresh=fresh,
        visit_order=dict(getattr(walk, "visit_order", {}) or {}),
    )


def _stale_excluded_copies(lockfile, nodes, moves, walk, modules_dir: Path) -> tuple:
    """Moving copies whose hash-excluded files (.git/, __pycache__/) differ from source.

    Moving such a copy as-is would leave bytes a fresh install replaces, so it
    is re-materialized from source instead (#5102). Read-only; a copy that
    cannot be compared keeps today's plain move.
    """
    moving = {current for current, _target in moves}
    fresh = []
    for key in nodes:
        row = lockfile.get_dependency(key)
        ref = row.to_dependency_ref()
        current = ref.get_install_path(modules_dir)
        source = (getattr(walk, "local_dirs", None) or {}).get(key)
        if source is None and ref.local_path:
            # A rescued root was declared by the project: its path is project-relative.
            source = modules_dir.parent / ref.local_path
        if current not in moving or source is None:
            continue
        try:
            stale = not _unhashed_files_match(Path(source), current)
        except (OSError, ValueError):
            stale = False
        if stale:
            fresh.append((current, ref, row.content_hash_framed))
    return tuple(fresh)


def _materialization_moves(lockfile, nodes, modules_dir: Path, label):
    """Each re-keyed root's current install directory and the one install now uses.

    A missing current copy, or a re-anchored directory that already exists,
    refuses before any write: either leaves apm_modules unlike a fresh install.
    """
    moves = []
    for key, node in nodes.items():
        current = lockfile.get_dependency(key).to_dependency_ref().get_install_path(modules_dir)
        target = node.get_install_path(modules_dir)
        _require_copy(current, key, label, modules_dir)
        if current == target:
            continue
        if target.exists():
            raise LocalReanchorRefused(
                f"{label(key)} stays, but its re-anchored directory "
                f"{target.relative_to(modules_dir).as_posix()} in apm_modules already exists"
            )
        moves.append((current, target))
    return tuple(moves)


def _require_copy(path: Path, key: str, label, modules_dir: Path) -> None:
    """Refuse a kept package whose apm_modules copy is gone: install would recreate it."""
    if path.is_symlink() or not path.is_dir():
        raise LocalReanchorRefused(
            f"{label(key)} stays, but its apm_modules copy "
            f"{path.relative_to(modules_dir).as_posix()} is missing, so the re-anchor cannot "
            "keep it as a fresh install would"
        )


def _require_fresh_copy(
    row, source: Path | None, copy: Path, key: str, label, modules_dir: Path
) -> None:
    """Refuse a kept descendant whose local source no longer matches its copy (#4999).

    Install recopies a changed local source, so keeping the old copy would not
    leave apm_modules as a fresh install does. An unreadable source refuses too.
    """
    from ...install.phases.local_content import local_source_matches_copy

    try:
        fresh = (
            source is not None
            and local_source_matches_copy(row, source, copy)
            and _unhashed_files_match(source, copy)
        )
    except (OSError, ValueError, TypeError):
        fresh = False
    if not fresh:
        from ...core.remediation import UNINSTALL_REANCHOR_STALE_COPY

        raise LocalReanchorRefused(
            f"{label(key)} stays, but its local source differs from its apm_modules copy "
            f"{copy.relative_to(modules_dir).as_posix()}, so the re-anchor cannot keep it as "
            "a fresh install would",
            next_step=UNINSTALL_REANCHOR_STALE_COPY.sentence(),
        )


def _unhashed_files_match(source: Path, copy: Path) -> bool:
    """Whether the files the package hash skips (.git/, __pycache__/) match the copy.

    Install copies them through the local copier, so a change there alone still
    makes a fresh install rewrite apm_modules (#4999).
    """
    import filecmp
    import tempfile

    from ...config import get_apm_temp_dir
    from ...install.phases.local_content import _copy_tree_dereferencing_validated

    if not source.is_dir():
        return True

    def unhashed(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and {".git", "__pycache__"} & set(path.relative_to(root).parts)
        }

    with tempfile.TemporaryDirectory(prefix="apm-reanchor-", dir=get_apm_temp_dir()) as stage:
        image = Path(stage) / "package"
        _copy_tree_dereferencing_validated(source, image, source)
        names = unhashed(image)
        # filecmp streams each pair in bounded chunks: no tree is held in memory.
        return names == unhashed(copy) and all(
            filecmp.cmp(image / name, copy / name, shallow=False) for name in sorted(names)
        )


def move_materializations(
    moves: tuple[tuple[Path, Path], ...], modules_dir: Path
) -> tuple[tuple[Path, Path, tuple[Path, ...]], ...]:
    """Move each install directory to its re-anchored path inside *modules_dir*.

    Every path is checked before the first move. The journal records each move
    with the parent directories it created; a failed move removes the parents
    it made and undoes the earlier moves, then re-raises, so apm_modules is
    left as it was and nothing outside it changes.
    """
    from ...utils.path_security import PathTraversalError, ensure_path_within

    root = Path(modules_dir)
    for path in (path for move in moves for path in move):
        try:
            ensure_path_within(path, root)
        except PathTraversalError as exc:
            raise ValueError(f"{path} is outside apm_modules") from exc
    # Every destination's missing parents are found before the first rename, so
    # an error inspecting them cannot strand a directory already moved.
    planned = [
        (
            current,
            target,
            tuple(
                parent
                for parent in reversed(target.parents)
                if parent != root and root in parent.parents and not parent.exists()
            ),
        )
        for current, target in moves
    ]
    done: list[tuple[Path, Path, tuple[Path, ...]]] = []
    for current, target, created in planned:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            current.rename(target)
        except BaseException:
            _undo_failed_move(current, target, created, done, root)
            raise
        done.append((current, target, created))
    return tuple(done)


def _undo_failed_move(current, target, created, done: list, root: Path) -> None:
    """Undo a failed or interrupted (Ctrl-C) move and every earlier one.

    A rename that completed before the interrupt is moved back too; earlier
    moves go back even when cleaning this move's parents fails.
    """
    try:
        if target.is_dir() and not current.exists():
            done.append((current, target, created))
        else:
            _remove_created(created)
    finally:
        if unrestored := restore_materializations(tuple(done), root):
            raise OSError(f"could not move back {', '.join(unrestored)}")


def _remove_created(created: tuple[Path, ...]) -> None:
    """Remove the parents a move created, deepest first, while they stay empty."""
    for parent in reversed(created):
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def restore_materializations(
    moved: tuple[tuple[Path, Path, tuple[Path, ...]], ...], modules_dir: Path
) -> list[str]:
    """Put each moved directory back and remove only the parents its move created.

    Every move is attempted; returns the ``apm_modules/...`` directories that
    could not be moved back (empty when all were), never raising part way.
    """
    unrestored: list[str] = []
    for current, target, created in reversed(moved):
        try:
            target.rename(current)
            _remove_created(created)
        except OSError:
            unrestored.append(_modules_path(target, modules_dir))
    return unrestored


def _modules_path(path: Path, modules_dir: Path) -> str:
    try:
        return f"apm_modules/{path.relative_to(modules_dir).as_posix()}"
    except ValueError:
        return str(path)


def _rekey_requests(
    binding: Any,
    renames: dict[str, str],
    dev: dict[str, bool],
    visit_order: dict[str, int] | None = None,
) -> bool:
    """Name each replacement request's parent by its re-anchored key (req-rpl-003).

    A re-anchored parent's request also takes the parent's new dev status,
    as install records the edge. Requests follow the post-uninstall resolver
    order (*visit_order*, keyed by the old parent keys): a root request first,
    then each parent as install visits it (#4999).
    """
    if binding is None or not binding.requests:
        return False
    rank = visit_order or {}
    ordered = sorted(
        binding.requests,
        key=lambda request: -1 if request.parent is None else rank.get(request.parent, len(rank)),
    )
    rekeyed = {
        replace(
            request,
            parent=renames.get(request.parent, request.parent),
            dev=dev.get(request.parent, request.dev),
        ): None
        for request in ordered
    }
    if list(rekeyed) == list(binding.requests):
        return False
    binding.requests = rekeyed
    return True


def _rename_ledger_owners(lockfile, renames: dict[str, str], writers=None) -> None:
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...core.deployment_state import DeploymentLedger

    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    records = {}
    for key, record in ledger.records.items():
        owners = tuple(renames.get(owner, owner) for owner in record.owners)
        active = renames.get(record.active_owner, record.active_owner)
        value = DeploymentLedgerCodec.legacy_value(record.locator).rstrip("/")
        if writer := (writers or {}).get(value):
            owners, active = (writer,), writer  # the writer a fresh install records (#5048)
        records[key] = (
            replace(record, owners=owners, active_owner=active)
            if owners != record.owners or active != record.active_owner
            else record
        )
    lockfile.deployment_ledger = DeploymentLedger(records=records)


def apply_local_reanchor(lockfile, plan: LocalReanchorPlan, dev: dict | None = None) -> bool:
    """Apply *plan* to the in-memory lock; the caller persists it once.

    *dev* maps an old key to the dev status install now records for it.
    """
    if not plan:
        return False
    renames = {old: new for old, new in plan.renames.items() if old != new}
    for key, (local_path, parent_key, anchored, repo_url) in plan.identities.items():
        row = lockfile.dependencies[key]
        row.local_path, row.declaring_parent, row.anchored_local_path = (
            local_path,
            parent_key,
            anchored,
        )
        row.repo_url = repo_url
    for key, (parent_key, local_path) in plan.declaring_parents.items():
        row = lockfile.dependencies[key]
        row.declaring_parent = parent_key
        if local_path is not None:
            row.local_path = local_path
    for key, parent in plan.resolved_by.items():
        if key in lockfile.dependencies and parent is not None:
            lockfile.dependencies[key].resolved_by = parent
    if renames:
        _rename_ledger_owners(lockfile, renames, plan.writers)
        rows = list(lockfile.dependencies.items())
        lockfile.dependencies.clear()
        for key, row in rows:
            if row.declaring_parent in renames:
                row.declaring_parent = renames[row.declaring_parent]
            if row.resolved_by in renames:
                row.resolved_by = renames[row.resolved_by]
            lockfile.dependencies[renames.get(key, key)] = row
    for row in lockfile.dependencies.values():
        _rekey_requests(row.replacement, renames, dev or {}, plan.visit_order)
    return True


def snapshot_for_rollback(plan: LocalReanchorPlan, paths: tuple[Path, ...]) -> LocalReanchorPlan:
    """Record *paths* as they are now, so a failed receipt rename rolls them back."""
    if not plan.renames:
        return plan
    return replace(
        plan,
        snapshot=tuple(
            # Decoded bytes, not read_text(): a CRLF file restores byte for byte.
            (path, path.read_bytes().decode("utf-8"))
            for path in paths
            if path.is_file()
        ),
    )


def roll_back(plan: LocalReanchorPlan) -> list[str]:
    """Restore apm.yml, apm.lock.yaml and the receipts to the snapshot; return what was not.

    Every path is attempted. One that cannot be restored keeps its snapshot
    beside it (``<name>.apm-reanchor-backup``) and is named in the result, so
    a failed rollback is never silent.
    """
    from ...integration.write_seam import atomic_write_text

    failures: list[str] = []
    for path, text in plan.snapshot:
        # A file still holding its snapshot is not rewritten: a read-only
        # receipt directory must not fail a rollback that changed nothing.
        with contextlib.suppress(OSError):
            if path.is_file() and path.read_bytes() == text.encode("utf-8"):
                continue
        try:
            atomic_write_text(path, text, normalize_line_endings=False)
        except OSError as exc:
            kept = ""
            # Never overwrite an earlier backup: each failed rollback keeps its own.
            for attempt in range(100):
                suffix = "" if attempt == 0 else f".{attempt}"
                backup = path.with_name(f"{path.name}.apm-reanchor-backup{suffix}")
                if backup.exists() or backup.is_symlink():
                    continue
                try:
                    atomic_write_text(backup, text, normalize_line_endings=False, create_only=True)
                except FileExistsError:
                    continue
                except OSError:
                    break
                kept = f"; its earlier content is kept in {backup}"
                break
            failures.append(f"{path} ({exc}{kept})")
    return failures


def refreshed_local_sources(lockfile, modules_dir: Path, source_root: Path) -> list[str]:
    """Direct local packages whose source no longer matches their materialized copy.

    Uninstall re-copies those before it rebuilds survivors, which a re-anchor
    rollback could not undo, so the re-anchor refuses them first (read-only).
    """
    from ...install.phases.local_content import local_source_matches_copy

    stale = []
    for key, row in sorted(lockfile.dependencies.items()):
        if row.source != "local" or row.depth != 1 or not row.local_path:
            continue
        try:
            ref = row.to_dependency_ref()
            copy = ref.get_install_path(modules_dir)
            source = (source_root / ref.local_path).resolve()
            if copy.is_dir() and not local_source_matches_copy(row, source, copy):
                stale.append(key)
        except (OSError, ValueError, TypeError):
            stale.append(key)
    return stale


# -- uninstall command steps (#4889) ----------------------------------------


def _refuse_unverifiable_local_survivors(
    lockfile, packages: list, apm_yml_path, logger, *, dry_run: bool = False, quiet: bool = False
) -> list:
    """Refuse a local removal a surviving local package still reaches while apm_modules/ is gone.

    Without the installed graph the still-needed split cannot run, and dropping
    a still-needed local row would break the next frozen install (#4889). The
    survivors' local sources are read offline; only a reaching or unreadable
    chain refuses.
    """
    from ...core.remediation import RESYNC_DEPLOYED
    from .local_survivors import read_survivor_direct_refs

    project_root = Path(apm_yml_path).parent
    local = {}
    unresolved = []
    for package in packages:
        row = lockfile.get_dependency(_removed_lock_key(package, lockfile))
        if row is not None and row.source == "local":
            if not row.local_path:
                unresolved.append(package)  # no target to check: unverifiable
                continue
            raw = Path(row.local_path).expanduser()
            try:
                directory = raw.resolve() if raw.is_absolute() else (project_root / raw).resolve()
            except (OSError, RuntimeError):
                unresolved.append(package)  # unverifiable: never dropped from the check
                continue
            local.setdefault(directory, []).append(package)
    if not local and not unresolved:
        return []
    direct_refs, dev_refs = read_survivor_direct_refs(apm_yml_path, packages)
    from ...deps.reachability import local_sources_reach

    # Replacement rules can turn a remote survivor into a local source; the
    # offline check does not evaluate them, so it cannot prove anything unused.
    if (
        not unresolved
        and not lockfile.replacements
        and not local_sources_reach((*direct_refs, *dev_refs), project_root, set(local))
    ):
        return []
    kept = [package for packages in local.values() for package in packages] + unresolved
    if dry_run:
        # A preview writes nothing: it shows the package kept, as the real run would.
        for package in () if quiet else kept:
            logger.warning(
                f"Dry run: Would keep {_dependency_public_label(package)}: apm_modules/ is "
                "missing, so APM cannot confirm that no other local package still needs it; "
                "the uninstall itself refuses."
            )
        return kept
    logger.error(
        f"{_dependency_public_label(kept[0])} was not removed: apm_modules/ "
        "is missing, so APM cannot confirm whether another local package still needs it; "
        f"apm.yml and apm.lock.yaml were not changed. {RESYNC_DEPLOYED.sentence()}"
    )
    sys.exit(1)


def _plan_reanchor_or_exit(
    lockfile,
    decision,
    still_needed: list,
    apm_yml_path,
    logger,
    *,
    others: list = (),
    shrinks=dict,
):
    """Plan re-anchoring of still-needed local packages; refuse before any write (#4889)."""
    from ...core.deployment_reanchor import reanchor_receipt_problems
    from ...core.scope import InstallScope
    from ...deps.lockfile import get_lockfile_path
    from .local_reanchor import (
        LocalReanchorRefused,
        plan_local_reanchor,
        refreshed_local_sources,
        snapshot_for_rollback,
    )
    from .local_reanchor_writers import carried_writers

    project_root = apm_yml_path.parent
    labels = {_removed_lock_key(package, lockfile): package for package in still_needed}

    def label(key: str) -> str:
        return _dependency_public_label(labels.get(key, key))

    try:
        plan = plan_local_reanchor(lockfile, decision, project_root, frozenset(labels), label=label)
        if plan.renames and (shrunk := sorted(shrinks())):
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays, but the remaining packages select "
                f"fewer skills or targets of {shrunk[0]}, which would remove deployed files"
            )
        if plan.renames and (
            changed := refreshed_local_sources(lockfile, project_root / "apm_modules", project_root)
        ):
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays, but the source of {changed[0]} "
                "changed since it was installed, and refreshing its copy cannot be undone; "
                "reinstall first"
            )
        if plan.renames and (blocked := _unrelocatable(lockfile, plan, project_root)):
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays, but {blocked}, which cannot be "
                "carried to its new identity in one transaction yet"
            )
        if plan.renames and _native_owned(lockfile):
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays, but native plugin registrations are "
                "reconciled separately, so it cannot be re-anchored in one transaction"
            )
        if plan.renames and others:
            # Rollback restores apm.yml and the lock, never deleted content, so a
            # re-anchor runs alone.
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays under the package that needs it, "
                "so it is re-anchored on its own; uninstall "
                f"{', '.join(_dependency_public_label(package) for package in others)} "
                "in a separate command"
            )
        if plan.renames:
            # Last: it replays the lock offline, so cheaper refusals go first (#5048).
            writers = carried_writers(
                lockfile, plan, project_root, label, dict(decision.rescued_depths)
            )
            plan = replace(plan, writers=writers)
        problems = reanchor_receipt_problems(
            project_root, InstallScope.PROJECT, lockfile, plan.renames, plan.writers
        )
        if problems:
            raise LocalReanchorRefused(
                f"{label(next(iter(plan.renames)))} stays, but APM cannot prove it wrote "
                f"{', '.join(problems[:3])}: the deployment receipt is missing or does not "
                "match its owner"
            )
    except LocalReanchorRefused as exc:
        logger.error(f"{exc}; apm.yml and apm.lock.yaml were not changed.")
        logger.progress(
            exc.next_step
            or "Uninstall the package that still needs it first, or keep the local "
            "declaration in apm.yml."
        )
        sys.exit(1)
    if not plan.renames:
        return plan
    from ...core.deployment_ledger import materialization_evidence_paths

    # The receipts go back with the lock: an interrupt after their rename never
    # leaves lock and receipts naming different owners (#5077).
    receipts, _identity = materialization_evidence_paths(project_root, InstallScope.PROJECT)
    return snapshot_for_rollback(plan, (apm_yml_path, get_lockfile_path(project_root), receipts))


def _unrelocatable(lockfile, plan, project_root: Path) -> str | None:
    """Why a re-anchor cannot keep its deployments intact, or ``None`` (#4889).

    Merged hooks are rebuilt from the materialization the re-key moves, and a
    file another package also claims may change writer; both would lose or
    change deployed state that a rollback cannot restore.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from .local_survivors import survivor_ships_hooks

    modules_dir = Path(project_root) / "apm_modules"
    for key in plan.renames:
        if survivor_ships_hooks(lockfile, key, modules_dir):
            return f"{key} ships hooks"
    # LSP provenance names the owning package by its lock key, which would go stale.
    owners = {f"package:{key}" for key in plan.renames}
    if owners & set((getattr(lockfile, "lsp_config_provenance", None) or {}).values()):
        return "it supplies a language server"
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        if len(record.owners) > 1 and any(owner in plan.renames for owner in record.owners):
            return f"it shares {DeploymentLedgerCodec.legacy_value(record.locator)} with another package"
    if linked := _links_into_moving_copy(lockfile, plan, Path(project_root)):
        return f"{linked} links into its apm_modules copy"
    return None


def _links_into_moving_copy(lockfile, plan, project_root: Path) -> str | None:
    """A recorded deployed file that names a copy the re-anchor moves (#4999).

    Rendered links (a package's in-package assets, or the project's own skill
    linking through apm_modules) point at or into the copy, so moving it would
    leave them dangling until the next install rewrites them. Only paths the
    deploy-path gate accepts are read; any other claim refuses too.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...utils.paths import portable_relpath

    spellings = set()
    for current, _target in getattr(plan, "moves", ()):
        relpath = portable_relpath(current, project_root)
        spellings.update((relpath, relpath.replace("/", "\\")))
    if not spellings:
        return None
    # The copy's own path, not a longer sibling name such as ``_local/e2``.
    literal = re.compile(
        b"(?:" + b"|".join(re.escape(s.encode()) for s in sorted(spellings)) + b")(?![\\w.-])"
    )
    dirs = _moving_dirs(plan.moves)

    def names(member: Path) -> bool:
        data = member.read_bytes()
        return bool(literal.search(data)) or _names_moving_dir(data, member, project_root, dirs)

    for deployed in sorted(DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)):
        if "://" not in deployed and _claim_names_copy(deployed, project_root, names):
            return deployed
    return None


def _claim_names_copy(deployed: str, project_root: Path, names) -> bool:
    """Whether a claim names the moving copy, or cannot be read safely (refuse)."""
    from ...integration.base_integrator import BaseIntegrator

    def gated(claim: str) -> bool:
        return BaseIntegrator.validate_deploy_path(claim, project_root)

    if not gated(deployed):
        return True
    path = project_root / deployed
    try:
        members = (
            [
                (f"{deployed.rstrip('/')}/{member.relative_to(path).as_posix()}", member)
                for member in sorted(path.rglob("*"))
            ]
            if path.is_dir()
            else [(deployed, path)]
        )
        return any(
            not gated(claim) or (member.is_file() and names(member)) for claim, member in members
        )
    except OSError:
        return True  # an unreadable file cannot be cleared


# A path-like token: anything between whitespace, brackets, quotes and separators.
_PATH_TOKEN = re.compile(r"[^\s()<>\[\]\"'`|,;*]+")
# A whole Markdown link destination: ``<...>`` may hold spaces; a bare one may
# hold punctuation such as commas and balanced parentheses, so neither is split
# like prose. One level of nesting keeps the match linear.
_MARKDOWN_DESTINATION = re.compile(r"\]\(\s*(?:<([^<>\n]+)>|((?:[^\s()<>]|\([^\s()<>]*\))+))")


def _moving_dirs(moves) -> tuple[str, ...]:
    """Each moving copy as written and as resolved (a symlinked apm_modules root).

    Compared by the case-folded, NFC spelling key (#5158): a case or Unicode
    variant of a link refuses like the exact spelling. On a case-sensitive
    volume that may refuse a link to a distinct look-alike directory; a
    refusal changes nothing, so it fails closed.
    """
    dirs = set()
    for current, _target in moves:
        dirs.add(spelling_key(os.path.normpath(os.fspath(current))))
        dirs.add(spelling_key(os.path.realpath(current)))
    return tuple(sorted(dirs))


def _names_moving_dir(data: bytes, member: Path, project_root: Path, dirs) -> bool:
    """Whether a path token in *data* names a moving copy once normalized (#5133).

    Whole Markdown link destinations are checked first, then prose tokens. Each
    token resolves against the file's directory and the project root:
    ``.`` and ``..`` segments are folded, and the result is compared both as
    written and through symlinks, so ``_local/./e`` or a link through a
    symlinked apm_modules root counts as the copy itself.
    """

    def under(candidate: str) -> bool:
        candidate = spelling_key(candidate)
        return any(candidate == d or candidate.startswith(d + "/") for d in dirs)

    text = data.decode("utf-8", "replace")
    destinations = [bracketed or bare for bracketed, bare in _MARKDOWN_DESTINATION.findall(text)]
    seen: set[str] = set()
    for token in (*destinations, *_PATH_TOKEN.findall(text)):
        path = token.split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
        # A bare destination is also a prose token: resolve each spelling once.
        if path in seen:
            continue
        seen.add(path)
        # A binary asset can hold a NUL, which no filesystem path accepts.
        if "/" not in path or "://" in path or "\x00" in path:
            continue
        # Sentence punctuation after a link ("see _local/./e.") is not part of it.
        while path.endswith((".", ":")) and not path.endswith(".."):
            path = path[:-1]
        for base in (member.parent, project_root):
            joined = os.path.join(base, path)
            # Lexical folding and symlink resolution are separate views: folding
            # ``..`` first would skip a symlinked apm_modules root.
            if under(os.path.normpath(joined)) or under(os.path.realpath(joined)):
                return True
    return False


def _native_owned(lockfile) -> bool:
    from ...claude_plugins.registrar import native_ownership

    return bool(native_ownership(lockfile))


def _refuse_reanchor_refresh(repair, refreshed_keys, logger) -> None:
    """Backstop: a re-anchor never runs with a survivor refresh that deletes files (#4889).

    Only apm.yml is written at this point, so the rollback is complete.
    """
    plan = repair.reanchor
    if not plan or not plan.renames or not refreshed_keys:
        return
    _roll_back_and_exit(
        plan,
        logger,
        "A remaining package must be redeployed, which removes its deployed files, so the "
        "still-needed local package was not re-anchored",
    )


def _roll_back_and_exit(plan, logger, reason: str):
    """Roll a re-anchor back, report the outcome (never silently partial) and exit 1."""
    from ...core.remediation import RESYNC_DEPLOYED
    from .local_reanchor import roll_back

    if failures := roll_back(plan):
        logger.error(
            f"{reason}; these could not be restored: {', '.join(failures)}. Every deployed "
            f"file is kept. {RESYNC_DEPLOYED.sentence()}"
        )
    else:
        logger.error(f"{reason}; apm.yml and apm.lock.yaml were restored and every file is kept.")
    sys.exit(1)


def _abort_unsaved_reanchor(repair, logger) -> None:
    """A re-anchor whose lock was not saved restores apm.yml and exits nonzero (#4889)."""
    plan = repair.reanchor
    if not plan or not plan.renames:
        return
    _roll_back_and_exit(
        plan,
        logger,
        "The lockfile was not saved, so the re-anchored package was not kept under its new parent",
    )


def _rename_reanchored_receipts(repair, lockfile_path, deploy_root, scope, logger) -> None:
    """After the lock commits, carry receipts to the re-anchored owners (#4889)."""
    plan = repair.reanchor
    if not plan or not plan.renames:
        return
    from ...core.deployment_reanchor import rename_receipt_owners
    from ...deps.lockfile import LockFile

    try:
        committed = LockFile.read(lockfile_path)
        renamed = {new for old, new in plan.renames.items() if old != new}
        if committed is None or not renamed <= committed.dependencies.keys():
            return  # the lock was not committed; receipts still match it
        rename_receipt_owners(deploy_root, scope, committed, plan.renames, plan.writers)
    except Exception as exc:
        # One transaction: without its receipts the re-anchor does not stand.
        _roll_back_and_exit(
            plan,
            logger,
            f"Deployment receipts could not be moved to the re-anchored package ({exc})",
        )


def _move_reanchored_dirs(repair, modules_dir, logger) -> tuple:
    """Move re-anchored roots' apm_modules directories before the lock commits (#4999).

    A copy with stale hash-excluded files is swapped for a verified fresh copy;
    the old one waits aside until the lock commits (#5102).
    """
    plan = repair.reanchor
    if not plan or not plan.moves:
        return ()
    staged: list[Path] = []
    try:
        moves = _fresh_copy_moves(plan, modules_dir, staged)
        return move_materializations(moves, modules_dir)
    except (OSError, ValueError) as exc:
        left = _discard_dirs(staged, modules_dir)
        note = f"; the staged copy {', '.join(left)} could not be removed" if left else ""
        _roll_back_and_exit(
            plan,
            logger,
            f"The re-anchored package's apm_modules directory could not be moved ({exc}){note}",
        )
    except BaseException as exc:
        # An interrupt names any staged copy it could not remove, never silently.
        _undo_reanchor(
            exc, (), modules_dir, repair, logger, left=_discard_dirs(staged, modules_dir)
        )
        raise


_FRESH = ".apm-reanchor-fresh-"
_ASIDE = ".apm-reanchor-aside-"


def _fresh_copy_moves(plan, modules_dir: Path, staged: list[Path]) -> tuple:
    """The renames of a re-anchor, with each stale copy replaced by a fresh one (#5102).

    A fresh copy is staged next to the old one through install's own image
    owner and must hash as the old copy does; then the old copy moves aside
    and the fresh one takes its re-anchored path. A copy that cannot be staged
    and verified keeps the plain move (the next install rebuilds it, as today).
    """
    fresh = {current: (ref, framed) for current, ref, framed in plan.fresh}
    moves = []
    for current, target in plan.moves:
        stage = current.with_name(f"{_FRESH}{current.name}")
        aside = current.with_name(f"{_ASIDE}{current.name}")
        # lexists: a dangling symlink there is in the way too, never written through.
        if current not in fresh or os.path.lexists(stage) or os.path.lexists(aside):
            moves.append((current, target))
            continue
        # Registered before the copy starts, so an interrupt never strands it.
        staged.append(stage)
        if not _stage_fresh_copy(stage, current, *fresh[current], modules_dir):
            if _discard_dirs([stage], modules_dir):
                # Still registered: the caller reports it and nothing commits.
                raise OSError(f"its staged copy {_modules_path(stage, modules_dir)} remains")
            staged.pop()
            moves.append((current, target))
            continue
        moves += [(current, aside), (stage, target)]
    return tuple(moves)


def _stage_fresh_copy(stage: Path, current: Path, ref, framed: bool, modules_dir: Path) -> bool:
    """Write install's image of *ref* to *stage*; whether it hashes as *current* does.

    Install drops a stale ``.apm-pin`` from an unmarkable (local) copy, which
    the hash does not see, so the staged image gets the same cleanup first.
    """
    from ...install.cache_pin import MARKER_FILENAME, _remove_marker
    from ...install.phases.local_content import (
        LocalPackageError,
        compute_local_package_hash,
        materialize_local_package,
    )
    from ...utils.path_security import PathTraversalError

    try:
        materialize_local_package(ref, stage, modules_dir.parent)
        _remove_marker(stage, modules_dir)
        if os.path.lexists(stage / MARKER_FILENAME):
            return False
        return compute_local_package_hash(stage, framed=framed) == compute_local_package_hash(
            current, framed=framed
        )
    except (LocalPackageError, PathTraversalError, OSError, ValueError, TypeError):
        return False


def _discard_dirs(paths, modules_dir: Path) -> list[str]:
    """Remove staged or set-aside copies inside apm_modules; return those left behind."""
    from ...utils.path_security import safe_rmtree

    left: list[str] = []
    for path in paths:
        try:
            if path.exists():
                safe_rmtree(path, modules_dir)
        except (OSError, ValueError):
            left.append(_modules_path(path, modules_dir))
    return left


def _fresh_swaps(moved: tuple) -> list[tuple[Path, Path]]:
    """``(aside, stage)`` of each fresh-copy swap in a move journal (#5102).

    A swap is exactly two journal entries in a row: the old copy moved to its
    aside path, then the staged copy moved into place. A package whose own
    name merely looks like a staging path is a plain move and never matches.
    """
    from itertools import pairwise

    swaps = []
    for (current, aside, _), (stage, _target, _created) in pairwise(moved):
        if aside == current.with_name(f"{_ASIDE}{current.name}") and stage == current.with_name(
            f"{_FRESH}{current.name}"
        ):
            swaps.append((aside, stage))
    return swaps


def discard_set_aside_copies(moved: tuple, modules_dir, logger) -> None:
    """After the lock commits, remove the stale copies a re-anchor set aside (#5102)."""
    aside = [aside for aside, _stage in _fresh_swaps(moved)]
    if left := _discard_dirs(aside, Path(modules_dir)):
        logger.warning(f"Could not remove the replaced copy {', '.join(left)}; delete it manually.")


# The lock was not saved: the re-anchor rolls back, as for any non-exit failure.
_UNSAVED_LOCK = RuntimeError("the lockfile was not saved")


def _undo_reanchor(exc: BaseException, moved: tuple, modules_dir, repair, logger, left=()) -> None:
    """Move the re-anchored directories back; anything but a handled exit also restores
    apm.yml and the lock, so an interrupt (Ctrl-C) never leaves half a re-anchor.

    The manifest rollback runs even when a directory cannot be moved back, and
    anything left unrestored (or *left* behind by staging) is named and exits
    1, never silently.
    """
    unrestored: list[str] = list(left)
    try:
        unrestored += _restore_reanchored_dirs(moved, modules_dir)
    finally:
        if not isinstance(exc, SystemExit) and repair.reanchor:
            unrestored += roll_back(repair.reanchor) or []
    _report_unrestored(unrestored, logger)


def _report_unrestored(unrestored: list[str], logger) -> None:
    """Name what a re-anchor rollback could not restore and exit 1 (#4999)."""
    if not unrestored:
        return
    from ...core.remediation import RESYNC_DEPLOYED

    logger.error(
        f"The re-anchor did not complete and these could not be restored: "
        f"{', '.join(unrestored)}. Every deployed file is kept. {RESYNC_DEPLOYED.sentence()}"
    )
    sys.exit(1)


def _restore_reanchored_dirs(moved: tuple, modules_dir) -> list[str]:
    """Undo the directory moves of a re-anchor whose transaction did not commit.

    A fresh copy staged for a stale one (#5102) goes back to its staging path
    and is removed; the old copy returns from aside.
    """
    if not moved:
        return []
    unrestored = restore_materializations(moved, modules_dir)
    staged = [stage for _aside, stage in _fresh_swaps(moved)]
    return unrestored + _discard_dirs(staged, Path(modules_dir))
