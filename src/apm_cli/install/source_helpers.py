"""Shared helpers for install dependency sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.scope import manifest_file

if TYPE_CHECKING:
    from apm_cli.deps.dependency_graph import DependencyNode
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import APMPackage, PackageType


def proxy_only_lock_entries(lockfile: Any, *, update_refs: bool, ctx: Any) -> list:
    """Return the lock entries a ``PROXY_REGISTRY_ONLY`` run keeps without re-resolving.

    A plain install keeps every entry. A full ref update re-resolves them all.
    A targeted update (``apm update PKG``) re-resolves only the selected subtree
    and merges the rest back, so those retained entries still need checking.
    """
    entries = list(lockfile.dependencies.values())
    if not update_refs:
        return entries
    if not getattr(ctx, "only_packages", None):
        return []
    reresolved = {dep.get_unique_key() for dep in getattr(ctx, "deps_to_install", ()) or ()}
    return [dep for dep in entries if dep.get_unique_key() not in reresolved]


def header_ref(resolved: Any, dep_ref: Any, locked: Any) -> str:
    """Return the ref the package header names for one resolved reference.

    A lockfile replay downloads the locked commit, so the downloader reports
    that commit as the ref. The header then names the declared ref, as a
    resolving install does, instead of repeating the commit (#4195).
    """
    ref_name = (getattr(resolved, "ref_name", None) or "") if resolved else ""
    commit = getattr(resolved, "resolved_commit", None) if resolved else None
    if ref_name and ref_name == commit and dep_ref.reference != commit:
        return getattr(locked, "resolved_ref", None) or dep_ref.reference or ""
    return ref_name


def lockfile_refresh_command(scope: Any) -> str:
    """Return the command that re-resolves dependencies and re-pins the lockfile."""
    from apm_cli.core.scope import InstallScope

    return "apm update -g" if scope is InstallScope.USER else "apm update"


def supply_chain_mismatch_hint(ctx: Any, dep_key: str) -> str:
    """Explain a lockfile content mismatch and name the command that re-pins it.

    A direct dependency gets the targeted ``apm update <dep_key>``: its key is
    the ``replace:``-projected unique key ``apm update`` matches. A transitive
    one cannot be named on that command line, so it gets the full refresh.
    """
    command = lockfile_refresh_command(ctx.scope)
    if dep_key in (getattr(ctx, "declared_direct_refs", None) or {}):
        return (
            f"This may indicate a supply-chain attack. Use '{command} {dep_key}' to "
            "accept new content for this package and re-pin it in the lockfile."
        )
    return (
        f"This may indicate a supply-chain attack. Use '{command}' to accept "
        "new content and update the lockfile."
    )


def content_hash_mismatch_hint(ctx: Any, dep_key: str) -> str:
    """Explain how to recover from a same-commit content mismatch.

    Only a moved commit may change content, so ``apm update`` cannot accept
    new bytes for the locked commit; re-pinning is deliberate (#4027).
    """
    from apm_cli.core.scope import InstallScope

    return locked_content_remediation(
        dep_key, user_scope=ctx.scope is InstallScope.USER, apm_dir=getattr(ctx, "apm_dir", None)
    )


def locked_content_remediation(dep_key: str, *, user_scope: bool, apm_dir: Path | None) -> str:
    """Remediation for a dependency whose bytes contradict its locked content hash."""
    from apm_cli.core.remediation import (
        CONTENT_HASH_CACHE_CLEAN,
        CONTENT_HASH_LOCK_RESTORE,
        CONTENT_HASH_LOCK_RESTORE_GLOBAL,
    )
    from apm_cli.deps.lockfile import get_lockfile_path

    restore = CONTENT_HASH_LOCK_RESTORE_GLOBAL if user_scope else CONTENT_HASH_LOCK_RESTORE
    lockfile = get_lockfile_path(apm_dir) if apm_dir is not None else "apm.lock.yaml"
    return (
        "This may indicate a supply-chain attack. "
        f"If the package copies changed, {CONTENT_HASH_CACHE_CLEAN.clause()}. "
        f"If {lockfile} changed, {restore.bind(lockfile=lockfile).clause()}."
    )


def locked_content_mismatch(
    dep_key: str,
    locked_dep: Any,
    package_path: Path,
    *,
    lockfile: Any,
    package_info: Any = None,
    exclude: Any = None,
    fresh_hash: str | None = None,
) -> bool:
    """Return whether a copy of *locked_dep* contradicts its locked ``content_hash``.

    The one content-hash verdict that ``apm install`` (with or without
    ``--frozen``) and the ``apm audit --ci`` replay apply to package bytes at
    the locked commit (#4187). *fresh_hash* is the digest this run already
    recorded; without it the bytes are verified in the row's stream. Carried
    licences come from *exclude*, else *package_info*, else the lock row. A
    0.28 marketplace plugin row may differ only by its normalization receipt.
    """
    expected = getattr(locked_dep, "content_hash", None)
    if not expected:
        return False
    if exclude is None:
        from apm_cli.deps.carried_license import carried_license_paths

        source = package_info if package_info is not None else locked_dep
        exclude = carried_license_paths(getattr(source, "carried_licenses", None) or ())
    if fresh_hash is not None:
        from apm_cli.utils.content_hash import same_package_digest

        if same_package_digest(fresh_hash, expected):
            return False
    else:
        from apm_cli.utils.content_hash import verify_package_hash

        framed = getattr(locked_dep, "content_hash_framed", True)
        if verify_package_hash(package_path, expected, exclude=exclude, framed=framed):
            return False
    from apm_cli.install.legacy_plugin_compat import matches_fresh_legacy_plugin_hash

    return not matches_fresh_legacy_plugin_hash(
        package_path,
        dep_key,
        lockfile=lockfile,
        package_type=getattr(package_info, "package_type", None),
        exclude=exclude,
    )


def locked_content_mismatch_line(dep_key: str, expected: str, observed: str) -> str:
    """The one diagnostic line for bytes that contradict a locked ``content_hash``.

    ``apm install`` (with or without ``--frozen``) and the ``apm audit --ci``
    replay name the entry, the locked value and the observed value the same way.
    """
    return f"Content hash mismatch for {dep_key}: expected {expected}, got {observed}."


def observed_package_hash(locked_dep: Any, package_path: Path, *, package_info: Any = None) -> str:
    """Digest *package_path* in the stream and exclusions *locked_dep* was recorded with."""
    from apm_cli.deps.carried_license import carried_license_paths
    from apm_cli.utils.content_hash import compute_package_hash

    source = package_info if package_info is not None else locked_dep
    exclude = carried_license_paths(getattr(source, "carried_licenses", None) or ())
    framed = getattr(locked_dep, "content_hash_framed", True)
    return compute_package_hash(package_path, exclude=exclude, framed=framed)


def locked_content_failure(
    dep_key: str,
    project_root: Path,
    locked_dep: Any,
    package_path: Path,
    *,
    package_info: Any = None,
) -> str:
    """Error for replayed bytes that contradict the lock, with install's remediation."""
    from apm_cli.core.scope import get_workspace_deploy_root

    user_scope = get_workspace_deploy_root(project_root) != project_root.resolve()
    observed = observed_package_hash(locked_dep, package_path, package_info=package_info)
    return (
        locked_content_mismatch_line(dep_key, locked_dep.content_hash, observed)
        + " The content at the locked commit differs from the lockfile record. "
        + locked_content_remediation(dep_key, user_scope=user_scope, apm_dir=project_root)
    )


def ref_update_operation_label() -> str:
    """Name the invoked ref-updating command (``update``, ``install --update``, ...)."""
    import click

    click_ctx = click.get_current_context(silent=True)
    if click_ctx is None:
        return "update"
    _, _, subcommand = click_ctx.command_path.partition(" ")
    if subcommand == "install":
        return "install --update"
    return subcommand or "update"


def locked_parent_identity(ctx: InstallContext, node: DependencyNode | None) -> str | None:
    """Use exact original parent keys in the replacement-enabled lock contract."""
    from apm_cli.deps.lockfile import lockfile_parent_identity

    if node is None or node.parent is None:
        return None
    return lockfile_parent_identity(
        node.parent.dependency_ref,
        ctx.apm_package.replace_rules if ctx.apm_package is not None else None,
    )


def materialized_manifest_snapshot(
    package: APMPackage,
    install_path: Path,
    package_type: PackageType | None = None,
) -> APMPackage:
    """Read acquired manifest metadata without reusing a pre-replacement descriptor.

    Native projections remain authoritative for their own formats. APM manifests
    are interpreted by the existing construction owner, with the original local
    source anchor, rather than by the process-lifetime path cache.
    """
    from apm_cli.models.apm_package import APMPackage, PackageType

    if (
        package.agent_plugin is not None
        or package.codex_plugin is not None
        or package_type == PackageType.MARKETPLACE_PLUGIN
    ):
        return package
    manifest = manifest_file(install_path)
    if not manifest.exists():
        return package

    import yaml

    from apm_cli.core.remediation import MATERIALIZED_MANIFEST_INVALID
    from apm_cli.install.errors import DirectDependencyError
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(manifest)
        if not isinstance(data, dict):
            raise ValueError("apm.yml must contain a YAML object")
        snapshot = APMPackage.from_mapping(
            data,
            package_path=install_path,
            source_path=package.source_path,
            manifest_path=manifest,
            warning_handler=None,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise DirectDependencyError(
            f"Invalid materialized manifest for {package.name}: {exc}. "
            f"{MATERIALIZED_MANIFEST_INVALID.sentence()}"
        ) from exc
    snapshot.source = package.source
    snapshot.resolved_commit = package.resolved_commit
    return snapshot


def format_package_type_label(pkg_type: Any) -> str | None:
    """Return a human-readable label for a detected ``PackageType``."""
    from apm_cli.models.apm_package import PackageType

    return {
        PackageType.CLAUDE_SKILL: "Skill (SKILL.md detected)",
        PackageType.AGENT_PLUGIN: "Agent Plugin (plugin.json)",
        PackageType.MARKETPLACE_PLUGIN: "Marketplace Plugin (plugin.json or agents/skills/commands)",
        PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
        PackageType.APM_PACKAGE: "APM Package (apm.yml)",
        PackageType.HOOK_PACKAGE: "Hook Package (hooks/*.json only)",
        PackageType.SKILL_BUNDLE: "Skill Bundle (skills/<name>/SKILL.md)",
    }.get(pkg_type)


def record_declared_license(ctx: Any, dep_key: str, install_path: Path) -> None:
    """Backfill ``ctx.package_declared_licenses`` from the resolved manifest."""
    try:
        from apm_cli.export.declared_license import read_declared_license

        declared = read_declared_license(install_path)
    except Exception:
        declared = None
    if declared:
        ctx.package_declared_licenses[dep_key] = declared


def record_package_targets(ctx: Any, dep_key: str, install_path: Path) -> None:
    """Record the package's own ``targets:`` restriction for the lockfile (#4850).

    An unreadable manifest records nothing, so pack keeps failing closed.
    """
    from apm_cli.core.scope import manifest_file
    from apm_cli.install.target_filter import recorded_package_targets
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.manifest_io import load_manifest

    manifest = manifest_file(install_path)
    try:
        # Parse uncached: this run may have rematerialized the manifest after it was cached.
        package = APMPackage.from_mapping(
            load_manifest(manifest),
            package_path=install_path,
            manifest_path=manifest,
            warning_handler=None,
            warn_deprecated=False,
        )
        recorded = recorded_package_targets(package)
    except Exception:
        return
    if recorded:
        ctx.package_targets[dep_key] = recorded


def carried_licenses_for(ctx: Any, dep_key: str, locked_dep: Any) -> tuple:
    """Return this run's carried licence records for *dep_key*, else the lock's (#3556)."""
    if dep_key in ctx.package_carried_licenses:
        return ctx.package_carried_licenses[dep_key]
    return tuple(getattr(locked_dep, "carried_licenses", ()) or ())


def package_digests(ctx: Any, package_path: Path, exclude: Any = ()) -> Any:
    """Digests to record: the legacy stream only while the existing lock uses it."""
    from apm_cli.deps.lockfile import records_framed_content_hashes
    from apm_cli.utils.content_hash import compute_package_digests

    framed = records_framed_content_hashes(getattr(ctx, "existing_lockfile", None))
    return compute_package_digests(package_path, exclude=exclude, legacy=not framed)


def record_package_digests(ctx: Any, dep_key: str, digests: Any) -> str:
    """Record one package's digests in the stream the existing lockfile uses.

    *digests* is a :class:`~apm_cli.utils.content_hash.PackageDigests` of the
    bytes this run accepted. The framed digest is kept beside it so the
    lockfile phase can promote the lock to version 4 without re-reading.
    """
    from apm_cli.deps.lockfile import records_framed_content_hashes

    framed = records_framed_content_hashes(getattr(ctx, "existing_lockfile", None))
    ctx.package_hashes[dep_key] = digests.select(framed=framed)
    ctx.package_framed_hashes[dep_key] = digests.framed
    return ctx.package_hashes[dep_key]


def record_carried_package_hash(
    ctx: Any,
    dep_key: str,
    install_path: Path,
    package_info: Any = None,
    *,
    locked_dep: Any = None,
) -> frozenset[str]:
    """Record carried licences (#3556) and the content hash that excludes them.

    A fresh download passes *package_info*; a cached package reuses this run's
    records or, when its copies are still intact, the lock entry's records.
    Returns the excluded package-relative paths.
    """
    from apm_cli.deps.carried_license import carried_copies_intact, carried_license_paths

    if package_info is not None:
        carried = tuple(getattr(package_info, "carried_licenses", ()) or ())
    else:
        carried = carried_licenses_for(ctx, dep_key, locked_dep)
        if carried and not carried_copies_intact(install_path, carried):
            carried = ()
    ctx.package_carried_licenses[dep_key] = carried
    excluded = carried_license_paths(carried)
    if install_path.is_dir():
        record_package_digests(ctx, dep_key, package_digests(ctx, install_path, excluded))
    return excluded


def locked_commit_unchanged(locked_dep: Any, commit: str | None) -> bool:
    """Return whether *commit* is the commit *locked_dep* records.

    A row without a commit on either side (registry or other commit-less
    rows) has no commit identity, so it never counts as unchanged (#4027).
    """
    locked_commit = getattr(locked_dep, "resolved_commit", None)
    return bool(commit not in (None, "", "cached") and commit == locked_commit)


def update_accepts_new_content(ctx: Any, locked_dep: Any, resolved_commit: str | None) -> bool:
    """Return whether this run may lock new content without the hash check.

    Only ``apm update`` may, and only for a commit that moved: an unchanged
    commit must still hold its locked content (#4027). The one exception is
    a lock row APM 0.28 wrote for a marketplace plugin, which hashed another
    materialization of the same commit (see ``legacy_plugin_compat``).
    """
    if ctx.update_refs and locked_commit_unchanged(locked_dep, resolved_commit):
        from apm_cli.install.legacy_plugin_compat import legacy_plugin_row_rehashable

        return legacy_plugin_row_rehashable(getattr(ctx, "existing_lockfile", None), locked_dep)
    return bool(ctx.update_refs and not locked_commit_unchanged(locked_dep, resolved_commit))


def locked_version_unchanged(locked_dep: Any, version: str | None) -> bool:
    """Return whether *version* is the registry version *locked_dep* records.

    A registry package is identified by its name and exact version, so an
    unchanged version must still hold its locked bytes (#4125).
    """
    return bool(
        version
        and getattr(locked_dep, "source", None) == "registry"
        and version == getattr(locked_dep, "version", None)
    )


def download_registry_package(ctx: Any, dep_ref: Any, target_path: Path, locked_dep: Any) -> Any:
    """Download *dep_ref* at its re-resolved registry version.

    A moved version may lock new content; an unchanged version must match
    the locked ``resolved_hash``, as an unchanged commit must match its
    locked content hash (#4125). A mismatch removes the download and fails
    closed before anything is deployed or locked.
    """
    from apm_cli.install.registry_wiring import get_registry_resolver

    resolver = get_registry_resolver(ctx)
    package_info = resolver.download_package(dep_ref, target_path)
    dep_key = dep_ref.get_unique_key()
    resolution = resolver.last_resolutions.get(dep_key)
    version = getattr(resolution, "version", None)
    expected = getattr(locked_dep, "resolved_hash", None)
    actual = getattr(resolution, "resolved_hash", None)
    if (
        not expected
        or not actual
        or not locked_version_unchanged(locked_dep, version)
        or expected.removeprefix("sha256:").lower() == actual.removeprefix("sha256:").lower()
    ):
        return package_info
    from apm_cli.install.errors import DirectDependencyError
    from apm_cli.utils.path_security import safe_rmtree

    safe_rmtree(target_path, ctx.apm_modules_dir)
    raise DirectDependencyError(
        f"Content hash mismatch for {dep_key} version {version}: "
        f"expected {expected}, got {actual}. "
        "The registry archive differs from the lockfile record. "
        + content_hash_mismatch_hint(ctx, dep_key)
    )


def ensure_carried_licenses_match(
    ctx: Any,
    dep_key: str,
    install_path: Path,
    locked_dep: Any,
    resolved_commit: str | None = None,
) -> None:
    """Fail closed when fresh carried licences differ from the lock record (#3556).

    The copies are excluded from content_hash, so the lock's carried records
    are their integrity baseline, as content_hash is for the package; ``apm
    update`` may change them only for a moved commit (#4027).
    """
    locked = getattr(locked_dep, "carried_licenses", ()) if locked_dep else ()
    if (
        update_accepts_new_content(ctx, locked_dep, resolved_commit)
        or dep_key in ctx.expected_hash_change_deps
        or not locked
        or frozenset(locked) == frozenset(ctx.package_carried_licenses.get(dep_key, ()))
    ):
        return
    from apm_cli.install.errors import DirectDependencyError
    from apm_cli.utils.path_security import safe_rmtree

    safe_rmtree(install_path, ctx.apm_modules_dir)
    raise DirectDependencyError(
        f"Licence file mismatch for {dep_key}: the licence files carried "
        "from the repository differ from the lockfile record. "
        + content_hash_mismatch_hint(ctx, dep_key)
    )


def rebuild_cached_semver_resolution(dep_locked_chk: Any) -> Any:
    """Rebuild a complete ``GitSemverResolution`` from a cached lockfile entry."""
    if dep_locked_chk is None:
        return None
    if not (
        dep_locked_chk.constraint
        and dep_locked_chk.version
        and dep_locked_chk.resolved_tag
        and dep_locked_chk.resolved_commit
    ):
        return None
    from apm_cli.deps.git_semver_resolver import GitSemverResolution

    return GitSemverResolution(
        constraint=dep_locked_chk.constraint,
        resolved_version=dep_locked_chk.version,
        resolved_tag=dep_locked_chk.resolved_tag,
        resolved_sha=dep_locked_chk.resolved_commit,
        matched_pattern="",
        resolved_at=dep_locked_chk.resolved_at or "",
    )


def with_preserved_install_hint(message: str, install_path: Path | None) -> str:
    """Explain that a failed replacement left the current install active."""
    if install_path is None or not install_path.exists():
        return message
    return (
        f"{message}. The existing installation remains active; fix the cause and retry the install."
    )
