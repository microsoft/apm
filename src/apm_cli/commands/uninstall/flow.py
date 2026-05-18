"""Helper functions for the uninstall CLI flow."""

from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass

from ...models.apm_package import APMPackage


@dataclass(frozen=True, slots=True)
class _ManifestUpdateContext:
    """Persistence dependencies for manifest updates."""

    apm_yml_path: object
    dump_yaml: object
    logger: object


@dataclass(frozen=True, slots=True)
class _McpCleanupContext:
    """Inputs required to reconcile MCP state after uninstall."""

    lockfile: object
    lockfile_path: object
    pre_uninstall_mcp_servers: object
    modules_dir: object
    deploy_root: object
    scope: object
    logger: object


def _load_manifest_data(apm_yml_path, logger):
    """Load uninstall manifest data and ensure the dependency list exists."""
    from ...utils.yaml_io import dump_yaml, load_yaml

    try:
        data = load_yaml(apm_yml_path) or {}
    except Exception as exc:
        logger.error(f"Failed to read {apm_yml_path}: {exc}")
        sys.exit(1)

    if "dependencies" not in data:
        data["dependencies"] = {}
    if "apm" not in data["dependencies"]:
        data["dependencies"]["apm"] = []
    return data, dump_yaml


def _update_manifest_dependencies(
    data, current_deps, packages_to_remove, ctx: _ManifestUpdateContext
):
    """Remove packages from the manifest and persist the updated YAML."""
    for package in packages_to_remove:
        current_deps.remove(package)
        ctx.logger.progress(f"Removed {package} from apm.yml")
    data["dependencies"]["apm"] = current_deps
    try:
        ctx.dump_yaml(data, ctx.apm_yml_path)
        ctx.logger.success(
            f"Updated {ctx.apm_yml_path} (removed {len(packages_to_remove)} package(s))"
        )
    except Exception as exc:
        ctx.logger.error(f"Failed to write {ctx.apm_yml_path}: {exc}")
        sys.exit(1)


def _load_lockfile_state(apm_dir):
    """Load the lockfile and capture the current MCP server set."""
    from ...deps.lockfile import LockFile, get_lockfile_path

    lockfile_path = get_lockfile_path(apm_dir)
    lockfile = LockFile.read(lockfile_path)
    pre_uninstall_mcp_servers = builtins.set(lockfile.mcp_servers) if lockfile else builtins.set()
    return lockfile_path, lockfile, pre_uninstall_mcp_servers


def _collect_removed_keys(packages_to_remove, actual_orphans):
    """Build the unique dependency keys removed by this uninstall run."""
    from .engine import _parse_dependency_entry

    removed_keys = builtins.set()
    for package in packages_to_remove:
        try:
            removed_keys.add(_parse_dependency_entry(package).get_unique_key())
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_keys.add(package)
    removed_keys.update(actual_orphans)
    return removed_keys


def _collect_deployed_files(lockfile, packages_to_remove, actual_orphans):
    """Collect the deployed files for removed packages before mutating the lockfile."""
    from ...integration.base_integrator import BaseIntegrator

    removed_keys = _collect_removed_keys(packages_to_remove, actual_orphans)
    all_deployed_files = builtins.set()
    if lockfile:
        for dep_key, dep in lockfile.dependencies.items():
            if dep_key in removed_keys:
                all_deployed_files.update(dep.deployed_files)
    return BaseIntegrator.normalize_managed_files(all_deployed_files) or builtins.set()


def _update_lockfile_after_uninstall(
    lockfile, lockfile_path, packages_to_remove, actual_orphans, logger
):
    """Remove deleted packages from the lockfile and persist the result."""
    if not lockfile:
        return

    from .engine import _parse_dependency_entry

    lockfile_updated = False
    for package in packages_to_remove:
        try:
            key = _parse_dependency_entry(package).get_unique_key()
        except (ValueError, TypeError, AttributeError, KeyError):
            key = package
        if key not in lockfile.dependencies:
            continue
        del lockfile.dependencies[key]
        lockfile_updated = True

    for orphan_key in actual_orphans:
        if orphan_key not in lockfile.dependencies:
            continue
        del lockfile.dependencies[orphan_key]
        lockfile_updated = True

    if not lockfile_updated:
        return

    try:
        if lockfile.dependencies:
            lockfile.write(lockfile_path)
        else:
            lockfile_path.unlink(missing_ok=True)
    except Exception:
        logger.warning(
            "Failed to update lockfile -- it may be out of sync with uninstalled packages."
        )


def _sync_integrations(manifest_path, deploy_root, all_deployed_files, logger, user_scope: bool):
    """Run best-effort integration cleanup after uninstall."""
    from .engine import _sync_integrations_after_uninstall

    cleaned = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "commands": 0,
        "hooks": 0,
        "instructions": 0,
    }
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        cleaned = _sync_integrations_after_uninstall(
            apm_package,
            deploy_root,
            all_deployed_files,
            logger,
            user_scope=user_scope,
        )
    except Exception:
        pass
    return cleaned


def _log_cleanup_counts(cleaned, logger):
    """Log non-zero integration cleanup counts."""
    for label, count in cleaned.items():
        if count <= 0:
            continue
        logger.progress(f"Cleaned up {count} integrated {label}", symbol="check")
        logger.verbose_detail(f"    Removed {count} deployed {label} file(s)")


def _cleanup_mcp_state(manifest_path, ctx: _McpCleanupContext):
    """Run best-effort MCP cleanup after uninstall."""
    from .engine import _cleanup_stale_mcp
    from .engine import _McpCleanupContext as _EngineMcpCleanupContext

    try:
        from ...core.scope import InstallScope

        apm_package = APMPackage.from_apm_yml(manifest_path)
        _cleanup_stale_mcp(
            _EngineMcpCleanupContext(
                apm_package=apm_package,
                lockfile=ctx.lockfile,
                lockfile_path=ctx.lockfile_path,
                old_mcp_servers=ctx.pre_uninstall_mcp_servers,
                modules_dir=ctx.modules_dir,
                project_root=ctx.deploy_root,
                user_scope=ctx.scope is InstallScope.USER,
                scope=ctx.scope,
            )
        )
    except Exception:
        ctx.logger.warning("MCP cleanup during uninstall failed")


def _summarise_uninstall(packages_to_remove, removed_from_modules, packages_not_found, logger):
    """Render the final uninstall summary and warnings."""
    summary_lines = [f"Removed {len(packages_to_remove)} package(s) from apm.yml"]
    if removed_from_modules > 0:
        summary_lines.append(f"Removed {removed_from_modules} package(s) from apm_modules/")
    logger.success("Uninstall complete: " + ", ".join(summary_lines))
    if packages_not_found:
        logger.warning(f"Note: {len(packages_not_found)} package(s) were not found in apm.yml")
