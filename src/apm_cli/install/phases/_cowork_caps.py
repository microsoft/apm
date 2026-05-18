"""Cowork skill-count and size cap checks (Amendment 7).

Extracted from ``integrate.py`` to keep that module under 500 lines.
All names are re-exported via ``integrate.py`` so existing import paths
remain unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


_COWORK_MAX_SKILLS: int = 50
"""Warn when the cowork skills directory contains more than this many skills."""

_COWORK_MAX_SKILL_SIZE: int = 1_048_576  # 1 MB
"""Warn when any source SKILL.md exceeds this size in bytes."""


def _warn_cowork_issue(ctx: InstallContext, message: str) -> None:
    if ctx.logger:
        ctx.logger.warning(message, symbol="warning")
    if ctx.diagnostics:
        ctx.diagnostics.warn(message, package="cowork")


def _emit_cowork_count_warning(ctx: InstallContext, skill_dirs: list) -> None:
    if len(skill_dirs) <= _COWORK_MAX_SKILLS:
        return
    _warn_cowork_issue(
        ctx,
        f"Cowork skills directory contains {len(skill_dirs)} skills "
        f"(cap: {_COWORK_MAX_SKILLS}). Consider removing unused skills.",
    )


def _emit_cowork_skill_size_warning(ctx: InstallContext, skill_dir, size: int) -> None:
    if size <= _COWORK_MAX_SKILL_SIZE:
        return
    size_mb = size / (1024 * 1024)
    _warn_cowork_issue(
        ctx,
        f"Skill '{skill_dir.name}/SKILL.md' is {size_mb:.1f} MB "
        "(cap: 1 MB). Large skills may degrade Copilot performance.",
    )


def _check_cowork_caps(ctx: InstallContext) -> None:
    """Emit warn-only diagnostics for cowork skill count and size caps.

    Walks ``<cowork_root>/skills/*/SKILL.md`` (existing + just-installed)
    and checks against ``_COWORK_MAX_SKILLS`` and ``_COWORK_MAX_SKILL_SIZE``.
    Install still succeeds regardless.
    """
    if not ctx.targets:
        return

    cowork_root = None
    for t in ctx.targets:
        if t.name == "copilot-cowork" and t.resolved_deploy_root is not None:
            cowork_root = t.resolved_deploy_root
            break
    if cowork_root is None:
        return
    if not cowork_root.is_dir():
        return

    skill_dirs = sorted(
        d for d in cowork_root.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )

    _emit_cowork_count_warning(ctx, skill_dirs)

    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        try:
            size = skill_md.stat().st_size
        except OSError:
            continue
        _emit_cowork_skill_size_warning(ctx, skill_dir, size)
