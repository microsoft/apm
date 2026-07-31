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
