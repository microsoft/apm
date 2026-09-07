"""Shared helpers for APM package download and validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.apm_package import APMPackage
    from ..models.dependency.reference import DependencyReference
    from ..models.validation import ValidationResult


_MARKETPLACE_MANIFEST_HEADER = "# apm-marketplace-manifest-sha256: "


class MarketplaceManifestMaterializationError(ValueError):
    """Catalog metadata could not be materialized as a complete package."""


def has_marketplace_deployable_manifest(dep_ref: DependencyReference) -> bool:
    """Return whether catalog metadata declares an inline deployable surface."""
    manifest = getattr(dep_ref, "marketplace_manifest", None)
    return isinstance(manifest, dict) and any(
        manifest.get(field) for field in ("lspServers", "mcpServers")
    )


def _marketplace_display_name(dep_ref: DependencyReference) -> str:
    """Return a stable marketplace identity without checkout paths."""
    plugin_name = getattr(dep_ref, "marketplace_plugin_name", None)
    marketplace_name = getattr(dep_ref, "marketplace_name", None)
    if plugin_name and marketplace_name:
        return f"{plugin_name}@{marketplace_name}"
    return dep_ref.get_display_name()


def _manifest_digest(manifest: dict[str, object]) -> str:
    """Return a deterministic digest for consumer materialization state."""
    encoded = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_generated_marketplace_manifest(apm_yml_path: Path) -> bool:
    """Return whether a manifest carries APM's catalog-generation marker."""
    try:
        with apm_yml_path.open(encoding="utf-8") as manifest_file:
            first_line = manifest_file.readline().rstrip("\r\n")
    except OSError:
        return False
    return first_line.startswith(_MARKETPLACE_MANIFEST_HEADER)


def _declared_server_names(manifest: dict[str, object], field: str) -> set[str]:
    """Return validated inline server names from admitted catalog metadata."""
    raw_servers = manifest.get(field)
    if not raw_servers:
        return set()
    if not isinstance(raw_servers, dict):
        raise MarketplaceManifestMaterializationError(
            f"catalog field '{field}' must be an inline server mapping"
        )
    names = set(raw_servers)
    if any(not isinstance(name, str) or not name for name in names):
        raise MarketplaceManifestMaterializationError(
            f"catalog field '{field}' contains an invalid server name"
        )
    return names


