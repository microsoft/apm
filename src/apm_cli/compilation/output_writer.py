"""Single chokepoint for persisting compiled outputs.

All compilation targets (single-file AGENTS.md, distributed AGENTS.md,
CLAUDE.md, GEMINI.md, future targets) MUST route their writes through
``CompiledOutputWriter.write``. The writer guarantees:

1. ``BUILD_ID_PLACEHOLDER`` is replaced with a deterministic hash
   (see ``build_id.stabilize_build_id``).
2. A defensive assertion fails loudly if the placeholder survives
   stabilization, so a future code path that bypasses or breaks
   stabilization cannot silently emit ``__BUILD_ID__`` to disk.
3. Parent directories are created.
4. The write is atomic (replace-on-rename), so a crash mid-write cannot
   corrupt a pre-existing target file.

Direct ``Path.write_text`` / ``open(...).write`` on compiled output is a
contract violation -- adding new write sites without using this writer
will, by design, miss every cross-cutting concern this writer owns.

Error contract:
    - ``OSError`` from filesystem operations (mkdir, rename) propagates
      to callers, which typically log + continue.
    - ``RuntimeError`` is raised when the stabilization assertion fails
      (i.e. ``BUILD_ID_PLACEHOLDER`` survived ``stabilize_build_id``).
      This is a programmer error -- never expected in production -- and
      is intentionally NOT caught by callers' ``except OSError`` blocks
      so it surfaces as a loud traceback rather than a silent skip.
"""

from collections.abc import Mapping
from pathlib import Path

from ..security.gate import BLOCK_POLICY, ScanVerdict, SecurityGate
from ..utils.atomic_io import atomic_write_text
from .build_id import stabilize_build_id
from .constants import BUILD_ID_PLACEHOLDER


class CompiledOutputPolicyError(RuntimeError):
    """Raised before mutation when compiled output violates blocking policy."""

    def __init__(self, verdict: ScanVerdict):
        super().__init__(
            f"Compiled output blocked: {verdict.critical_count} critical "
            "hidden-character finding(s)"
        )
        self.verdict = verdict


class CompiledOutputWriter:
    """Persist compiled output with cross-cutting concerns applied."""

    def prepare(self, outputs: Mapping[Path, str]) -> tuple[dict[Path, str], ScanVerdict]:
        """Stabilize and scan a complete output batch before mutation."""
        prepared: dict[Path, str] = {}
        for path, content in outputs.items():
            final = stabilize_build_id(content)
            if BUILD_ID_PLACEHOLDER in final:
                raise RuntimeError(
                    "build_id stabilization bypassed: "
                    f"{BUILD_ID_PLACEHOLDER!r} still present after stabilization "
                    f"(target={path})"
                )
            prepared[path] = final
        verdict = SecurityGate.scan_texts(
            {str(path): content for path, content in prepared.items()},
            policy=BLOCK_POLICY,
        )
        if verdict.should_block:
            raise CompiledOutputPolicyError(verdict)
        return prepared, verdict

    def write_many(self, outputs: Mapping[Path, str]) -> ScanVerdict:
        """Validate the whole batch, then persist only through atomic writes."""
        prepared, verdict = self.prepare(outputs)
        for path, final in prepared.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, final)
        return verdict

    def write(self, path: Path, content: str) -> ScanVerdict:
        """Validate and persist one compiled output."""
        return self.write_many({path: content})
