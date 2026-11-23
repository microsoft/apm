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
from pathlib import Path
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
    writers: dict[str, str] = {}
    for path, rendered in sorted(providers.items()):
        record = records.get(path)
        if record is None or not reranked.intersection(rendered):
            continue
        if any(key not in rank for key in rendered):
            raise LocalReanchorRefused(_changes(label, plan, path, "has a writer APM cannot order"))
        writer = max(rendered, key=rank.__getitem__)
        if writer == record.active_owner:
            continue
        if _shared_path_rule(record) != _LAST_WINS or len(record.owners) != 1:
            raise LocalReanchorRefused(_changes(label, plan, path, "would change its writer"))
        if rendered[writer] != _digest(project_root / path):
            raise LocalReanchorRefused(_changes(label, plan, path, "would change its writer"))
        writers[path] = writer
    return writers


def _changes(label, plan, path: str, reason: str) -> str:
    return f"{label(next(iter(plan.renames)))} stays, but {path} {reason}"


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
            providers.setdefault(path, {})[key] = _digest(scratch_root / path)

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


def _file_records(post: Any) -> dict[str, Any]:
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...core.deployment_state import LocatorKind

    return {
        DeploymentLedgerCodec.legacy_value(record.locator): record
        for record in DeploymentLedgerCodec.from_lockfile(post).records.values()
        if record.locator.kind != LocatorKind.URI
    }


def _shared_path_rule(record: Any) -> str | None:
    """The rule the owning integrator declares for the record's primitive, if any."""
    from ...integration.skill_integrator import SkillIntegrator
    from ...integration.targets import KNOWN_TARGETS

    target = KNOWN_TARGETS.get(record.locator.target or "")
    if target is None or "skills" not in target.primitives:
        return None
    root = target.skills_rel_root.rstrip("/") + "/"
    return SkillIntegrator.SHARED_PATH_RULE if record.locator.value.startswith(root) else None


def _digest(path: Path) -> str:
    """The bytes' SHA-256, a directory marker, or empty for a missing path."""
    if path.is_dir() and not path.is_symlink():
        return _DIRECTORY
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