def materialize_marketplace_manifest(dep_ref: DependencyReference, target_path: Path) -> bool:
    """Atomically synthesize a complete package from admitted catalog metadata."""
    manifest = getattr(dep_ref, "marketplace_manifest", None)
    if not has_marketplace_deployable_manifest(dep_ref) or not isinstance(manifest, dict):
        return False

    from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.utils.atomic_io import atomic_write_text
    from apm_cli.utils.file_ops import robust_rmtree
    from apm_cli.utils.yaml_io import load_yaml

    apm_yml_path = target_path / "apm.yml"
    apm_dir = target_path / ".apm"
    stage_path = target_path / ".apm-marketplace-stage.yml"
    try:
        declared_lsp = _declared_server_names(manifest, "lspServers")
        declared_mcp = _declared_server_names(manifest, "mcpServers")
        if not declared_lsp and not declared_mcp:
            return False
        if target_path.is_symlink() or apm_yml_path.is_symlink():
            raise MarketplaceManifestMaterializationError(
                "catalog-only package paths must not be symbolic links"
            )
        digest = _manifest_digest(manifest)
        existing_generated = False
        if apm_yml_path.exists():
            if not apm_yml_path.is_file():
                raise MarketplaceManifestMaterializationError(
                    "catalog-only package has a non-file apm.yml"
                )
            existing_generated = _is_generated_marketplace_manifest(apm_yml_path)
            if not existing_generated:
                return False

        if stage_path.exists() or stage_path.is_symlink():
            stage_path.unlink()
        staged_apm_yml = synthesize_apm_yml_from_plugin(
            target_path,
            dict(manifest),
            output_path=stage_path,
            map_artifacts=False,
            merge_existing=False,
            substitute_plugin_root=False,
            warn_on_invalid_servers=False,
        )
        staged_data = load_yaml(staged_apm_yml)
        if not isinstance(staged_data, dict):
            raise MarketplaceManifestMaterializationError(
                "catalog metadata produced a non-mapping manifest"
            )
        package = APMPackage.from_mapping(
            staged_data,
            package_path=target_path,
            manifest_path=staged_apm_yml,
        )
        actual_lsp = {dependency.name for dependency in package.get_lsp_dependencies()}
        actual_mcp = {dependency.name for dependency in package.get_mcp_dependencies()}
        if actual_lsp != declared_lsp:
            missing = ", ".join(sorted(declared_lsp - actual_lsp)) or "unknown"
            raise MarketplaceManifestMaterializationError(
                f"catalog LSP metadata did not materialize every declared server: {missing}"
            )
        if actual_mcp != declared_mcp:
            missing = ", ".join(sorted(declared_mcp - actual_mcp)) or "unknown"
            raise MarketplaceManifestMaterializationError(
                f"catalog MCP metadata did not materialize every declared server: {missing}"
            )
        if apm_dir.is_symlink() or (apm_dir.exists() and not apm_dir.is_dir()):
            raise MarketplaceManifestMaterializationError(
                "catalog-only package has an unsafe .apm path"
            )
        apm_dir.mkdir(exist_ok=True)
        staged_body = stage_path.read_text(encoding="utf-8")
        atomic_write_text(
            stage_path,
            f"{_MARKETPLACE_MANIFEST_HEADER}{digest}\n{staged_body}",
            new_file_mode=0o644,
        )
        if existing_generated and apm_yml_path.read_bytes() == stage_path.read_bytes():
            stage_path.unlink()
            return False
        os.replace(stage_path, apm_yml_path)
    except Exception as exc:
        stage_cleanup_error: OSError | None = None
        if stage_path.exists() or stage_path.is_symlink():
            try:
                stage_path.unlink()
            except OSError as cleanup_exc:
                stage_cleanup_error = cleanup_exc
        if target_path.exists():
            try:
                robust_rmtree(target_path)
            except OSError as cleanup_exc:
                cleanup_detail = str(cleanup_exc)
                if stage_cleanup_error is not None:
                    cleanup_detail += f"; staging cleanup failed: {stage_cleanup_error}"
                raise MarketplaceManifestMaterializationError(
                    f"invalid marketplace metadata for '{_marketplace_display_name(dep_ref)}'; "
                    f"rejected download cleanup also failed: {cleanup_detail}"
                ) from exc
        if isinstance(exc, MarketplaceManifestMaterializationError):
            raise MarketplaceManifestMaterializationError(
                f"invalid marketplace metadata for '{_marketplace_display_name(dep_ref)}': {exc}"
            ) from exc
        raise MarketplaceManifestMaterializationError(
            f"invalid marketplace metadata for '{_marketplace_display_name(dep_ref)}': {exc}"
        ) from exc
    return True


def _validate_and_load_package(
    validation_result: ValidationResult,
    target_path: Path,
    dep_ref: DependencyReference,
) -> APMPackage:
    """Check *validation_result*, clean up *target_path* on failure, and return the package.

    Args:
        validation_result: Result from ``validate_apm_package(target_path)``.
        target_path: Destination directory; removed on validation failure.
        dep_ref: Dependency reference (for error messages and ``source`` assignment).

    Returns:
        The :class:`~apm_cli.models.apm_package.APMPackage` from the validation
        result (with ``source`` already set to ``dep_ref.to_github_url()``).

    Raises:
        RuntimeError: If the package is invalid or metadata is missing.
        AgentPluginError: If *target_path* is a rejected Agent Plugin
            (unsupported or foreign schema); *target_path* is cleaned up
            before this propagates.
    """
    from ..agent_plugins.errors import AgentPluginError
    from ..bundle.local_bundle import route_agent_plugin_package
    from ..models.validation import validate_apm_package
    from ..utils.file_ops import robust_rmtree

    if materialize_marketplace_manifest(dep_ref, target_path):
        validation_result = validate_apm_package(target_path)

    if target_path.is_dir():
        try:
            route_agent_plugin_package(target_path)
        except AgentPluginError:
            if target_path.exists():
                robust_rmtree(target_path, ignore_errors=True)
            raise
    if not validation_result.is_valid:
        if target_path.exists():
            robust_rmtree(target_path, ignore_errors=True)
        error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
        for error in validation_result.errors:
            error_msg += f"  - {error}\n"
        raise RuntimeError(error_msg.strip())

    if not validation_result.package:
        if target_path.exists():
            robust_rmtree(target_path, ignore_errors=True)
        raise RuntimeError(
            f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}"
        )

    package = validation_result.package
    package.source = dep_ref.to_github_url()
    return package
