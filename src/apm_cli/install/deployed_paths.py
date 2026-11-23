"""Lockfile path helpers for deployed install outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.security.gate import is_generated_python_artifact
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    resolve_for_containment,
)
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

    resolved: list[Path] = []

    def _resolved_candidate() -> Path:
        # Resolved lazily, once per call (external-root targets never need it).
        if not resolved:
            resolved.append(resolve_for_containment(target_path))
        return resolved[0]

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
                    _resolved_candidate(),
                    project_root=project_root,
                    target=_t,
                    scope=InstallScope.PROJECT,
                    candidate_resolved=True,
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


def skill_dir_entry(physical: Path, root: Path, target_name: str, targets: Any) -> tuple[str, ...]:
    """Return the skill directory entry a normal lock records beside a member (#3772).

    The receipt's own target is resolved at user scope even when this run did
    not select it (#4813); its directory is adopted only for a selected target
    that deploys skills to that same destination (Copilot and Codex share
    ``~/.agents/skills``), and is attributed to that selected target. An
    unselected target whose user root cannot resolve adopts nothing.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    origin = next((t for t in targets if t.name == target_name), None)
    if origin is None and (static := KNOWN_TARGETS.get(target_name)) is not None:
        origin = scoped_profile_or_none(static, user_scope=True)
    if origin is None or not origin.supports("skills"):
        return ()
    skills_root = origin.skills_deploy_path(root).resolve()
    try:
        parts = physical.relative_to(skills_root).parts
    except ValueError:
        return ()
    if len(parts) < 2:
        return ()
    for target in targets:
        destination = target.skills_deploy_path(root) if target.supports("skills") else None
        if destination is not None and destination.resolve() == skills_root:
            return (deployed_path_entry(destination / parts[0], root, [target]),)
    return ()


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


def _managed_skills_root(tp: Path, project_root: Path, targets: Any) -> str | None:
    """Name the real skills directory of the managed-root target that owns *tp*.

    Project-relative when it lies under *project_root*, absolute otherwise.
    """
    for target in targets:
        root = target.managed_deploy_root
        if root is None or not target.supports("skills") or not tp.is_relative_to(root):
            continue
        skills_root = target.skills_deploy_path(project_root)
        if skills_root.is_relative_to(project_root):
            return portable_relpath(skills_root, project_root)
        return skills_root.as_posix()
    return None


def skill_summary_paths(target_paths: Any, project_root: Path, targets: Any) -> list[str]:
    """Return the sorted skills roots (``<root>/<subdir>/``) a skill landed in.

    Each root is derived from the owning target's skills mapping so targets with
    a non-default subdir (e.g. ``agent-data/workflows``) render truthfully;
    managed-root deployments (e.g. a configured Cowork skills dir) name the
    owning target's real skills directory.
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
        managed = _managed_skills_root(tp, project_root, targets)
        if managed is not None:
            roots.add(managed)
            continue
        try:
            rel = tp.relative_to(project_root)
        except ValueError:
            locator_name = target_name_for_locator(deployed_path_entry(tp, project_root, targets))
            roots.add(f"{locator_name or 'external'}/skills")
            continue
        if not rel.parts:
            continue
        rel_posix = rel.as_posix()
        owner_root = next((r for r in static_roots if rel_posix.startswith(f"{r}/")), None)
        roots.add(owner_root or f"{rel.parts[0]}/skills")
    return [f"{root}/" for root in sorted(roots)]


def log_hook_display_payloads(
    payloads: list,
    verbose: bool,
    log_fn: Any,
    logger: Any,
) -> None:
    """Emit per-hook-file action summaries for hook transparency.

    Targets that render the same hook identically produce the same summary
    line; each distinct line is printed once per package (#4336).
    """
    _printed: set[str] = set()

    def _emit(line: str) -> None:
        if line not in _printed:
            _printed.add(line)
            log_fn(line)

    for _payload in payloads:
        _src = _payload.get("source_hook_file", "hook file")
        _actions = _payload.get("actions", [])
        if _actions:
            for _act in _actions:
                _emit(f"  |   {_act.get('event', '?')}: {_act.get('summary', '?')} ({_src})")
        else:
            _emit(f"  |   Hook file integrated: {_src}")
        if verbose and logger is not None:
            _out_path = _payload.get("output_path", "")
            logger.verbose_detail(f"  |   Hook JSON ({_src} -> {_out_path}):")
            for _jline in _payload.get("rendered_json", "").splitlines():
                logger.verbose_detail(f"  |     {_jline}")
