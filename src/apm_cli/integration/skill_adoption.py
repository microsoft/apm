"""Cold-restore adoption of unchanged deployed skill trees (#3391, #3625, #3883).

Without a local deployment receipt trusted for the current lock claim (none
in a new worktree, or one written under another checkout's lock), an existing
skill directory is adopted only when its whole file tree equals what this
install would write: every admitted source member after final link
rendering, with no extra or missing files. When the commit being installed
differs from the locked one (``apm update``, or ``apm install`` after an
``apm.yml`` ref change), the tree may instead equal what the currently locked
version renders, materialized from its locked commit, so unchanged committed
output can be replaced by the new version. The verified on-disk hash then
stands in for the missing baseline, so the guarded write path rewrites the
files and records receipts. Lockfile hashes never grant a baseline. Any
difference returns no adoption, so the caller keeps its fail-closed refusal.
"""

from __future__ import annotations

import filecmp
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.deps.carried_license import is_license_file_name
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
    otherwise the member stays a byte copy of its source. Licence files
    (``LICENSE.md`` and friends, #3556) are never read or rewritten, so they
    deploy as uninterpreted byte copies whatever their encoding.
    """
    if is_license_file_name(source_file.name):
        return "", 0
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


def _rendered_members(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
) -> dict[Path, Path]:
    """Map each destination member this install writes to its admitted source member."""
    expected: dict[Path, Path] = {}
    for directory, directories, filenames in walk_source(source):
        ignored = set(ignore(directory, [*directories, *filenames]))
        directories[:] = [name for name in directories if name not in ignored]
        for name in filenames:
            if name not in ignored:
                member = Path(directory) / name
                expected[destination / member.relative_to(source)] = member
    return expected


def kept_copy_is_stale(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
    recorded: Iterable[Path],
) -> bool:
    """Return whether a kept managed copy differs from what this install renders there (#4692).

    The current source is rendered for *destination* itself, so links stay
    destination-relative. Stale when an admitted member is missing or renders
    other bytes, or when a *recorded* member is no longer rendered.
    """
    if destination.is_symlink() or not destination.is_dir():
        return False
    expected = _rendered_members(source, destination, walk_source=walk_source, ignore=ignore)
    try:
        for target, member in expected.items():
            if target.is_symlink() or not target.is_file():
                return True
            if target.read_bytes() != _expected_bytes(resolve_links, member, target, source):
                return True
    except (OSError, UnicodeError):
        return True
    return any(path not in expected for path in recorded)


def adoptable_skill_members(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
    trusted: Callable[[Path], str | None] | None = None,
) -> frozenset[Path]:
    """Return destination members when the tree equals this install's output, else empty.

    A member matches when its bytes equal the render or its own *trusted*
    receipt (bytes this installation wrote there). Call before
    copying so earlier writes in the same pass cannot complete a tree that
    was not identical on disk.
    """
    if destination.is_symlink() or not destination.is_dir():
        return frozenset()
    expected = _rendered_members(source, destination, walk_source=walk_source, ignore=ignore)
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
            receipted = trusted(target) if trusted is not None else None
            if receipted is not None and compute_file_hash(target) == receipted:
                continue
            if target.read_bytes() != _expected_bytes(resolve_links, member, target, source):
                return frozenset()
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(expected)


def tree_equals_install_output(
    integrator: Any, source: Path, destination: Path, ignore: Callable[[str, list[str]], list[str]]
) -> bool:
    """Return whether *destination* already holds exactly what this install writes."""
    return bool(
        adoptable_skill_members(
            source,
            destination,
            walk_source=integrator.walk_source_tree,
            ignore=ignore,
            resolve_links=integrator.resolve_links,
        )
    )


def adoption_baselines(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
    owned: Callable[[Path], bool],
    trusted: Callable[[Path], str | None] | None = None,
) -> dict[Path, str]:
    """Map adoptable members to verified on-disk hashes when every member is claimed.

    Identical files without an ownership claim never gain local authority.
    """
    members = adoptable_skill_members(
        source,
        destination,
        walk_source=walk_source,
        ignore=ignore,
        resolve_links=resolve_links,
        trusted=trusted,
    )
    if not all(owned(path) for path in members):
        return {}
    return {path: compute_file_hash(path) for path in members}


def all_files_receipted(destination: Path, held: Callable[[Path], bool]) -> bool:
    """Return whether every file under *destination* still holds its receipted bytes."""
    if destination.is_symlink() or not destination.is_dir():
        return True
    return all(
        not (Path(directory) / name).is_symlink() and held(Path(directory) / name)
        for directory, _directories, filenames in os.walk(destination)
        for name in filenames
    )


def installing_commit(package_info: Any) -> str | None:
    """Return the commit this install materializes for *package_info*, when known."""
    return getattr(getattr(package_info, "resolved_reference", None), "resolved_commit", None)


class LockedSkillSources:
    """Materialize each changed package's currently locked source at most once (#3625).

    *fetch* downloads the locked commit of one owner into a private scratch
    directory and returns its package info, or ``None`` when the lockfile has
    no immutable remote pin for it. *locked_commit* names that commit, so an
    install of the same commit skips the redundant download. Any failure
    yields no plan, so callers keep their refusal.
    """

    def __init__(
        self,
        fetch: Callable[[str, Path], Any],
        locked_commit: Callable[[str], str | None] | None = None,
    ) -> None:
        self._fetch = fetch
        self._locked_commit = locked_commit
        self._scratch: Path | None = None
        self._plans: dict[str, tuple[DeployableSourcePlan, Any] | None] = {}

    def plan(
        self,
        owner: str,
        current: DeployableSourcePlan,
        targets: Sequence[Any],
        installing: str | None = None,
    ) -> DeployableSourcePlan | None:
        """Return the locked source authorized like *current*, or ``None``.

        ``None`` too when *installing* is the locked commit: its render is
        the current source's, which the caller already compared.
        """
        if owner not in self._plans:
            unchanged = (
                installing is not None
                and self._locked_commit is not None
                and self._locked_commit(owner) == installing
            )
            self._plans[owner] = None if unchanged else self._materialize(owner, current, targets)
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
        from apm_cli.utils.file_ops import robust_rmtree  # write-seam-exempt: scratch

        if self._scratch is not None:
            robust_rmtree(self._scratch, ignore_errors=True)  # write-seam-exempt: scratch
            self._scratch = None


def locked_skill_sources(ctx: Any) -> LockedSkillSources | None:
    """Fetch currently locked sources on demand whenever an install has a lockfile (#3883)."""
    if ctx.existing_lockfile is None or ctx.downloader is None:
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

    def locked_commit(owner: str) -> str | None:
        locked = lockfile.get_dependency(owner)
        return locked.resolved_commit if locked is not None else None

    return LockedSkillSources(fetch, locked_commit)


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
    commit: str | None = None,
) -> dict[Path, str]:
    """Return adoption baselines for members of a skill tree without a trusted receipt.

    The single decision shared by skill preflight and materialization: compare
    with the source being installed first, then, when *commit* (the one being
    installed) differs from the lock, with the currently locked source
    rendered under the same copy filter. *plan*, *owner*, *targets* and
    *commit* default to the integrator's current package. A tree whose files
    all still hold the bytes this installation receipted needs no adoption;
    the session's baseline decides it. A missing, or no longer held, receipt
    (another worktree or checkout) falls through to the renders, where a
    member matches its trusted receipt or the render, so bytes equal to what
    APM renders are never refused or kept.
    """
    session = integrator._materialization_session
    if session is None or force:
        return {}
    plan = plan or integrator._skill_source_plan
    if owner is None:
        owner, commit = integrator._skill_current_owner, integrator._skill_current_commit
    targets = targets if targets is not None else integrator._skill_targets or ()

    def trusted(path: Path) -> str | None:
        return session.baseline(path, targets)

    if all_files_receipted(destination, session.receipt_held):
        return {}
    compare = partial(
        adoption_baselines, walk_source=integrator.walk_source_tree, owned=owned, trusted=trusted
    )
    adopted = compare(source, destination, ignore=ignore, resolve_links=integrator.resolve_links)
    locked_sources: LockedSkillSources | None = session.locked_sources
    rebased = getattr(ignore, "rebased", None)
    if adopted or locked_sources is None or plan is None or owner is None or rebased is None:
        return adopted
    try:
        relative = source.resolve().relative_to(plan.source_root)
    except ValueError:
        return {}
    locked = locked_sources.plan(owner, plan, targets, commit)
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


_NON_SKILL_MEMBERS = frozenset({"bin", ".claude-plugin"})


def _inactive_skill_roots(integrator: Any, project_root: Path) -> Iterator[tuple[Any, Path, bool]]:
    """Yield (profile, skills root, legacy layout) for skill targets outside this install."""
    from apm_cli.install.helpers.reconciliation_scope import (
        _scoped_known_targets_for_reconciliation,
    )
    from apm_cli.integration.skill_layout import resolve_skill_layout

    targets = list(integrator._skill_targets or ())
    active_names = {target.name for target in targets}
    active_roots = {
        target.skills_deploy_path(project_root).resolve()
        for target in targets
        if target.supports("skills")
    }
    profiles = _scoped_known_targets_for_reconciliation(
        user_scope=integrator.install_user_scope(), active_targets=targets, declared_targets=None
    )
    for name, profile in profiles.items():
        if name in active_names or not profile.supports("skills"):
            continue
        for candidate, legacy in (
            (profile, False),
            (resolve_skill_layout([profile], legacy_skill_paths=True)[0], True),
        ):
            if legacy and candidate is profile:
                continue
            root = candidate.skills_deploy_path(project_root)
            if root.resolve() not in active_roots:
                yield candidate, root, legacy


def report_stale_kept_copies(
    integrator: Any,
    source: Path,
    destination: Path,
    project_root: Path,
    *,
    ignore: Callable[[str, list[str]], list[str]],
    diagnostics: Any,
) -> None:
    """Name each recorded copy of this skill, in a target outside this install, left stale.

    A ``--target``-narrowed install keeps another target's managed copy
    without writing it; when that copy no longer equals what the current
    source renders there, say so with the refreshing command (#4692).
    """
    from apm_cli.utils.paths import portable_relpath

    owner = integrator._skill_current_owner
    if owner is None or diagnostics is None:
        return
    from apm_cli.core import remediation
    from apm_cli.core.scope import InstallScope, get_apm_dir

    user_scope = integrator.install_user_scope()
    lockfile_root = get_apm_dir(InstallScope.USER) if user_scope else project_root
    _, recorded_owners = integrator._ownership_maps(lockfile_root)
    for profile, root, legacy in _inactive_skill_roots(integrator, project_root):
        kept = root / destination.name
        if not kept.is_dir() or kept in integrator._stale_kept_reported:
            continue
        entry = portable_relpath(kept, project_root)
        if recorded_owners.get(entry) != owner:
            continue
        recorded = [
            project_root / value
            for value, value_owner in recorded_owners.items()
            if value_owner == owner
            and value.startswith(f"{entry}/")
            and value[len(entry) + 1 :].split("/", 1)[0] not in _NON_SKILL_MEMBERS
            and (project_root / value).is_file()
        ]
        if not kept_copy_is_stale(
            source,
            kept,
            walk_source=integrator.walk_source_tree,
            ignore=ignore,
            resolve_links=integrator.resolve_links,
            recorded=recorded,
        ):
            continue
        integrator._stale_kept_reported.add(kept)
        names = ",".join(
            dict.fromkeys([*(t.name for t in integrator._skill_targets), profile.name])
        )
        flags = f"{' -g' if user_scope else ''} --target {names}"
        remedy = remediation.KEPT_SKILL_REFRESH.bind(targets=names).with_command(
            f"apm install{flags}{' --legacy-skill-paths' if legacy else ''}"
        )
        diagnostics.warn(
            remediation.kept_file_message(
                f"stale skill copy {'~/' if user_scope else ''}{entry}",
                f"target '{profile.name}' is not part of this install, so it was not refreshed",
                remedy,
            )
        )
