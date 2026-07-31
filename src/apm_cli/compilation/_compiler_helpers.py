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
    rules_dir: Path,
    base_dir: Path,
    warn_fn: Callable[[str], None],
    expected_filenames: set[str] | None = None,
) -> bool:
    """Return True when rules_dir contains at least one rule file safely inside base_dir.

    Shared by the Claude, Copilot, and Antigravity compile paths: each passes
    its own target-specific rules directory (.claude/rules/, .github/instructions/,
    or .agents/rules/) so the detection logic stays in one place (R0801 guard).
    """
    if not rules_dir.is_dir():
        return False
    from ..utils.path_security import PathTraversalError, ensure_path_within

    try:
        ensure_path_within(rules_dir, base_dir)
    except PathTraversalError:
        warn_fn(f"{rules_dir} is a symlink outside the project root -- ignoring")
        return False

    if expected_filenames is not None:
        return any((rules_dir / name).is_file() for name in expected_filenames)
    return any(rules_dir.glob("*.md"))


def _build_expected_rule_filenames(
    target_key: str,
    primitives: "PrimitiveCollection",
) -> set[str]:
    """Return the set of expected rule filenames for *target_key* given *primitives*.

    Shared by the Antigravity/Copilot (_compile_distributed) and Claude
    (_compile_claude_md) dedup paths so the filename-building policy stays in
    one place (R0801 guard).

    Args:
        target_key: Canonical KNOWN_TARGETS key (e.g. "claude", "copilot",
            "antigravity").
        primitives: Compiled agent primitives whose instructions drive the
            expected filenames.

    Returns:
        A set of expected rule filenames (stem + target extension), or an empty
        set if the target profile is unknown or lacks an ``instructions``
        primitive mapping.
    """
    from ..integration.targets import KNOWN_TARGETS

    expected: set[str] = set()
    target_profile = KNOWN_TARGETS.get(target_key)
    if target_profile and "instructions" in target_profile.primitives:
        mapping = target_profile.primitives["instructions"]
        extension = mapping.extension
        for instr in primitives.instructions:
            stem = instr.file_path.name
            if stem.endswith(".instructions.md"):
                stem = stem[: -len(".instructions.md")]
            expected.add(f"{stem}{extension}")
    return expected


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
