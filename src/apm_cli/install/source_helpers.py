"""Shared helpers for install dependency sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.remediation import ACCEPT_NEW_CONTENT, CLEAN_DOWNLOAD_CACHE, RERUN_AFTER_FIX

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
    from apm_cli.deps.lockfile import get_lockfile_path

    global_flag = " -g" if ctx.scope is InstallScope.USER else ""
    apm_dir = getattr(ctx, "apm_dir", None)
    lockfile = get_lockfile_path(apm_dir) if apm_dir is not None else "apm.lock.yaml"
    return (
        f"This may indicate a supply-chain attack. {CLEAN_DOWNLOAD_CACHE.sentence()} "
        + ACCEPT_NEW_CONTENT.bind(
            global_flag=global_flag, package=dep_key, lockfile=lockfile
        ).sentence()
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
    manifest = install_path / "apm.yml"
    if not manifest.exists():
        return package

    import yaml

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
            + RERUN_AFTER_FIX.bind(fix="restore valid dependency metadata").sentence()
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
