"""The one local-dependency content verdict for install --frozen and audit --ci (#4447)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.scope import manifest_file

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.reference import DependencyReference


def local_content_reasons(
    apm_package: APMPackage,
    lockfile: LockFile,
    manifest_deps: list[DependencyReference],
) -> list[str]:
    """Name selected local dependencies whose content differs from the lock.

    The one local-content verdict that ``install --frozen`` and the ``apm
    audit --ci`` replay apply (#4447).
    """
    from collections import deque
    from dataclasses import replace

    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.path_anchoring import (
        LocalResolutionError,
        build_local_parent_index,
        resolve_local_dep_dir,
    )
    from apm_cli.install.phases.local_content import compute_local_package_hash
    from apm_cli.install.plan import lockfile_satisfies_manifest
    from apm_cli.install.service import InstallService
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.replacement import build_replacement_index

    queue = deque(dep for dep in manifest_deps if dep.is_local)
    queue.extend(InstallService._selected_locked_replacements(lockfile, manifest_deps))
    if not queue:
        return []
    package_root = Path(apm_package.package_path).resolve()
    consumer_root = apm_package.source_path or package_root
    rules = build_replacement_index(apm_package.replace_rules or [])
    parent_index = build_local_parent_index(lockfile)
    resolved_cache: dict[str, Path] = {}
    seen: set[str] = set()
    reasons: list[str] = []
    while queue:
        declared = queue.popleft()
        key = declared.get_unique_key()
        if key in seen:
            continue
        seen.add(key)
        satisfied, missing = lockfile_satisfies_manifest(lockfile, [declared])
        if not satisfied:
            reasons.extend(missing)
            continue
        dependency = lockfile.get_dependency(key)
        try:
            source = resolve_local_dep_dir(
                dependency,
                lockfile,
                consumer_root,
                parent_index=parent_index,
                resolved_cache=resolved_cache,
            )
            selected_source = source
            if source.is_file():
                # Admission must see the selected leaf, not a resolved symlink.
                source_base = resolve_local_dep_dir(
                    replace(dependency, local_path="."),
                    lockfile,
                    package_root,
                    parent_index=parent_index,
                )
                selected_source = source_base / Path(dependency.local_path).expanduser()
            observed_hash = compute_local_package_hash(
                selected_source, framed=dependency.content_hash_framed
            )
            expected_hash = dependency.content_hash
            # v0.1 readers also accept the identical SHA-256 digest without its envelope.
            if (
                observed_hash != expected_hash
                and observed_hash.removeprefix("sha256:") != expected_hash
            ):
                reasons.append(
                    f"  - local dependency {declared.local_path!r} content differs "
                    "from its locked snapshot"
                )
                continue
            # Only selected, verified local manifests can introduce further
            # local declarations. Do not inspect orphan lock entries or
            # expand local paths declared by untrusted remote packages.
            if manifest_file(source).is_file():
                package = APMPackage.from_apm_yml(manifest_file(source), source_path=source)
                for child in package.get_apm_dependencies():
                    child = rules.bind(
                        child,
                        consumer_root=consumer_root,
                        modules_dir=package_root / "apm_modules",
                        parent=key,
                        is_dev=dependency.is_dev,
                    )
                    if not child.is_local:
                        continue
                    if child.replacement is not None:
                        queue.append(child)
                        continue
                    anchored = APMDependencyResolver._compute_dep_source_path(
                        child, package, source
                    )
                    child = replace(
                        child,
                        declaring_parent=key,
                        anchored_local_path=APMDependencyResolver._portable_anchor_identity(
                            anchored, apm_package.source_path or package_root
                        ),
                    )
                    queue.append(child)
        except (LocalResolutionError, OSError, ValueError) as exc:
            reasons.append(
                f"  - local dependency {declared.local_path!r} cannot be verified: {exc}"
            )
    return reasons
