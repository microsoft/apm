"""Private helpers extracted from services.py to keep each function under threshold.

All symbols here are module-private (single underscore prefix) and are only
called from ``apm_cli.install.services``.  They are NOT part of the public
API and MUST NOT be imported from outside this package.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..utils.diagnostics import DiagnosticCollector


# Shadow builtins shadowed at the top of services.py for the same reason.
set = builtins.set
list = builtins.list
dict = builtins.dict


# ---------------------------------------------------------------------------
# _format_target_collapse
# ---------------------------------------------------------------------------


def _format_target_collapse(paths: list[str], verbose: bool) -> tuple[str, list[str]]:
    """Apply the 1/2/3+ multi-target collapse rule.

    Returns a tuple ``(suffix, expansion_lines)``:

    * ``suffix`` -- the text appended after ``-> `` on the aggregate line.
    * ``expansion_lines`` -- extra ``  |     -> <path>`` lines emitted
      AFTER the aggregate line when ``verbose`` is True. Empty list when
      collapsed.

    The rule:
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


# ---------------------------------------------------------------------------
# _warn_cowork_nonsupported
# ---------------------------------------------------------------------------


def _warn_cowork_nonsupported(
    targets: Any,
    ctx: Any,
    package_info: Any,
    package_name: str,
    logger: InstallLogger | None,
    diagnostics: DiagnosticCollector,
) -> None:
    """Emit the Amendment-6 cowork non-skill primitive warning (once per run).

    Checks whether the copilot-cowork target is active and whether the package
    contains any non-skill primitives.  When both conditions hold the warning
    is logged via *logger* and recorded in *diagnostics*, then the
    ``ctx.cowork_nonsupported_warned`` flag is set to prevent duplicate lines.
    """
    import builtins as _builtins

    _cowork_active = any(t.name == "copilot-cowork" for t in targets)
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
    _types_str = ", ".join(sorted(_builtins.set(_found_types)))
    _warn_msg = (
        f"copilot-cowork target only supports skills; "
        f"non-skill primitives in {_pkg_label} "
        f"({_types_str}) will not deploy to cowork"
    )
    if logger:
        logger.warning(_warn_msg, symbol="warning")
    diagnostics.warn(_warn_msg)
    ctx.cowork_nonsupported_warned = True


# ---------------------------------------------------------------------------
# _log_hook_display_payloads
# ---------------------------------------------------------------------------


def _log_hook_display_payloads(
    payloads: list,
    verbose: bool,
    log_fn: Any,
    logger: Any,
) -> None:
    """Emit per-hook-file action summaries for the hook transparency feature.

    Uses post-path-rewrite data from display_payloads, so the output
    faithfully reflects what was written to disk and will be executed.
    """
    for _payload in payloads:
        _src = _payload.get("source_hook_file", "hook file")
        _actions = _payload.get("actions", [])
        if _actions:
            for _act in _actions:
                log_fn(f"  |   {_act.get('event', '?')}: {_act.get('summary', '?')} ({_src})")
        else:
            log_fn(f"  |   Hook file integrated: {_src}")
        if verbose and logger is not None:
            _out_path = _payload.get("output_path", "")
            logger.verbose_detail(f"  |   Hook JSON ({_src} -> {_out_path}):")
            for _jline in _payload.get("rendered_json", "").splitlines():
                logger.verbose_detail(f"  |     {_jline}")


# ---------------------------------------------------------------------------
# _log_per_kind_results
# ---------------------------------------------------------------------------


def _log_per_kind_results(
    per_kind: dict[str, dict[str, Any]],
    dispatch: dict,
    verbose: bool,
    logger: InstallLogger | None,
    *,
    log_integration: Any | None = None,
) -> None:
    """Emit one aggregated log line per primitive kind in dispatch order.

    ``per_kind`` maps primitive name to a sub-dict with keys
    ``files``, ``adopted``, ``label``, and ``paths``.  Kinds absent from
    ``per_kind`` are silently skipped.
    """
    log_fn = log_integration
    if log_fn is None and logger is not None:
        log_fn = logger.tree_item

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
        if log_fn is None:
            continue
        if _expansion:
            log_fn(f"  |-- {_verb_phrase}:")
            for line in _expansion:
                log_fn(line)
        else:
            log_fn(f"  |-- {_verb_phrase} -> {_suffix}")
        if _prim_name == "hooks" and _files > 0:
            _hook_verbose = verbose or bool(getattr(logger, "verbose", False))
            _log_hook_display_payloads(
                _info.get("hook_payloads", []),
                _hook_verbose,
                log_fn,
                logger,
            )
        from apm_cli.install._services_helpers import _emit_integration_hints

        _emit_integration_hints(_prim_name, _info, log_fn)


