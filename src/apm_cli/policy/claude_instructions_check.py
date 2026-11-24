"""Audit check: an instruction routed to AGENTS.md is not dropped silently (#5344)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import CheckResult

CHECK_NAME = "claude-instructions-compiled"


def _instruction_sources(project_root: Path, modules_root: Path, lock: Any) -> list[Path]:
    """The instructions install integrates for Claude: the project's own, then
    each locked dependency whose targets keep Claude, found link-safely."""
    from ..integration.instruction_integrator import (
        InstructionIntegrator,
        claude_dependency_sources,
    )

    integrator = InstructionIntegrator()
    found = list(integrator.find_instruction_files(project_root))
    if lock is not None:
        for _dep, package_info, source_plan in claude_dependency_sources(
            lock, modules_root, include_local=True
        ):
            found.extend(integrator.find_instruction_files(package_info.install_path, source_plan))
    return found


def check_claude_instructions_compiled(
    project_root: Path,
    targets: Sequence[Any],
    *,
    user_scope: bool,
    modules_root: Path,
    lock: Any = None,
) -> CheckResult:
    """Warn while a routed Claude instruction has no compiled AGENTS.md carrying it.

    A warning in 0.39.0: the check passes (``audit --ci`` exits 0) and names
    the instructions.

    Install names the same instructions with a warning; both use the routing
    owner in compilation/claude_agents_md.py, so they never disagree.
    """
    if user_scope or not any(getattr(t, "compile_family", None) == "claude" for t in targets):
        return CheckResult(name=CHECK_NAME, passed=True, message="No project Claude target")
    from ..compilation.claude_agents_md import uncompiled_routed_instructions
    from ..install.phases.finalize import uncompiled_instructions_warning

    names = uncompiled_routed_instructions(
        project_root, _instruction_sources(Path(project_root), Path(modules_root), lock)
    )
    if not names:
        return CheckResult(
            name=CHECK_NAME, passed=True, message="Routed Claude instructions are compiled"
        )
    return CheckResult(
        name=CHECK_NAME,
        passed=True,
        warned=True,
        advisory=True,
        message=f"{len(names)} instruction(s) routed to AGENTS.md are not compiled",
        details=[uncompiled_instructions_warning(names)],
    )


def claude_instructions_result(
    project_root: Path, targets: Sequence[Any], prepared_replay: Any, lock: Any
) -> CheckResult:
    """The baseline batch's project-scope entry point: modules from the replay if any."""
    from ..constants import APM_MODULES_DIR

    modules_root = (
        prepared_replay.modules_root
        if prepared_replay is not None
        else Path(project_root) / APM_MODULES_DIR
    )
    return check_claude_instructions_compiled(
        project_root, targets, user_scope=False, modules_root=modules_root, lock=lock
    )
