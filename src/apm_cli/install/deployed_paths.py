"""Lockfile path helpers for deployed install outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.security.gate import is_generated_python_artifact
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath


def format_target_collapse(paths: list[str], verbose: bool) -> tuple[str, list[str]]:
    """Name every destination folder: inline, or one line each when verbose (#4211)."""
    deduped = list(dict.fromkeys(paths))
    if verbose and len(deduped) >= 2:
        return "", [f"  |     -> {path}" for path in deduped]
    return ", ".join(deduped), []


def destination_tree_lines(phrase: str, paths: list[str], verbose: bool) -> list[str]:
    """Return the ``|-- <phrase> -> <folders>`` install tree lines for *paths*."""
    suffix, expansion = format_target_collapse(paths, verbose)
    if expansion:
        return [f"  |-- {phrase}:", *expansion]
    return [f"  |-- {phrase} -> {suffix}"]


def log_bundle_deployed(logger: Any, summary: str, deployed: list[str], targets: Any) -> None:
    """Report a local bundle install and name the folders it deployed into (#4192)."""
    logger.success(summary)
    if not deployed:
        return
    verbose = bool(getattr(logger, "verbose", False))
    phrase = f"{len(deployed)} file(s) deployed"
    for line in destination_tree_lines(phrase, destination_folders(deployed, targets), verbose):
        logger.tree_item(line)


def destination_folders(records: list[str], targets: Any) -> list[str]:
    """Return the primitive folders (``.github/agents/``) holding *records*, in order.

    A record outside every target primitive folder is named by its parent.
    """
    roots = [
        f"{mapping.deploy_root or target.root_dir}/{mapping.subdir}/".replace("//", "/")
        for target in targets
        for mapping in (target.primitives or {}).values()
    ]
    folders: list[str] = []
    for record in records:
        owner = max((root for root in roots if record.startswith(root)), key=len, default=None)
        folders.append(owner or f"{record.rsplit('/', 1)[0]}/")
    return list(dict.fromkeys(folders))


def deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the compatibility path view produced by the canonical codec."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.targets import encode_external_target_locator

    def _try_target(tgts) -> str | None:
        for _t in tgts:
            deploy_root = _t.managed_deploy_root
            if deploy_root is not None:
                try:
                    encoded = encode_external_target_locator(_t, target_path)
                except PathTraversalError:
                    raise
                except ValueError:
                    encoded = None
                if encoded is not None:
                    return encoded
            absolute_static_root = _t.resolved_deploy_root is None and deploy_root is not None
            if absolute_static_root:
                try:
                    target_path.relative_to(deploy_root)
                except ValueError:
                    pass
                else:
                    resolved_target = ensure_path_within(target_path, deploy_root)
                    return portable_relpath(resolved_target, project_root)
            try:
                locator = DeploymentLedgerCodec.locator_for_path(
                    target_path,
                    project_root=project_root,
                    target=_t,
                    scope=InstallScope.PROJECT,
                )
            except RuntimeError:
                continue
            return locator.value
        return None

    if targets:
        result = _try_target(targets)
        if result is not None:
            return result
    try:
        project_path = ensure_path_within(target_path, project_root)
        return portable_relpath(project_path, project_root)
    except (PathTraversalError, ValueError):
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
            relative = bundle_file.relative_to(skill_dir)
            if is_generated_python_artifact(relative):
                continue
            if bundle_file.is_file() and not bundle_file.is_symlink():
                entries.append(deployed_path_entry(bundle_file, project_root, targets))
        except OSError:
            continue
    return entries


def skill_summary_paths(target_paths: Any, project_root: Path, targets: Any) -> list[str]:
    """Return the sorted skills roots (``<root>/<subdir>/``) a skill landed in.

    Each root is derived from the owning target's skills mapping so targets with
    a non-default subdir (e.g. ``agent-data/workflows``) render truthfully;
    external (dynamic-root) deployments are labelled by their owning target.
    """
    from apm_cli.integration.targets import target_name_for_locator

    static_roots = sorted(
        {
            t.skills_rel_root
            for t in targets
            if t.supports("skills") and t.resolved_deploy_root is None
        }
    )
    roots: set[str] = set()
    for tp in target_paths:
        try:
            rel = tp.relative_to(project_root)
        except ValueError:
            owner = next(
                (
                    t
                    for t in targets
                    if t.managed_deploy_root is not None
                    and tp.is_relative_to(t.managed_deploy_root)
                ),
                None,
            )
            locator_name = target_name_for_locator(deployed_path_entry(tp, project_root, targets))
            label = owner.name if owner is not None else locator_name or "external"
            roots.add(f"{label}/skills")
            continue
        if not rel.parts:
            continue
        rel_posix = rel.as_posix()
        owner_root = next((r for r in static_roots if rel_posix.startswith(f"{r}/")), None)
        roots.add(owner_root or f"{rel.parts[0]}/skills")
    return [f"{root}/" for root in sorted(roots)]
