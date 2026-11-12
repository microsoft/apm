"""Cold-restore adoption of unchanged deployed skill trees (#3391, #3625).

Without any local deployment receipt for it, an existing skill directory is
adopted only when its whole file tree equals what this install would write:
every admitted source member after final link rendering, with no extra or
missing files. During ``apm update`` the tree may instead equal what the
currently locked version renders, materialized from its locked commit, so
unchanged committed output can be replaced by the new version. The verified
on-disk hash then stands in for the missing baseline, so the guarded write
path rewrites the files and records receipts. Lockfile hashes are never
consulted. Any difference returns no adoption, so the caller keeps its
fail-closed refusal.
"""

from __future__ import annotations

import filecmp
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.content_hash import compute_file_hash

if TYPE_CHECKING:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan

ResolveLinks = Callable[..., tuple[str, int]]
WalkSource = Callable[[Path], Iterator[tuple[str, list[str], list[str]]]]


def is_skill_dir_identical_to_source(dir_a: Path, dir_b: Path) -> bool:
    """Check if two directory trees have identical raw file contents."""
    return dircmp_equal(filecmp.dircmp(str(dir_a), str(dir_b)))


def dircmp_equal(dcmp) -> bool:
    """Recursively check if dircmp shows identical contents."""
    if dcmp.left_only or dcmp.right_only or dcmp.funny_files:
        return False
    _, mismatches, errors = filecmp.cmpfiles(
        dcmp.left, dcmp.right, dcmp.common_files, shallow=False
    )
    if mismatches or errors:
        return False
    return all(dircmp_equal(sub_dcmp) for sub_dcmp in dcmp.subdirs.values())


def render_skill_markdown(
    resolve_links: ResolveLinks, source_file: Path, target_file: Path, source_root: Path
) -> tuple[str, int]:
    """Return the rendered markdown text and the number of links it rewrites.

    The deployer writes the rendered text only when the count is nonzero;
    otherwise the member stays a byte copy of its source.
    """
    content = source_file.read_text(encoding="utf-8")
    return resolve_links(content, source_file, target_file, preserved_source_root=source_root)


def _expected_bytes(
    resolve_links: ResolveLinks, source_file: Path, target_file: Path, source_root: Path
) -> bytes:
    if target_file.suffix == ".md":
        rendered, count = render_skill_markdown(
            resolve_links, source_file, target_file, source_root
        )
        if count:
            return normalize_crlf_to_lf(rendered).encode("utf-8")
    return source_file.read_bytes()


def adoptable_skill_members(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
) -> frozenset[Path]:
    """Return destination members when the tree equals this install's output, else empty.

    Call before copying so earlier writes in the same pass cannot complete a
    tree that was not identical on disk.
    """
    if destination.is_symlink() or not destination.is_dir():
        return frozenset()
    expected: dict[Path, Path] = {}
    for directory, directories, filenames in walk_source(source):
        ignored = set(ignore(directory, [*directories, *filenames]))
        directories[:] = [name for name in directories if name not in ignored]
        for name in filenames:
            if name not in ignored:
                member = Path(directory) / name
                expected[destination / member.relative_to(source)] = member
    actual: set[Path] = set()
    for directory, directories, filenames in os.walk(destination):
        if any((Path(directory) / name).is_symlink() for name in (*directories, *filenames)):
            return frozenset()
        actual.update(Path(directory) / name for name in filenames)
    if not expected or actual != set(expected):
        return frozenset()
    try:
        for target, member in expected.items():
            if member.is_symlink() or not member.is_file() or not target.is_file():
                return frozenset()
            if target.read_bytes() != _expected_bytes(resolve_links, member, target, source):
                return frozenset()
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(expected)


def adoption_baselines(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
    owned: Callable[[Path], bool],
) -> dict[Path, str]:
    """Map adoptable members to verified on-disk hashes when every member is claimed.

    Identical files without an ownership claim never gain local authority.
    """
    members = adoptable_skill_members(
        source, destination, walk_source=walk_source, ignore=ignore, resolve_links=resolve_links
    )
    if not all(owned(path) for path in members):
        return {}
    return {path: compute_file_hash(path) for path in members}


