"""Lockfile path helpers for deployed install outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath


def deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the lockfile-safe path string for a deployed file."""

    def _try_dynamic_root(tgts, *, strict: bool = False) -> str | None:
        for _t in tgts:
            deploy_root = _t.managed_deploy_root
            absolute_static_root = _t.resolved_deploy_root is None and deploy_root is not None
            if deploy_root is None:
                continue
            if not strict or absolute_static_root:
                try:
                    target_path.relative_to(deploy_root)
                except ValueError:
                    continue
            if absolute_static_root:
                resolved_target = ensure_path_within(target_path, deploy_root)
                return portable_relpath(resolved_target, project_root)
            if _t.name == "copilot-app":
                from apm_cli.integration.copilot_app_db import to_lockfile_uri

                return to_lockfile_uri(target_path.name)
            from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

            return to_lockfile_path(target_path, deploy_root)
        return None

    if targets:
        result = _try_dynamic_root(targets)
        if result is not None:
            return result
    try:
        return target_path.relative_to(project_root).as_posix()
    except ValueError:
        if targets:
            result = _try_dynamic_root(targets, strict=True)
            if result is not None:
                return result
        raise RuntimeError(  # noqa: B904
            f"Cannot translate {target_path!r} to a lockfile path: "
            f"path is outside the project tree and no dynamic-root "
            f"target matched. This is a bug -- please report it."
        )


def skill_bundle_file_entries(
    skill_dir: Path,
    project_root: Path,
    targets: Any,
) -> list[str]:
    """Expand a deployed skill directory into per-file lockfile entries."""
    try:
        if not (skill_dir.is_dir() and not skill_dir.is_symlink()):
            return []
    except OSError:
        return []
    entries: list[str] = []
    for bundle_file in sorted(skill_dir.rglob("*")):
        try:
            if bundle_file.is_file() and not bundle_file.is_symlink():
                entries.append(deployed_path_entry(bundle_file, project_root, targets))
        except OSError:
            continue
    return entries
