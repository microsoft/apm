"""Audit check: an instruction routed to AGENTS.md is not dropped silently (#5344)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import CheckResult

CHECK_NAME = "claude-instructions-compiled"


def _instruction_sources(project_root: Path, modules_root: Path) -> list[Path]:
    """The project's own and its installed packages' compile-input instructions."""
    found: list[Path] = []
    own = project_root / ".apm" / "instructions"
    for root in (own, modules_root):
        if not root.is_dir() or root.is_symlink():
            continue
        found.extend(
            path
            for path in sorted(root.rglob("*.instructions.md"))
            if not path.is_symlink() and (root == own or "/.apm/instructions/" in path.as_posix())
        )
    return found


def check_claude_instructions_compiled(
    project_root: Path, targets: Sequence[Any], *, user_scope: bool, modules_root: Path
) -> CheckResult:
    """Fail while a routed Claude instruction has no compiled AGENTS.md carrying it.

    Install names the same instructions with a warning; both use the routing
    owner in compilation/claude_agents_md.py, so they never disagree.
    """
    if user_scope or not any(getattr(t, "compile_family", None) == "claude" for t in targets):
        return CheckResult(name=CHECK_NAME, passed=True, message="No project Claude target")
    from ..compilation.claude_agents_md import uncompiled_routed_instructions
    from ..install.phases.finalize import uncompiled_instructions_warning

    names = uncompiled_routed_instructions(
        project_root, _instruction_sources(Path(project_root), Path(modules_root))
    )
    if not names:
        return CheckResult(
            name=CHECK_NAME, passed=True, message="Routed Claude instructions are compiled"
        )
    return CheckResult(
        name=CHECK_NAME,
        passed=False,
        message=f"{len(names)} instruction(s) routed to AGENTS.md are not compiled",
        details=[uncompiled_instructions_warning(names)],
    )
