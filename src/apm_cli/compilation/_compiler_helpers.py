"""Compilation helpers extracted to keep agents_compiler.py under 800 lines.

``_detect_deployed_instructions`` is imported by both ``agents_compiler`` and
``_agents_emit`` via a late ``from .agents_compiler import ...`` call so that
test monkey-patches on ``apm_cli.compilation.agents_compiler.*`` still fire.
The re-export in ``agents_compiler.py`` ensures that import path keeps working.
"""

from pathlib import Path
from typing import Callable  # noqa: UP035


def _detect_deployed_instructions(
    rules_dir: Path, base_dir: Path, warn_fn: Callable[[str], None]
) -> bool:
    """Return True when rules_dir contains at least one .md file safely inside base_dir.

    Shared by the Claude and Copilot compile paths: each passes its own
    target-specific rules directory (.claude/rules/ or .github/instructions/)
    so the detection logic stays in one place (R0801 guard).
    """
    if not rules_dir.is_dir():
        return False
    from ..utils.path_security import PathTraversalError, ensure_path_within

    try:
        ensure_path_within(rules_dir, base_dir)
    except PathTraversalError:
        warn_fn(f"{rules_dir} is a symlink outside the project root -- ignoring")
        return False
    return any(rules_dir.glob("*.md"))
