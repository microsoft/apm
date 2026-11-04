"""Shared helpers for install dependency sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.deps.dependency_graph import DependencyNode
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import APMPackage, PackageType


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
            "Restore valid dependency metadata and rerun 'apm install'."
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
