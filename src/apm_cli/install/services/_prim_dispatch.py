"""Dispatch pipeline helpers for :mod:`primitives`.

Extracted from :mod:`primitives` to keep that module under 400 lines.
All public names continue to be importable from
:mod:`apm_cli.install.services.primitives` via explicit re-exports.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.integration._opts import IntegrateOpts

from .deployed_path import _deployed_path_entry

# Shadow builtins defensively: see ``__init__`` module-level comment.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _format_target_collapse(paths: list[str], verbose: bool) -> tuple[str, list[str]]:
    """Apply the 1/2/3+ multi-target collapse rule.

    Returns a tuple ``(suffix, expansion_lines)``:

    * ``suffix`` -- text appended after ``-> `` on the aggregate line.
    * ``expansion_lines`` -- extra ``  |     -> <path>`` lines emitted
      AFTER the aggregate line when ``verbose`` is True; empty otherwise.

    Rule:
      1 target  -> ``<path1>``
      2 targets -> ``<path1>, <path2>``
      3+        -> ``N targets`` (verbose forces full enumeration)
    """
    deduped: list[str] = []
    seen: set = builtins.set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    if verbose and len(deduped) >= 2:
        return "", [f"  |     -> {p}" for p in deduped]
    if len(deduped) == 0:
        return "", []
    if len(deduped) == 1:
        return deduped[0], []
    if len(deduped) == 2:
        return f"{deduped[0]}, {deduped[1]}", []
    return f"{len(deduped)} targets", []


@dataclass(frozen=True, slots=True)
class _WarnCtx:
    """Bundled warn-context arguments for :func:`_maybe_emit_cowork_warning`."""

    ctx: Any
    logger: Any
    diagnostics: Any


def _maybe_emit_cowork_warning(
    package_info: Any,
    package_name: str,
    targets: Any,
    warn_ctx: _WarnCtx,
) -> None:
    """Emit the cowork non-skill primitive warning once per run (Amendment 6).

    No-ops when the cowork target is not active, *ctx* is absent, or
    the warning has already been emitted for this install session.
    """
    _cowork_active = any(t.name == "copilot-cowork" for t in targets)
    ctx = warn_ctx.ctx
    logger = warn_ctx.logger
    diagnostics = warn_ctx.diagnostics
    if not (_cowork_active and ctx is not None and not ctx.cowork_nonsupported_warned):
        return

    _apm_dir = Path(package_info.install_path) / ".apm"
    _NON_SKILL_DIRS = {
        "agents": "agents",
        "prompts": "prompts",
        "instructions": "instructions",
        "hooks": "hooks",
    }
    _found_types = [
        ptype
        for ptype, subdir in _NON_SKILL_DIRS.items()
        if (_apm_dir / subdir).is_dir() and any((_apm_dir / subdir).iterdir())
    ]
    if not _found_types:
        return

    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    _types_str = ", ".join(sorted(builtins.set(_found_types)))
    _warn_msg = (
        f"copilot-cowork target only supports skills; "
        f"non-skill primitives in {_pkg_label} "
        f"({_types_str}) will not deploy to cowork"
    )
    if logger:
        logger.warning(_warn_msg, symbol="warning")
    diagnostics.warn(_warn_msg)
    ctx.cowork_nonsupported_warned = True


@dataclass
class _DispatchCtx:
    """Bundled arguments for :func:`_dispatch_non_skill_primitives`."""

    dispatch: Any
    integrator_kwargs: dict
    targets: Any
    package_info: Any
    project_root: Path
    force: bool
    managed_files: Any
    diagnostics: Any
    deployed: list
    result: dict
    log_fn: Any
    verbose: bool


@dataclass
class _SkillLogCtx:
    """Bundled arguments for :func:`_collect_and_log_skills`."""

    skill_integrator: Any
    package_info: Any
    project_root: Path
    diagnostics: Any
    managed_files: Any
    force: bool
    targets: Any
    skill_subset: Any
    result: dict
    deployed: list
    log_fn: Any
    verbose: bool


def _compute_prim_label(prim_name: str, mapping, target, deploy_dir: str) -> tuple[str, str]:
    """Return ``(label, deploy_dir)`` for a single primitive/target combination.

    May update *deploy_dir* for hooks targets that expose a config-display path.
    """
    if prim_name == "instructions" and mapping.format_id in ("cursor_rules", "claude_rules"):
        return "rule(s)", deploy_dir
    elif prim_name == "instructions":
        return "instruction(s)", deploy_dir
    elif prim_name == "hooks":
        if target.hooks_config_display:
            deploy_dir = target.hooks_config_display
        return "hook(s)", deploy_dir
    else:
        return prim_name, deploy_dir


def _emit_prim_aggregates(per_kind: dict, dispatch, log_fn, verbose: bool) -> None:
    """Emit aggregated per-kind integration lines in dispatch order."""
    for _prim_name in dispatch:
        if _prim_name not in per_kind:
            continue
        _info = per_kind[_prim_name]
        _suffix, _expansion = _format_target_collapse(_info["paths"], verbose)
        _files = _info["files"]
        _adopted = _info["adopted"]
        if _files > 0:
            _verb_phrase = f"{_files} {_info['label']} integrated"
            if _adopted > 0:
                _verb_phrase = f"{_verb_phrase} ({_adopted} adopted)"
        else:
            _verb_phrase = f"{_adopted} {_info['label']} adopted"
        if _expansion:
            log_fn(f"  |-- {_verb_phrase}:")
            for line in _expansion:
                log_fn(line)
        else:
            log_fn(f"  |-- {_verb_phrase} -> {_suffix}")


def _dispatch_non_skill_primitives(ctx: _DispatchCtx) -> None:
    """Dispatch all non-skill primitives across targets; emit aggregate log lines.

    Mutates *result* counters and *deployed* paths in-place.
    Skills are skipped here (``entry.multi_target`` flag) and handled by
    ``_collect_and_log_skills``.
    """
    dispatch = ctx.dispatch
    integrator_kwargs = ctx.integrator_kwargs
    targets = ctx.targets
    package_info = ctx.package_info
    project_root = ctx.project_root
    force = ctx.force
    managed_files = ctx.managed_files
    diagnostics = ctx.diagnostics
    deployed = ctx.deployed
    result = ctx.result
    log_fn = ctx.log_fn
    verbose = ctx.verbose
    _per_kind: dict[str, dict[str, Any]] = {}

    for _prim_name, _entry in dispatch.items():
        if _entry.multi_target:
            continue  # skills handled separately

        _integrator = integrator_kwargs[_prim_name]
        _agg_files = 0
        _agg_adopted = 0
        _agg_paths: list[str] = []
        _label = _prim_name

        for _target in targets:
            _mapping = _target.primitives.get(_prim_name)
            if _mapping is None:
                continue
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target,
                package_info,
                project_root,
                IntegrateOpts(
                    force=force,
                    managed_files=managed_files,
                    diagnostics=diagnostics,
                ),
            )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(_deployed_path_entry(tp, project_root, targets))

            _adopted_attr = getattr(_int_result, "files_adopted", 0)
            # Coerce defensively: MagicMock auto-attributes may not be ints.
            _adopted = _adopted_attr if isinstance(_adopted_attr, int) else 0

            if _int_result.files_integrated <= 0 and _adopted <= 0:
                continue

            _agg_files += _int_result.files_integrated
            _agg_adopted += _adopted
            result[_entry.counter_key] += _int_result.files_integrated
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = (
                f"{_effective_root}/{_mapping.subdir}/"
                if _mapping.subdir
                else f"{_effective_root}/"
            )
            _label, _deploy_dir = _compute_prim_label(_prim_name, _mapping, _target, _deploy_dir)
            _agg_paths.append(_deploy_dir)

        if _agg_files > 0 or _agg_adopted > 0:
            _per_kind[_prim_name] = {
                "files": _agg_files,
                "adopted": _agg_adopted,
                "label": _label,
                "paths": _agg_paths,
            }

    # Emit aggregated per-kind lines in dispatch order so output is stable.
    _emit_prim_aggregates(_per_kind, dispatch, log_fn, verbose)


def _emit_skill_log_lines(header_line: str, expansion: list, single_line: str, log_fn) -> None:
    """Emit either a multi-line expansion block or a single-line suffix entry."""
    if expansion:
        log_fn(header_line)
        for line in expansion:
            log_fn(line)
    else:
        log_fn(single_line)


def _collect_and_log_skills(ctx: _SkillLogCtx) -> None:
    """Run skill integration, update result/deployed, and emit log lines.

    Mutates *result* and *deployed* in-place.
    """
    skill_integrator = ctx.skill_integrator
    package_info = ctx.package_info
    project_root = ctx.project_root
    diagnostics = ctx.diagnostics
    managed_files = ctx.managed_files
    force = ctx.force
    targets = ctx.targets
    skill_subset = ctx.skill_subset
    result = ctx.result
    deployed = ctx.deployed
    log_fn = ctx.log_fn
    verbose = ctx.verbose
    skill_result = skill_integrator.integrate_package_skill(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        targets=targets,
        skill_subset=skill_subset,
    )
    _skill_target_dirs: set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            # Dynamic-root target (copilot-cowork) -- path outside project tree.
            _skill_target_dirs.add("copilot-cowork")

    _skill_target_paths = [f"{d}/skills/" for d in sorted(_skill_target_dirs)]
    if not _skill_target_paths:
        _skill_target_paths = ["skills/"]
    _skill_suffix, _skill_expansion = _format_target_collapse(_skill_target_paths, verbose)

    if skill_result.skill_created:
        result["skills"] += 1
        _emit_skill_log_lines(
            "  |-- Skill integrated:",
            _skill_expansion,
            f"  |-- Skill integrated -> {_skill_suffix}",
            log_fn,
        )

    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        _emit_skill_log_lines(
            f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated:",
            _skill_expansion,
            f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_suffix}",
            log_fn,
        )

    for tp in skill_result.target_paths:
        deployed.append(_deployed_path_entry(tp, project_root, targets))
