"""Registry-backed outdated checks for ``apm outdated``.

Compares the lockfile's exact registry ``version`` against the highest semver
on the registry that satisfies the manifest range (same ``pick_best`` semantics
as install).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...constants import APM_MODULES_DIR, APM_YML_FILENAME
from ...deps.outdated_row import OutdatedRow
from ...marketplace.semver import SemVer, parse_semver
from ...models.dependency.reference import DependencyReference
from .auth import make_auth_context
from .client import RegistryClient, RegistryError
from .config_loader import resolve_effective_registries
from .feature_gate import is_package_registry_enabled
from .resolver import _split_owner_repo
from .semver import is_semver_range, pick_best

if TYPE_CHECKING:
    from ...deps.lockfile import LockedDependency, LockFile


@dataclass(frozen=True)
class RegistryOutdatedContext:
    """Manifest + registry config needed to check registry lockfile rows."""

    manifest_index: dict[str, DependencyReference]
    registries: dict[str, str]
    default_registry: str | None


def _highest_semver(version_strings: list[str]) -> str | None:
    """Return the highest parseable semver in *version_strings*."""
    candidates: list[tuple[SemVer, str]] = []
    for raw in version_strings:
        parsed = parse_semver(raw)
        if parsed is not None:
            candidates.append((parsed, raw))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def _add_registry_manifest_deps(
    apm_yml: Path,
    manifest_index: dict[str, DependencyReference],
    default_registry: str | None,
) -> None:
    """Merge registry-routed deps from one ``apm.yml`` into *manifest_index*."""
    from ...models.apm_package import APMPackage, _route_unscoped_to_default_registry

    if not apm_yml.is_file():
        return
    try:
        pkg = APMPackage.from_apm_yml(apm_yml)
    except (ValueError, FileNotFoundError, OSError):
        return

    dep_list = pkg.get_apm_dependencies() + pkg.get_dev_apm_dependencies()
    if default_registry:
        _route_unscoped_to_default_registry(dep_list, default_registry)
    for dep in dep_list:
        if dep.source != "registry":
            continue
        manifest_index.setdefault(dep.get_unique_key(), dep)


def _index_installed_manifest_deps(
    project_root: Path,
    lockfile: LockFile | None,
    manifest_index: dict[str, DependencyReference],
    default_registry: str | None,
) -> None:
    """Index registry manifest ranges declared by installed packages."""
    if lockfile:
        for locked in lockfile.dependencies.values():
            if locked.source != "local" or not locked.local_path:
                continue
            _add_registry_manifest_deps(
                project_root / locked.local_path / APM_YML_FILENAME,
                manifest_index,
                default_registry,
            )

    modules_dir = project_root / APM_MODULES_DIR
    if not modules_dir.is_dir():
        return
    for apm_yml in modules_dir.rglob(APM_YML_FILENAME):
        if ".apm" in apm_yml.parts:
            continue
        _add_registry_manifest_deps(apm_yml, manifest_index, default_registry)


def load_registry_outdated_context(
    project_root: Path,
    lockfile: LockFile | None = None,
) -> RegistryOutdatedContext:
    """Build manifest index and merged registry map from *project_root*."""
    from ...models.apm_package import APMPackage, _route_unscoped_to_default_registry

    apm_yml = project_root / "apm.yml"
    manifest_index: dict[str, DependencyReference] = {}
    project_registries: dict[str, str] | None = None
    project_default: str | None = None

    if apm_yml.is_file():
        pkg = APMPackage.from_apm_yml(apm_yml)
        project_registries = pkg.registries
        project_default = pkg.default_registry
        dep_list = pkg.get_apm_dependencies() + pkg.get_dev_apm_dependencies()
        merged, default_name = resolve_effective_registries(
            project_registries,
            project_default,
        )
        if default_name:
            _route_unscoped_to_default_registry(dep_list, default_name)
        for dep in dep_list:
            manifest_index[dep.get_unique_key()] = dep
        registries = merged or {}
        default_registry = default_name
    else:
        registries, default_registry = resolve_effective_registries(None, None)

    if registries is None:
        registries = {}

    _index_installed_manifest_deps(
        project_root,
        lockfile,
        manifest_index,
        default_registry,
    )

    return RegistryOutdatedContext(
        manifest_index=manifest_index,
        registries=registries,
        default_registry=default_registry,
    )


def _semver_lt(left: str, right: str) -> bool:
    vl, vr = parse_semver(left), parse_semver(right)
    if vl is None or vr is None:
        return left != right
    return vl < vr


def check_registry_locked_dep(
    locked: LockedDependency,
    ctx: RegistryOutdatedContext | None,
    *,
    client_factory=None,
    verbose: bool = False,
) -> OutdatedRow:
    """Compare *locked* against the newest registry version in manifest range."""
    package_name = locked.get_unique_key()
    current = locked.version or ""

    if ctx is None:
        return OutdatedRow(
            package=package_name,
            current=current or "(none)",
            latest="-",
            status="unknown",
            source="registry",
        )

    if not is_package_registry_enabled():
        return OutdatedRow(
            package=package_name,
            current=current or "(none)",
            latest="-",
            status="unknown",
            source="registry (feature disabled)",
        )

    manifest_dep = ctx.manifest_index.get(package_name)
    lockfile_only = manifest_dep is None
    manifest_range: str | None = None
    if manifest_dep is not None:
        manifest_range = manifest_dep.reference
        if not manifest_range or not is_semver_range(manifest_range):
            return OutdatedRow(
                package=package_name,
                current=current or "(none)",
                latest="-",
                status="unknown",
                source="registry (invalid manifest range)",
            )

    if not current:
        return OutdatedRow(
            package=package_name,
            current="(none)",
            latest="-",
            status="unknown",
            source="registry (missing locked version)",
        )

    registry_name = (manifest_dep.registry_name if manifest_dep else None) or ctx.default_registry
    if not registry_name:
        return OutdatedRow(
            package=package_name,
            current=current,
            latest="-",
            status="unknown",
            source="registry (no default registry)",
        )

    base_url = ctx.registries.get(registry_name)
    if not base_url:
        return OutdatedRow(
            package=package_name,
            current=current,
            latest="-",
            status="unknown",
            source=f"registry ({registry_name!r} not configured)",
        )

    source_label = f"registry: {registry_name}"
    if lockfile_only:
        source_label = f"{source_label} (lockfile)"

    try:
        owner, repo = _split_owner_repo(locked.repo_url)
    except Exception:
        return OutdatedRow(
            package=package_name,
            current=current,
            latest="-",
            status="unknown",
            source=source_label,
        )

    factory = client_factory or (lambda url, auth: RegistryClient(url, auth))
    client = factory(base_url, make_auth_context(registry_name))

    try:
        version_entries = client.list_versions(owner, repo)
    except RegistryError:
        return OutdatedRow(
            package=package_name,
            current=current,
            latest="-",
            status="unknown",
            source=source_label,
        )

    version_strings = [entry.version for entry in version_entries]
    if lockfile_only:
        latest = _highest_semver(version_strings)
    else:
        latest = pick_best(manifest_range, version_strings)
    if latest is None:
        return OutdatedRow(
            package=package_name,
            current=current,
            latest="-",
            status="unknown",
            source=source_label,
        )

    extra: list[str] = []
    if verbose:
        sorted_versions = sorted(
            version_strings,
            key=lambda s: parse_semver(s) or parse_semver("0.0.0"),
            reverse=True,
        )
        if lockfile_only:
            extra = [v for v in sorted_versions if _semver_lt(current, v)][:10]
        else:
            extra = [v for v in sorted_versions if pick_best(manifest_range, [v]) == v][:10]

    if current == latest:
        status = "up-to-date"
    elif _semver_lt(current, latest):
        status = "outdated"
    else:
        status = "up-to-date"

    return OutdatedRow(
        package=package_name,
        current=current,
        latest=latest,
        status=status,
        extra_tags=extra,
        source=source_label,
    )
