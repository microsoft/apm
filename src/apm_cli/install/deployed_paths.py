"""Lockfile path helpers for deployed install outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.utils.paths import portable_relpath


def deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the compatibility path view produced by the canonical codec."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.scope import InstallScope

    def _try_target(tgts) -> str | None:
        for _t in tgts:
            try:
                locator = DeploymentLedgerCodec.locator_for_path(
                    target_path,
                    project_root=project_root,
                    target=_t,
                    scope=InstallScope.PROJECT,
                )
            except RuntimeError:
                continue
            if locator.kind.value == "target-relative":
                if _t.name == "copilot-app":
                    from apm_cli.integration.copilot_app_db import to_lockfile_uri

                    return to_lockfile_uri(target_path.name)
                if _t.name == "copilot-cowork":
                    from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

                    return to_lockfile_path(target_path, _t.managed_deploy_root)
            return locator.value
        return None

    if targets:
        result = _try_target(targets)
        if result is not None:
            return result
    try:
        return portable_relpath(target_path, project_root)
    except ValueError:
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
