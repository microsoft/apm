"""Pre-flight helpers for the ``apm compile`` command."""

from __future__ import annotations

import sys
from pathlib import Path

from ...compilation import AgentsCompiler
from ...constants import APM_DIR, APM_MODULES_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...primitives.discovery import discover_primitives
from ._display import _display_validation_errors


def _ensure_compilable_content(logger: CommandLogger, dry_run: bool) -> None:
    """Validate that the current project has content worth compiling."""
    from ...compilation.constitution import find_constitution

    if not Path(APM_YML_FILENAME).exists():
        logger.error("Not an APM project - no apm.yml found")
        logger.progress(" To initialize an APM project, run:")
        logger.progress("   apm init")
        sys.exit(1)

    apm_modules_exists = Path(APM_MODULES_DIR).exists()
    constitution_exists = find_constitution(Path(".")).exists()
    apm_dir = Path(APM_DIR)
    local_apm_has_content = apm_dir.exists() and (
        any(apm_dir.rglob("*.instructions.md")) or any(apm_dir.rglob("*.chatmode.md"))
    )
    if apm_modules_exists or local_apm_has_content or constitution_exists:
        return

    has_empty_apm = (
        apm_dir.exists()
        and not any(apm_dir.rglob("*.instructions.md"))
        and not any(apm_dir.rglob("*.chatmode.md"))
    )
    if has_empty_apm:
        logger.error("No instruction files found in .apm/ directory")
        logger.progress(" To add instructions, create files like:")
        logger.progress("   .apm/instructions/coding-standards.instructions.md")
        logger.progress("   .apm/chatmodes/backend-engineer.chatmode.md")
    else:
        logger.error("No APM content found to compile")
        logger.progress(" To get started:")
        logger.progress("   1. Install APM dependencies: apm install <owner>/<repo>")
        logger.progress("   2. Or create local instructions: mkdir -p .apm/instructions")
        logger.progress("   3. Then create .instructions.md or .chatmode.md files")
    if not dry_run:
        sys.exit(1)


def _run_validation_mode(logger: CommandLogger) -> None:
    """Run validation-only mode and exit the command."""
    logger.start("Validating APM context...", symbol="gear")
    compiler = AgentsCompiler(".")
    try:
        primitives = discover_primitives(".")
    except Exception as e:
        logger.error(f"Failed to discover primitives: {e}")
        logger.progress(f" Error details: {type(e).__name__}")
        sys.exit(1)
    validation_errors = compiler.validate_primitives(primitives)
    if validation_errors:
        _display_validation_errors(validation_errors)
        logger.error(f"Validation failed with {len(validation_errors)} errors")
        sys.exit(1)
    logger.success("All primitives validated successfully!")
    logger.progress(f"Validated {primitives.count()} primitives:")
    logger.progress(f"  * {len(primitives.chatmodes)} chatmodes")
    logger.progress(f"  * {len(primitives.instructions)} instructions")
    logger.progress(f"  * {len(primitives.contexts)} contexts")
    try:
        from ...models.apm_package import APMPackage

        mcp_count = len(APMPackage.from_apm_yml(Path(APM_YML_FILENAME)).get_mcp_dependencies())
        if mcp_count > 0:
            logger.progress(f"  * {mcp_count} MCP dependencies")
    except Exception:
        pass
