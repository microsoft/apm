"""Scratch-dir lifecycle and package-materialization helpers for drift detection.

Extracted from :mod:`drift` to keep that module under 400 lines.
All names continue to be importable from :mod:`apm_cli.install.drift`
via explicit re-exports.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import click

from apm_cli.utils.console import STATUS_SYMBOLS

from ._drift_types import CacheMissError


def _assert_scratch_bound(project_root: Path, scratch_root: Path) -> None:
    """Defense-in-depth: a scratch dir must NOT live inside the project tree.

    Prevents the replay engine from accidentally writing into the live
    project (which would defeat the read-only contract).
    """
    project_root = project_root.resolve()
    scratch_root = scratch_root.resolve()
    try:
        scratch_root.relative_to(project_root)
    except ValueError:
        return
    raise RuntimeError(
        f"drift scratch dir {scratch_root!s} is inside project tree "
        f"{project_root!s}; refusing to proceed"
    )


def _make_scratch_root(project_root: Path) -> Path:
    """Allocate a scratch dir outside the project tree, with atexit cleanup."""
    scratch = Path(tempfile.mkdtemp(prefix="apm_drift_"))
    _assert_scratch_bound(project_root, scratch)

    def _cleanup() -> None:
        try:
            shutil.rmtree(scratch, ignore_errors=False)
        except OSError as exc:
            click.echo(
                f"{STATUS_SYMBOLS['warning']} failed to clean drift scratch dir {scratch}: {exc}",
                err=True,
            )

    atexit.register(_cleanup)
    return scratch


def _materialize_install_path(
    lock_dep: LockedDependency,
    project_root: Path,
    apm_modules_dir: Path,
    cache_only: bool,
) -> Path:
    """Resolve the on-disk path for a locked dep's package contents.

    For local deps -- contents live at ``project_root / lock_dep.local_path``.
    For remote deps -- contents live at the canonical apm_modules subpath.

    Raises
    ------
    CacheMissError
        If ``cache_only`` is True and the path does not exist.
    NotImplementedError
        If ``cache_only`` is False (network-enabled replay is a follow-up).
    """
    if not cache_only:
        raise NotImplementedError("--no-cache replay requires auth wiring; tracked in follow-up")

    if lock_dep.source == "local":
        if not lock_dep.local_path:
            raise CacheMissError(f"local dep {lock_dep.repo_url!r} has no local_path in lockfile")
        candidate = (project_root / lock_dep.local_path).resolve()
        if not candidate.exists():
            raise CacheMissError(
                f"local source missing for {lock_dep.local_path!r}: expected {candidate}"
            )
        return candidate

    dep_ref = lock_dep.to_dependency_ref()
    candidate = dep_ref.get_install_path(apm_modules_dir)
    # Supply-chain fail-closed: a remote dep without a resolved_commit is
    # unverifiable -- there is no marker we can write at install time and
    # no commit we can compare at audit time. Refuse to replay it rather
    # than silently trust whatever happens to live in the cache.
    if getattr(lock_dep, "source", None) != "local" and not lock_dep.resolved_commit:
        raise CacheMissError(
            f"cannot replay {lock_dep.repo_url}: lockfile entry has no resolved_commit "
            "(cache freshness unverifiable). Re-run 'apm install' with a pinned ref "
            "(commit, tag, or specific branch HEAD) before audit."
        )
    if not candidate.exists():
        raise CacheMissError(
            f"cache miss for {lock_dep.repo_url}@{lock_dep.resolved_commit}: "
            f"expected {candidate}; run 'apm install' to populate the cache"
        )
    # Stale-cache detection: verify the cache pin marker matches the
    # lockfile's resolved_commit. Catches the "teammate bumped the
    # lockfile, didn't reinstall" + "shared CI runner reused stale
    # apm_modules" scenarios. Not defense against active tampering.
    if lock_dep.resolved_commit:
        from apm_cli.install.cache_pin import CachePinError, verify_marker

        try:
            verify_marker(candidate, lock_dep.resolved_commit)
        except CachePinError as exc:
            raise CacheMissError(f"{exc}; run 'apm install' to refresh apm_modules cache") from exc
    return candidate


def _build_package_info(
    lock_dep: LockedDependency,
    install_path: Path,
):
    """Construct a real ``PackageInfo`` for the integrators.

    Loads ``apm.yml`` when present so integrators that read
    ``package_info.package.name`` see the right package identity.
    """
    from apm_cli.models.apm_package import (
        APMPackage,
        GitReferenceType,
        PackageInfo,
        ResolvedReference,
    )
    from apm_cli.models.validation import detect_package_type

    apm_yml = install_path / "apm.yml"
    if apm_yml.exists():
        try:
            pkg = APMPackage.from_apm_yml(apm_yml, source_path=install_path)
        except Exception:
            pkg = APMPackage(
                name=install_path.name,
                version=lock_dep.version or "unknown",
                package_path=install_path,
                source=lock_dep.repo_url,
            )
        if not pkg.source:
            pkg.source = lock_dep.repo_url
    else:
        pkg = APMPackage(
            name=install_path.name,
            version=lock_dep.version or "unknown",
            package_path=install_path,
            source=lock_dep.repo_url,
        )

    resolved_ref = ResolvedReference(
        original_ref=lock_dep.resolved_ref or "locked",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=lock_dep.resolved_commit or "locked",
        ref_name=lock_dep.resolved_ref or "locked",
    )

    info = PackageInfo(
        package=pkg,
        install_path=install_path,
        resolved_reference=resolved_ref,
        dependency_ref=lock_dep.to_dependency_ref(),
    )
    try:
        pkg_type, _ = detect_package_type(install_path)
        info.package_type = pkg_type
    except Exception:
        info.package_type = None
    return info
