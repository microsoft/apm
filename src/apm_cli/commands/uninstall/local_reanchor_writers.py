"""The writer a fresh install records for a path a re-anchor re-ranks (#5048).

Re-anchoring moves a kept local package deeper in the graph, so it installs
later. A skill that another package also ships is last-installed-wins, so a
fresh install can record a different final writer than the lock holds.
Before any write, the post-rename lock is replayed offline through audit
replay (the install integrators), every package's own output is attributed,
and each re-ranked path gets the writer install's own order selects:

* the same writer as today: nothing to carry;
* a different writer whose rendered bytes equal the deployed bytes: the path
  is carried to that writer;
* anything else (different bytes, a primitive whose owner declares no
  shared-path rule, several recorded owners, a provider missing from the
  order, or a render that fails offline): the re-anchor refuses.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

_LAST_WINS = "last-installed-wins"
_DIRECTORY = "<directory>"


def carried_writers(
    lockfile: Any, plan: Any, project_root: Path, label, depths: dict[str, int]
) -> dict[str, str]:
    """Return ``{deployed path: new writer key}``; raise when a writer is unprovable.

    *depths* are the rescued rows' new depths (old keys), as uninstall applies them.
    """
    from .local_reanchor import LocalReanchorRefused, apply_local_reanchor

    renames = {old: new for old, new in plan.renames.items() if old != new}
    if not renames:
        return {}
    post = copy.deepcopy(lockfile)
    for key, depth in depths.items():
        if key in post.dependencies:
            post.dependencies[key].depth = depth
    apply_local_reanchor(post, plan)
    reranked = _reranked(lockfile, post, renames)
    providers = _render_providers(post, project_root)
    if providers is None:
        raise LocalReanchorRefused(
            f"{label(next(iter(plan.renames)))} stays, but the packages that share its deployed "
            "files could not be rendered offline to prove their writers"
        )
    rank = _install_rank(post)
    records = _file_records(post)
    skill_roots = _skill_roots()

    def refuse(path: str, reason: str) -> LocalReanchorRefused:
        return LocalReanchorRefused(f"{label(next(iter(plan.renames)))} stays, but {path} {reason}")

    if unproven := _unrendered_skill_claim(records, providers, post, skill_roots):
        # The bare render did not reproduce a recorded skill (another layout,
        # a dropped target): its writers cannot be proven.
        raise refuse(unproven, "was not reproduced by the offline render")
    writers: dict[str, str] = {}
    for path, rendered in sorted(providers.items()):
        if not reranked.intersection(rendered):
            continue
        if any(key not in rank for key in rendered):
            raise refuse(path, "has a writer APM cannot order")
        writer = max(rendered, key=rank.__getitem__)
        claims = records.get(path, ())
        if not claims:
            # A member only a directory claim covers: its bytes must not change.
            if _changes_unclaimed(path, rendered[writer], skill_roots, project_root):
                raise refuse(path, "has no claim that proves its writer")
            continue
        if len(claims) == 1 and writer == claims[0].active_owner:
            continue
        if not _carriable(claims, path, skill_roots) or rendered[writer] != _digest(
            project_root / path
        ):
            raise refuse(path, "would change its writer")
        writers[path] = writer
    if loose := _unproven_directory(writers, providers, records, project_root):
        raise refuse(loose, "would change its writer")
    return writers


def _carriable(claims: list, path: str, skill_roots: dict[str, set[str]]) -> bool:
    """One single-owner claim on a skill root one target owns, as the skill owner declares."""
    from ...integration.skill_integrator import SkillIntegrator

    root = next((root for root in skill_roots if path.startswith(root)), None)
    return (
        SkillIntegrator.SHARED_PATH_RULE == _LAST_WINS
        and root is not None
        and len(skill_roots[root]) == 1
        and len(claims) == 1
        and len(claims[0].owners) == 1
    )


def _changes_unclaimed(path, rendered: str, skill_roots, project_root: Path) -> bool:
    """Whether a fresh install would change an unclaimed skill member's bytes.

    Skill members merge into the directory, so the member's last provider
    writes it: its *rendered* bytes must equal the deployed bytes.
    """
    return any(path.startswith(root) for root in skill_roots) and rendered != _digest(
        project_root / path
    )


def _unrendered_skill_claim(records, providers, post, skill_roots) -> str | None:
    """A recorded skill claim the render did not attribute to its writer, if any."""
    for path, claims in sorted(records.items()):
        if not any(path.startswith(root) for root in skill_roots):
            continue
        for claim in claims:
            owner = claim.active_owner
            if owner in post.dependencies and owner not in providers.get(path, {}):
                return path
    return None


def _unproven_directory(writers, providers, records, project_root: Path) -> str | None:
    """A carried directory whose files are not all carried to, or owned by, its writer."""
    # Keys are normalized without a trailing "/", as every path index here is.
    members = {
        path.rstrip("/"): set()
        for path, writer in writers.items()
        if providers[path][writer] == _DIRECTORY
    }
    if not members:
        return None
    # One pass over the render: each file joins every carried ancestor directory.
    for child, by in providers.items():
        for parent in PurePosixPath(child).parents:
            directory = parent.as_posix()
            if directory in members and by.get(writers[directory]) not in (None, _DIRECTORY):
                members[directory].add(child)
    for path, rendered in sorted(members.items()):
        writer = writers[path]
        live = {
            child.relative_to(project_root).as_posix()
            for child in (project_root / path).rglob("*")
            if child.is_file() or child.is_symlink()
        }
        if live != rendered or any(
            writers.get(child) != writer
            and [claim.active_owner for claim in records.get(child, ())] != [writer]
            for child in rendered
        ):
            return path
    return None


def _reranked(before: Any, after: Any, renames: dict[str, str]) -> set[str]:
    """New keys of renamed rows, plus every row whose depth the re-anchor changes."""
    moved = set(renames.values())
    for key, row in after.dependencies.items():
        prior = before.dependencies.get(key)
        if prior is not None and prior.depth != row.depth:
            moved.add(key)
    return moved


def _render_providers(post: Any, project_root: Path) -> dict[str, dict[str, str]] | None:
    """``{path: {package key: digest}}`` from one offline replay of the whole lock.

    The full lock is replayed without its deployment claims: replay follows
    recorded ownership, which hides a provider the lock never recorded (a
    skill another package replaced). Each package's bytes are read right
    after it renders, before a later package overwrites them.
    """
    from ...core.deployment_state import DeploymentLedger
    from ...integration.cleanup import _render_locked
    from ...utils.diagnostics import DiagnosticCollector

    bare = copy.deepcopy(post)
    for row in bare.dependencies.values():
        row.deployed_files, row.deployed_file_hashes = [], {}
    bare.local_deployed_files, bare.local_deployed_file_hashes = [], {}
    bare.deployment_ledger = DeploymentLedger(records={})
    providers: dict[str, dict[str, str]] = {}

    def record(key: str, paths: list[str], scratch_root: Path) -> None:
        for path in paths:
            providers.setdefault(path.rstrip("/"), {})[key] = _digest(scratch_root / path)

    scratch = _render_locked(
        bare,
        None,
        Path(project_root),
        False,
        DiagnosticCollector(),
        cache_only=True,
        on_package=record,
        records=False,
    )
    return None if scratch is None else providers


def _install_rank(post: Any) -> dict[str, int]:
    """Each lock key's position in install's integration order (depth, unique key)."""
    from ...deps.apm_resolver import _select_dependency_winners
    from ...deps.dependency_graph import DependencyNode

    nodes = [
        DependencyNode(package=None, dependency_ref=row.to_dependency_ref(), depth=row.depth)
        for key, row in post.dependencies.items()
        if key != "."
    ]
    ordered, _winners = _select_dependency_winners(nodes)
    return {node.dependency_ref.get_unique_key(): index for index, node in enumerate(ordered)}


def _file_records(post: Any) -> dict[str, list]:
    """Every non-URI claim by deployed path (no trailing "/"); several targets, several."""
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...core.deployment_state import LocatorKind

    records: dict[str, list] = {}
    for record in DeploymentLedgerCodec.from_lockfile(post).records.values():
        if record.locator.kind != LocatorKind.URI:
            value = DeploymentLedgerCodec.legacy_value(record.locator).rstrip("/")
            records.setdefault(value, []).append(record)
    return records


def _skill_roots() -> dict[str, set[str]]:
    """Each skills root in either layout -> the targets that deploy skills there."""
    from ...integration.skill_layout import with_every_skill_layout
    from ...integration.targets import KNOWN_TARGETS

    roots: dict[str, set[str]] = {}
    for target in with_every_skill_layout(KNOWN_TARGETS.values()):
        if "skills" in target.primitives:
            roots.setdefault(target.skills_rel_root.rstrip("/") + "/", set()).add(target.name)
    return roots


def _digest(path: Path) -> str:
    """The bytes' SHA-256, a directory marker, or empty for a missing path."""
    if path.is_dir() and not path.is_symlink():
        return _DIRECTORY
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