class LockedSkillSources:
    """Materialize each updated package's currently locked source at most once (#3625).

    *fetch* downloads the locked commit of one owner into a private scratch
    directory and returns its package info, or ``None`` when the lockfile has
    no immutable remote pin for it. Any failure yields no plan, so callers
    keep their refusal.
    """

    def __init__(self, fetch: Callable[[str, Path], Any]) -> None:
        self._fetch = fetch
        self._scratch: Path | None = None
        self._plans: dict[str, tuple[DeployableSourcePlan, Any] | None] = {}

    def plan(
        self, owner: str, current: DeployableSourcePlan, targets: Sequence[Any]
    ) -> DeployableSourcePlan | None:
        """Return the locked source authorized like *current*, or ``None``."""
        if owner not in self._plans:
            self._plans[owner] = self._materialize(owner, current, targets)
        materialized = self._plans[owner]
        return materialized[0] if materialized is not None else None

    def package(self, owner: str) -> Any | None:
        """Return the materialized locked package info, once planned."""
        materialized = self._plans.get(owner)
        return materialized[1] if materialized is not None else None

    def _materialize(
        self, owner: str, current: DeployableSourcePlan, targets: Sequence[Any]
    ) -> tuple[DeployableSourcePlan, Any] | None:
        from apm_cli.config import get_apm_temp_dir
        from apm_cli.install.deployable_source_plan import DeployableSourcePlan

        try:
            if self._scratch is None:
                self._scratch = Path(
                    tempfile.mkdtemp(prefix="apm-locked-source-", dir=get_apm_temp_dir())
                )
            package = self._fetch(owner, self._scratch / str(len(self._plans)))
            if package is None:
                return None
            names = current.selected_skill_names
            plan = DeployableSourcePlan.create(
                package,
                list(targets),
                skill_subset=tuple(sorted(names)) if names is not None else None,
                hooks_approved=False,
                canvas_approved=False,
                skip_bin=False,
                package_name=owner,
            )
            return plan, package
        except (OSError, RuntimeError, ValueError):
            return None

    def close(self) -> None:
        """Remove the private scratch directory."""
        from apm_cli.utils.file_ops import robust_rmtree

        if self._scratch is not None:
            robust_rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


def locked_skill_sources(ctx: Any) -> LockedSkillSources | None:
    """Fetch currently locked sources on demand, only for ``apm update`` runs."""
    if not ctx.update_refs or ctx.existing_lockfile is None or ctx.downloader is None:
        return None
    from apm_cli.drift import build_download_ref

    lockfile = ctx.existing_lockfile
    downloader = ctx.downloader

    def fetch(owner: str, destination: Path) -> Any:
        locked = lockfile.get_dependency(owner)
        if locked is None or locked.source in ("local", "registry"):
            return None
        reference = build_download_ref(
            locked.to_dependency_ref(),
            lockfile,
            update_refs=False,
            ref_changed=False,
            require_locked=True,
        )
        if reference.reference != locked.resolved_commit:
            return None
        return downloader.download_package(reference, destination)

    return LockedSkillSources(fetch)


def skill_tree_baselines(
    integrator: Any,
    source: Path,
    destination: Path,
    *,
    ignore: Callable[[str, list[str]], list[str]],
    owned: Callable[[Path], bool],
    force: bool,
    plan: DeployableSourcePlan | None = None,
    owner: str | None = None,
    targets: Sequence[Any] | None = None,
) -> dict[Path, str]:
    """Return adoption baselines for a skill tree without any local receipt.

    The single decision shared by skill preflight and materialization: compare
    with the source being installed first, then, during update, with the
    currently locked source rendered under the same copy filter. *plan*,
    *owner* and *targets* default to the integrator's current package.
    """
    session = integrator._materialization_session
    if session is None or force or session.has_receipt(destination / "SKILL.md"):
        return {}
    plan = plan or integrator._skill_source_plan
    owner = owner or integrator._skill_current_owner
    targets = targets if targets is not None else integrator._skill_targets or ()
    compare = partial(adoption_baselines, walk_source=integrator.walk_source_tree, owned=owned)
    adopted = compare(source, destination, ignore=ignore, resolve_links=integrator.resolve_links)
    locked_sources: LockedSkillSources | None = session.locked_sources
    rebased = getattr(ignore, "rebased", None)
    if adopted or locked_sources is None or plan is None or owner is None or rebased is None:
        return adopted
    try:
        relative = source.resolve().relative_to(plan.source_root)
    except ValueError:
        return {}
    locked = locked_sources.plan(owner, plan, targets)
    locked_source = locked.source_root / relative if locked is not None else None
    if locked_source is None or locked_source.is_symlink() or not locked_source.is_dir():
        return {}
    # Render as the earlier deployment did: locked links, current deployment frame.
    resolve_links = integrator.projected_resolve_links(locked_sources.package(owner))
    return compare(
        locked_source,
        destination,
        ignore=rebased(locked),
        resolve_links=resolve_links or integrator.resolve_links,
    )
