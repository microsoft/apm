"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import builtins
import logging
from dataclasses import dataclass
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CollectOpts:
    """Shared options for transitive MCP collection helpers."""

    trust_private: bool
    logger: object
    diagnostics: object | None


def _load_locked_package_paths(
    apm_modules_dir: Path,
    lock_path: Path | None,
) -> tuple[list[Path] | None, set[Path]]:
    """Return lock-derived apm.yml paths and the direct-dependency subset."""
    if not lock_path or not lock_path.exists():
        return None, set()
    lockfile = LockFile.read(lock_path)
    if lockfile is None:
        return None, set()

    locked_paths: set[Path] = set()
    direct_paths: set[Path] = set()
    for dep in lockfile.get_package_dependencies():
        if not dep.repo_url:
            continue
        yml_path = (
            apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml"
            if dep.virtual_path
            else apm_modules_dir / dep.repo_url / "apm.yml"
        )
        resolved_path = yml_path.resolve()
        locked_paths.add(resolved_path)
        if dep.depth == 1:
            direct_paths.add(resolved_path)
    return [path for path in sorted(locked_paths) if path.exists()], direct_paths


def _handle_self_defined_dep(dep, *, pkg_name: str, is_direct: bool, opts: _CollectOpts) -> bool:
    """Return True when a self-defined MCP dep should be kept."""
    if not (hasattr(dep, "is_self_defined") and dep.is_self_defined):
        return True
    if is_direct:
        opts.logger.progress(f"Trusting direct dependency MCP '{dep.name}' from '{pkg_name}'")
        return True
    if opts.trust_private:
        opts.logger.progress(
            f"Trusting self-defined MCP server '{dep.name}' from transitive package '{pkg_name}' "
            "(--trust-transitive-mcp)"
        )
        return True

    trust_message = (
        f"Transitive package '{pkg_name}' declares self-defined MCP server '{dep.name}' "
        "(registry: false). Re-declare it in your apm.yml or use --trust-transitive-mcp."
    )
    if opts.diagnostics:
        opts.diagnostics.warn(trust_message)
    else:
        opts.logger.warning(trust_message)
    return False


def _collect_package_dependencies(
    apm_yml_path: Path,
    *,
    direct_paths: set[Path],
    opts: _CollectOpts,
) -> list:
    """Collect MCP dependencies from one package file."""
    from apm_cli.models.apm_package import APMPackage

    pkg = APMPackage.from_apm_yml(apm_yml_path)
    mcp_dependencies = pkg.get_mcp_dependencies()
    if not mcp_dependencies:
        return []

    is_direct = apm_yml_path.resolve() in direct_paths
    collected = []
    for dep in mcp_dependencies:
        if _handle_self_defined_dep(
            dep,
            pkg_name=pkg.name,
            is_direct=is_direct,
            opts=opts,
        ):
            collected.append(dep)
    return collected


def collect_transitive(
    apm_modules_dir: Path,
    lock_path: Path | None = None,
    trust_private: bool = False,
    logger=None,
    diagnostics=None,
) -> list:
    """Collect MCP dependencies from resolved APM packages listed in apm.lock.

    Only scans apm.yml files for packages present in apm.lock to avoid
    picking up stale/orphaned packages from previous installs.
    Falls back to scanning all apm.yml files if no lock file is available.

    Self-defined servers (registry: false) from direct dependencies
    (depth == 1) are auto-trusted.  Self-defined servers from transitive
    dependencies (depth > 1) are skipped with a warning unless
    *trust_private* is True.
    """
    if logger is None:
        logger = NullCommandLogger()
    if not apm_modules_dir.exists():
        return []

    locked_paths, direct_paths = _load_locked_package_paths(apm_modules_dir, lock_path)
    apm_yml_paths = locked_paths if locked_paths is not None else apm_modules_dir.rglob("apm.yml")
    opts = _CollectOpts(trust_private=trust_private, logger=logger, diagnostics=diagnostics)

    collected = []
    for apm_yml_path in apm_yml_paths:
        try:
            collected.extend(
                _collect_package_dependencies(
                    apm_yml_path,
                    direct_paths=direct_paths,
                    opts=opts,
                )
            )
        except Exception:
            _log.debug(
                "Skipping package at %s: failed to parse apm.yml",
                apm_yml_path,
                exc_info=True,
            )
            continue
    return collected


def deduplicate(deps: list) -> list:
    """Deduplicate MCP dependencies by name; first occurrence wins.

    Root deps are listed before transitive, so root overlays take
    precedence.
    """
    seen_names: builtins.set = builtins.set()
    result = []
    for dep in deps:
        if hasattr(dep, "name"):
            name = dep.name
        elif isinstance(dep, dict):
            name = dep.get("name", "")
        else:
            name = str(dep)
        if not name:
            if dep not in result:
                result.append(dep)
            continue
        if name not in seen_names:
            seen_names.add(name)
            result.append(dep)
    return result
