# pylint: disable=duplicate-code
"""Orphan-package detection helpers extracted from _helpers.py."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..constants import APM_DIR, APM_MODULES_DIR, APM_YML_FILENAME
from ..utils.path_security import PathTraversalError, validate_path_segments


def _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir: Path) -> set:
    """Build expected package paths under *apm_modules_dir*.

    Combines direct deps (from ``apm.yml``) with transitive deps
    (depth > 1 from ``apm.lock``), using ``get_install_path()`` for
    consistency with how packages are actually installed.
    """
    expected = set()
    for dep in declared_deps:
        install_path = dep.get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(relative_path.as_posix())
        except ValueError:
            expected.add(str(install_path))

    if lockfile:
        for dep in lockfile.get_package_dependencies():
            if dep.depth is not None and dep.depth > 1:
                dep_ref = dep.to_dependency_ref()
                install_path = dep_ref.get_install_path(apm_modules_dir)
                try:
                    relative_path = install_path.relative_to(apm_modules_dir)
                    expected.add(relative_path.as_posix())
                except ValueError:
                    pass
    return expected


def _expand_with_ancestors(
    paths: Iterable[str],
    installed: Iterable[str] | None = None,
    validate_segments=validate_path_segments,
) -> set[str]:
    """Expand a set of expected paths to include ancestor prefixes.

    Given ``{"owner/repo/.apm/skills/my-skill"}``, returns a set containing
    the original path plus all intermediate path prefixes with 2+ segments
    (e.g., ``"owner/repo"``, ``"owner/repo/.apm"``,
    ``"owner/repo/.apm/skills"``, plus the original
    ``"owner/repo/.apm/skills/my-skill"``).
    This allows O(1) membership checks when determining whether a scanned
    directory is an ancestor of an expected package path.

    Ancestor expansion exists because a subdirectory dependency
    (``git: owner/repo, path: .apm/skills/x``) is installed by cloning the
    entire repo to ``apm_modules/owner/repo/``. Intermediate filesystem
    directories created by that clone are required parts of the install --
    not stale leftovers.

    Real-orphan safety: when *installed* is supplied, an ancestor that
    matches one of the installed paths is NOT added to the expansion
    unless that path is also directly declared in *paths*. Callers should
    pass only the subset of installed paths that look like *real
    standalone packages* (i.e., directories that ship their own
    ``apm.yml``) -- not filesystem intermediaries (which typically have
    only a ``.apm/`` subtree from a cloned subdir dep). This preserves
    orphan detection for the case where a user has a genuinely orphaned
    ``owner/repo`` package on disk alongside a declared sibling
    subdirectory dep (``owner/repo/.apm/skills/foo``): only filesystem
    intermediaries are suppressed, never real installed packages.

    Security contract -- ancestor depth cap: ``get_install_path()``
    anchors installs at the 2-segment repo root (GitHub) or 3-segment
    root (ADO). Anything deeper is a filesystem-intermediary path
    (``.apm/``, ``skills/``, ...) that ``_scan_installed_packages``
    skips, so emitting ancestors past depth 3 would only widen the
    orphan-suppression surface without serving any real lookup. The
    loop is therefore capped at depth 3 (``min(4, len(parts))``), which
    bounds the number of paths an attacker-influenced ``apm.yml`` dep
    declaration can hide from orphan detection. If the install strategy
    ever grows deeper roots, lift this cap and document the new
    invariant here.

    Traversal guard: any input path that fails
    :func:`apm_cli.utils.path_security.validate_path_segments` (which
    rejects both ``.`` and ``..`` segments after backslash
    normalisation) is kept in the result as-is (membership check) but
    produces no ancestors. Routing through the canonical guard --
    rather than a hand-rolled ``".." in parts`` check -- ensures
    single-dot segments (``owner/./repo``) are also caught and keeps
    the project's path-validation contract centralised.
    """
    materialized = list(paths)
    materialized_set = set(materialized)
    expanded = set(materialized)
    installed_set = set(installed) if installed is not None else set()
    for p in materialized:
        try:
            validate_segments(p, context="ancestor expansion")
        except PathTraversalError:
            continue
        # Normalise backslashes so Windows-style tokens split into the
        # same parts as POSIX inputs for the depth-capped loop below.
        normalised = p.replace("\\", "/")
        parts = normalised.split("/")
        # Cap at depth 3 -- the ADO install-root depth -- to bound the
        # ancestor-suppression surface (see security contract above).
        for i in range(2, min(4, len(parts))):
            ancestor = "/".join(parts[:i])
            # Do not mask a real installed package via ancestor expansion;
            # only filesystem intermediaries should be added. A real
            # installed package that is also directly declared remains in
            # expanded via materialized_set.
            if ancestor in installed_set and ancestor not in materialized_set:
                continue
            expanded.add(ancestor)
    return expanded


def _scan_installed_packages(apm_modules_dir: Path) -> list:
    """Scan *apm_modules_dir* for installed package paths.

    Walks the tree to find directories containing ``apm.yml`` or ``.apm``,
    supporting GitHub (2-level), ADO (3-level), and subdirectory packages.

    Returns:
        List of ``"owner/repo"`` or ``"org/project/repo"`` path keys.
    """
    installed: list = []
    if not apm_modules_dir.exists():
        return installed
    for candidate in apm_modules_dir.rglob("*"):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        if not ((candidate / APM_YML_FILENAME).exists() or (candidate / APM_DIR).exists()):
            continue
        rel_parts = candidate.relative_to(apm_modules_dir).parts
        if len(rel_parts) >= 2:
            installed.append("/".join(rel_parts))
    return installed


def _standalone_installed_packages(
    installed: Iterable[str], apm_modules_dir: Path, lockfile=None
) -> list:
    """Filter *installed* to entries that look like real standalone packages.

    Determination order (tamper-evident first):

    1. Path appears as a dependency key in *lockfile* -- the canonical
       record of what APM installed. The lockfile is integrity-checked
       and not forgeable by dropping/omitting files in ``apm_modules/``.
    2. Fallback: path has its own ``apm.yml``. Used when the lockfile
       is absent (older installs / fresh checkouts) or does not list
       the key. A directory with only a ``.apm/`` marker is treated as
       a filesystem intermediary, not a standalone package.

    Combining both signals closes the suppression-via-absence gap
    (panel finding: forgeable ``apm.yml`` heuristic) while preserving
    behaviour for projects that pre-date the lockfile or have not yet
    re-installed.

    Failure mode: only narrowly-typed shape errors against
    ``lockfile.dependencies`` (``AttributeError`` / ``TypeError`` /
    ``KeyError``) are absorbed and degrade to the ``apm.yml``-only
    fallback. Any other exception (e.g. lockfile parse / I/O failure)
    propagates so the outer caller can decide whether to log or fail
    closed -- preventing a corrupted or attacker-crafted lockfile from
    silently disabling the tamper-evident standalone check.
    """
    lockfile_keys: set[str] = set()
    if lockfile is not None:
        try:
            for dep_key in lockfile.dependencies:
                if dep_key:
                    lockfile_keys.add(dep_key)
        except (AttributeError, TypeError, KeyError):
            lockfile_keys = set()
    standalone: list = []
    for p in installed:
        if p in lockfile_keys:
            standalone.append(p)
            continue
        if (apm_modules_dir / p / APM_YML_FILENAME).exists():
            standalone.append(p)
    return standalone


def _check_orphaned_packages():
    """Check for packages in apm_modules/ that are not declared in apm.yml or apm.lock.

    Considers both direct dependencies (from apm.yml) and transitive dependencies
    (from apm.lock) as expected packages, so transitive deps are not falsely
    flagged as orphaned.

    Returns:
        List[str]: List of orphaned package names in org/repo or org/project/repo format
    """
    try:
        if not Path(APM_YML_FILENAME).exists():
            return []

        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            return []

        try:
            from ..deps.lockfile import LockFile, get_lockfile_path
            from ..models.apm_package import APMPackage

            apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
            expected = _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir)
        except Exception:
            return []

        installed = _scan_installed_packages(apm_modules_dir)
        # Combined lockfile-membership + apm.yml fallback determines
        # which installed paths are real standalone packages (and so
        # must NOT be masked by ancestor expansion). The lockfile is
        # the canonical, tamper-evident record; apm.yml-existence is
        # the fallback for projects without a lockfile yet.
        # See _expand_with_ancestors for the user-safety rationale.
        standalone_installed = _standalone_installed_packages(
            installed, apm_modules_dir, lockfile=lockfile
        )
        expected_with_ancestors = _expand_with_ancestors(expected, standalone_installed)
        # Sort for deterministic, diffable output across runs (rglob
        # traversal order is filesystem-dependent).
        return sorted(p for p in installed if p not in expected_with_ancestors)
    except Exception:
        return []
