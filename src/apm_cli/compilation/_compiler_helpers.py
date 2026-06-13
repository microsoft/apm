"""Compilation helpers extracted to keep agents_compiler.py under 800 lines.

``_detect_deployed_instructions`` is imported by both ``agents_compiler`` and
``_agents_emit`` via a late ``from .agents_compiler import ...`` call so that
test monkey-patches on ``apm_cli.compilation.agents_compiler.*`` still fire.
The re-export in ``agents_compiler.py`` ensures that import path keeps working.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Callable  # noqa: UP035

if TYPE_CHECKING:
    from ..primitives.models import PrimitiveCollection


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


def compile_agents_md(
    primitives: "PrimitiveCollection | None" = None,
    output_path: str = "AGENTS.md",
    chatmode: str | None = None,
    dry_run: bool = False,
    base_dir: str = ".",
) -> str:
    """Generate AGENTS.md with conditional sections.

    Args:
        primitives: Primitives to use, or None to discover.
        output_path: Output file path. Defaults to "AGENTS.md".
        chatmode: Specific chatmode to use, or None for default.
        dry_run: If True, don't write output file. Defaults to False.
        base_dir: Base directory for compilation. Defaults to current directory.

    Returns:
        str: Generated AGENTS.md content.
    """
    from .agents_compiler import AgentsCompiler, CompilationConfig

    config = CompilationConfig(
        output_path=output_path,
        chatmode=chatmode,
        dry_run=dry_run,
        strategy="single-file",  # Force single-file mode for backward compatibility
    )
    compiler = AgentsCompiler(base_dir)
    result = compiler.compile(config, primitives)
    if not result.success:
        raise RuntimeError(f"Compilation failed: {'; '.join(result.errors)}")
    return result.content
