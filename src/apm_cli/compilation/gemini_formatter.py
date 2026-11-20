"""GEMINI.md formatter for Gemini CLI integration.

Generates a lightweight GEMINI.md stub that imports AGENTS.md via Gemini
CLI's ``@path`` preprocessor.  Gemini CLI resolves ``@./AGENTS.md`` at
load time, so there is no need to duplicate the instruction roll-up that
the AGENTS.md pipeline already produces.
"""

import builtins
from dataclasses import dataclass, field
from pathlib import Path

from ..primitives.models import Instruction, PrimitiveCollection
from ..version import get_version
from .constants import (
    BUILD_ID_PLACEHOLDER,
    GEMINI_MD_GENERATED_MARKER,
    has_generated_marker_header,
)

# CRITICAL: Shadow Click commands to prevent namespace collision
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass
class GeminiPlacement:
    """Result of GEMINI.md placement analysis."""

    gemini_path: Path
    instructions: builtins.list[Instruction]


@dataclass
class GeminiCompilationResult:
    """Result of GEMINI.md compilation."""

    success: bool
    placements: builtins.list[GeminiPlacement]
    content_map: builtins.dict[Path, str]
    warnings: builtins.list[str] = field(default_factory=list)
    errors: builtins.list[str] = field(default_factory=list)
    stats: builtins.dict[str, float] = field(default_factory=dict)


class GeminiFormatter:
    """Formatter for generating GEMINI.md stub files.

    Generates a single GEMINI.md at the project root that imports
    AGENTS.md via Gemini CLI's ``@path`` directive.  The actual
    instruction roll-up is produced by the AGENTS.md pipeline;
    this formatter only creates the thin import wrapper.
    """

    def __init__(self, base_dir: str = ".") -> None:
        try:
            self.base_dir = Path(base_dir).resolve()
        except (OSError, FileNotFoundError):
            self.base_dir = Path(base_dir).absolute()

        self.warnings: builtins.list[str] = []
        self.errors: builtins.list[str] = []

    def format_distributed(
        self,
        primitives: PrimitiveCollection,
        placement_map: builtins.dict[Path, builtins.list[Instruction]] | None = None,
        config: dict | None = None,
        *,
        dry_run: bool = False,
    ) -> GeminiCompilationResult:
        """Generate a GEMINI.md stub that imports AGENTS.md.

        The *placement_map* argument is accepted for interface
        compatibility with other formatters but is not used -- the
        stub always lives at the project root.

        AGENTS.md compiles first. When it is absent afterwards (no
        instructions, or ``--clean`` removed it), the stub would import a
        missing file, so none is written and a stale APM stub is removed;
        a hand-authored GEMINI.md is never touched (#3994).
        """
        self.warnings.clear()
        self.errors.clear()

        try:
            gemini_path = self.base_dir / "GEMINI.md"
            if not dry_run and not (self.base_dir / "AGENTS.md").is_file():
                self._retire_stub(gemini_path)
                return GeminiCompilationResult(
                    success=True,
                    placements=[],
                    content_map={},
                    warnings=self.warnings.copy(),
                    errors=self.errors.copy(),
                    stats={"gemini_files_generated": 0},
                )
            content = self._generate_stub()

            placement = GeminiPlacement(
                gemini_path=gemini_path,
                instructions=[],
            )

            stats: builtins.dict[str, float] = {
                "gemini_files_generated": 1,
                "primitives_found": primitives.count(),
            }

            return GeminiCompilationResult(
                success=True,
                placements=[placement],
                content_map={gemini_path: content},
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats=stats,
            )
        except Exception as e:
            self.errors.append(f"GEMINI.md formatting failed: {e!s}")
            return GeminiCompilationResult(
                success=False,
                placements=[],
                content_map={},
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats={},
            )

    def _retire_stub(self, gemini_path: Path) -> None:
        """Remove an APM-generated GEMINI.md whose AGENTS.md import is gone."""
        try:
            header = gemini_path.read_text(encoding="utf-8")[:2048]
        except (OSError, UnicodeDecodeError):
            return
        if has_generated_marker_header(header, (GEMINI_MD_GENERATED_MARKER,)):
            gemini_path.unlink()
            self.warnings.append("Removed GEMINI.md: the AGENTS.md it imported no longer exists")

    def _generate_stub(self) -> str:
        """Generate the GEMINI.md stub content."""
        lines = [
            "# GEMINI.md",
            GEMINI_MD_GENERATED_MARKER,
            BUILD_ID_PLACEHOLDER,
            f"<!-- APM Version: {get_version()} -->",
            "",
            "@./AGENTS.md",
            "",
        ]
        return "\n".join(lines)
