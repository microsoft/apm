"""Shared helpers for MCP and LSP integrators.

Extracted to satisfy the R0801 (duplicate-code) lint gate.
"""

from __future__ import annotations

import builtins
from pathlib import Path

from apm_cli.deps.lockfile import LockFile
from apm_cli.runtime.utils import find_runtime_binary


def _hermes_runtime_opted_in() -> bool:
    """Return ``True`` when Hermes MCP writes are opted into.

    Gate: the ``hermes`` experimental flag is enabled AND Hermes is actually
    present on the host (its home dir exists, or the ``hermes`` binary is on
    PATH).  Prevents surprise writes to ``~/.hermes/`` on hosts where Hermes
    was never installed.  Any import/path error is treated as "not opted in".
    """
    try:
        from apm_cli.core.experimental import is_enabled
        from apm_cli.integration.targets import resolve_hermes_root

        if not is_enabled("hermes"):
            return False
        return resolve_hermes_root().is_dir() or find_runtime_binary("hermes") is not None
    except (ImportError, ValueError):
        return False


def _runtime_opted_in(
    runtime_name: str,
    project_root_path: Path,
    is_vscode_available,
    manager,
    dir_gated: dict[str, str],
    *,
    user_scope: bool = False,
) -> bool:
    """Decide whether a single runtime should be targeted for this project.

    Opt-in runtimes are gated on a project marker directory (or, for Claude,
    a binary on PATH) so a host-wide install does not silently opt every
    project into MCP writes.  Plain runtimes fall back to availability probing.
    """
    if runtime_name == "vscode":
        return bool(is_vscode_available(project_root=project_root_path))
    if runtime_name == "kiro" and user_scope:
        return True
    if runtime_name in dir_gated:
        return (project_root_path / dir_gated[runtime_name]).is_dir()
    if runtime_name == "claude":
        return (project_root_path / ".claude").is_dir() or (
            find_runtime_binary("claude") is not None
        )
    if runtime_name == "intellij":
        from apm_cli.adapters.client.intellij import _intellij_config_dir

        return _intellij_config_dir().is_dir()
    if runtime_name == "hermes":
        return _hermes_runtime_opted_in()
    return bool(manager.is_runtime_available(runtime_name))


def deduplicate_deps(deps: list) -> list:
    """Deduplicate dependency entries by name; first occurrence wins.

    Root deps are listed before transitive, so root overlays take
    precedence.  Works with any object that has a ``name`` attribute,
    plain dicts with a ``"name"`` key, or bare strings.
    """
    seen_names: builtins.set = builtins.set()
    result: list = []
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


def resolve_locked_apm_yml_paths(
    apm_modules_dir: Path,
    lock_path: Path | None,
) -> tuple[list[Path] | None, builtins.set]:
    """Resolve apm.yml paths from the lockfile.

    Returns ``(locked_paths_or_None, direct_paths_set)``.
    When *locked_paths* is ``None`` the caller should fall back to rglob.
    """
    locked_paths: builtins.set | None = None
    direct_paths: builtins.set = builtins.set()

    if lock_path and lock_path.exists():
        lockfile = LockFile.read(lock_path)
        if lockfile is not None:
            locked_paths = builtins.set()
            for dep in lockfile.get_package_dependencies():
                if dep.repo_url:
                    yml = (
                        apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml"
                        if dep.virtual_path
                        else apm_modules_dir / dep.repo_url / "apm.yml"
                    )
                    locked_paths.add(yml.resolve())
                    if dep.depth == 1:
                        direct_paths.add(yml.resolve())

    if locked_paths is not None:
        resolved = [path for path in sorted(locked_paths) if path.exists()]
        return resolved, direct_paths

    return None, direct_paths