# ---------------------------------------------------------------------------
# _log_skill_result
# ---------------------------------------------------------------------------


def _log_skill_result(
    skill_result: Any,
    result: dict,
    project_root: Path,
    targets: Any,
    verbose: bool,
    logger: InstallLogger | None,
    *,
    package_name: str = "",
    package_info: Any = None,
    log_integration: Any | None = None,
) -> None:
    """Process skill integration result: update counters and emit log lines.

    Mutates *result* in-place (``skills``, ``sub_skills``, ``deployed_files``
    keys) and emits tree-item log lines via *logger*.
    """
    from apm_cli.install._services_helpers import _deployed_path_entry, _skill_bundle_file_entries

    log_fn = log_integration
    if log_fn is None and logger is not None:
        log_fn = logger.tree_item

    _skill_target_dirs: set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            _skill_target_dirs.add("copilot-cowork")
    _skill_target_paths = [f"{d}/skills/" for d in sorted(_skill_target_dirs)]
    if not _skill_target_paths:
        _skill_target_paths = ["skills/"]
    _skill_suffix, _skill_expansion = _format_target_collapse(_skill_target_paths, verbose)

    if skill_result.skill_created:
        result["skills"] += 1
        if logger:
            if _skill_expansion:
                logger.tree_item("  |-- Skill integrated:")
                for line in _skill_expansion:
                    logger.tree_item(line)
            else:
                logger.tree_item(f"  |-- Skill integrated -> {_skill_suffix}")

    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        if logger:
            if _skill_expansion:
                logger.tree_item(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated:")
                for line in _skill_expansion:
                    logger.tree_item(line)
            else:
                logger.tree_item(
                    f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated"
                    f" -> {_skill_suffix}"
                )

    if (skill_result.bin_deployed > 0 or skill_result.bin_skipped_reason) and log_fn is not None:
        from apm_cli.install.exec_gate import log_bin_status

        log_bin_status(skill_result, _skill_suffix, package_name, package_info, log_fn)

    for tp in skill_result.target_paths:
        result["deployed_files"].append(_deployed_path_entry(tp, project_root, targets))
        # #1716: also record the bundle's contained files so per-file
        # content hashes cover SKILL.md / assets / scripts. The directory
        # entry above is retained (cleanup's directory-rejection gate and
        # the manifest dir-exclusion contract depend on it); the file
        # entries give ``content-integrity`` its per-file coverage so skill
        # drift is caught under ``apm audit --ci --no-drift``.
        result["deployed_files"].extend(_skill_bundle_file_entries(tp, project_root, targets))


# ---------------------------------------------------------------------------
# _validate_bundle_slug
# ---------------------------------------------------------------------------


def _validate_bundle_slug(slug_str: str, logger: InstallLogger | None) -> bool:
    """Return True if *slug_str* passes the bundle-slug whitelist check.

    The allowed character set is ``[A-Za-z0-9._-]+`` with no leading or
    trailing dot and no ``..`` sequence.  Invalid slugs are logged as a
    warning and cause the caller to skip the instruction-staging step.
    """
    from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

    _ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    _slug_ok = (
        bool(slug_str)
        and all(c in _ALLOWED for c in slug_str)
        and not slug_str.startswith(".")
        and not slug_str.endswith(".")
        and ".." not in slug_str
    )
    if not _slug_ok:
        if logger is not None:
            logger.warning(
                f"Skipped instruction staging for unsafe slug {slug_str!r}: "
                "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
            )
        return False
    try:
        validate_path_segments(slug_str, context="bundle slug")
    except PathTraversalError as exc:
        if logger is not None:
            logger.warning(f"Skipped instruction staging for unsafe slug {slug_str!r}: {exc}")
        return False
    return True
