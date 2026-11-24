"""Single chokepoint for persisting compiled outputs.

All compilation targets (single-file AGENTS.md, distributed AGENTS.md,
CLAUDE.md, GEMINI.md, future targets) MUST route their writes through
``CompiledOutputWriter.write``. The writer guarantees:

1. ``BUILD_ID_PLACEHOLDER`` is replaced with a deterministic hash
   (see ``build_id.stabilize_build_id``).
2. A defensive assertion fails loudly if the placeholder survives
   stabilization, so a future code path that bypasses or breaks
   stabilization cannot silently emit ``__BUILD_ID__`` to disk.
3. No output is written through a linked directory: a batch is refused
   before any write when an output under ``base_dir`` is reached through a
   symlinked path component. A final-path symlink is replaced in place by
   the atomic rename; its target is never written.
4. Parent directories are created.
5. The write is atomic (replace-on-rename), so a crash mid-write cannot
   corrupt a pre-existing target file.

Direct ``Path.write_text`` / ``open(...).write`` on compiled output is a
contract violation -- adding new write sites without using this writer
will, by design, miss every cross-cutting concern this writer owns.

Error contract:
    - ``OSError`` from filesystem operations (mkdir, rename) propagates
      to callers, which typically log + continue. ``CompiledOutputLinkError``
      is an ``OSError`` raised before any write for an output reached through
      a linked directory.
    - ``RuntimeError`` is raised when the stabilization assertion fails
      (i.e. ``BUILD_ID_PLACEHOLDER`` survived ``stabilize_build_id``).
      This is a programmer error -- never expected in production -- and
      is intentionally NOT caught by callers' ``except OSError`` blocks
      so it surfaces as a loud traceback rather than a silent skip.
"""

from collections.abc import Mapping
from pathlib import Path

from ..security.gate import BLOCK_POLICY, ScanVerdict, SecurityGate
from ..utils.atomic_io import atomic_write_text, umask_file_mode
from ..utils.path_security import has_symlink_component
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


class CompiledOutputLinkError(OSError):
    """Raised before mutation when a compiled output path crosses a symlink."""


def _refuse_linked(target: Path, root: Path) -> None:
    # Lexical relative path: resolving would follow the link being reported.
    shown = target.relative_to(root).as_posix()
    raise CompiledOutputLinkError(
        f"Refusing deployment through a symlinked path component: {shown}. "
        "Replace the symlink with a regular directory or remove it, then retry."
    )


class _LinkedParentCheck:
    """Refuse outputs reached through a linked directory.

    ``reject`` is the batch preflight: the project root is resolved once, and
    a directory proven free of links (with its ancestors) is not checked again
    for later outputs in the batch. The proof is keyed by exact spelling, so a
    case or Unicode respelling misses and is checked afresh; ``..`` paths are
    never cached. ``confirm`` re-checks one output's full directory chain
    right before it is written, so a directory swapped for a link after the
    preflight is still refused.
    """

    def __init__(self, base_dir: Path):
        resolved = base_dir.resolve()
        self._spellings = {base_dir.absolute(), resolved}
        self._resolved_root = resolved
        self._verified: set[str] = {str(spelling) for spelling in self._spellings}
        self._roots: dict[Path, Path] = {}

    def _output_root(self, target: Path) -> Path | None:
        """Return the ancestor of *target* that spells the project root, if any.

        The project may be spelled resolved or through a linked alias on either
        side; only an output outside every spelling (an explicit ``--output``
        elsewhere) has no root.
        """
        for ancestor in target.parents:
            if ancestor in self._spellings:
                return ancestor
        # Not cached: Path keys compare case-insensitively on Windows, so a
        # cached resolution could be reused for a distinct, differently cased link.
        for ancestor in target.parents:
            if ancestor.resolve() == self._resolved_root:
                return ancestor
        return None

    def _checked_ancestor(self, root: Path, parent: Path) -> Path:
        for ancestor in (parent, *parent.parents):
            if str(ancestor) in self._verified or ancestor == root:
                return ancestor
        return root

    def reject(self, path: Path) -> None:
        target = path.absolute()
        root = self._output_root(target)
        if root is None:
            return
        self._roots[path] = root
        parent = target.parent
        # ``..`` is collapsed by the full check, so only plain paths are memoized.
        plain = ".." not in parent.relative_to(root).parts
        start = self._checked_ancestor(root, parent) if plain else root
        if start == parent:
            return
        if has_symlink_component(start, parent):
            _refuse_linked(target, root)
        if plain:
            self._verified.add(str(parent))
            self._verified.update(
                str(ancestor) for ancestor in parent.parents if ancestor.is_relative_to(root)
            )

    def confirm(self, path: Path) -> None:
        root = self._roots.get(path)
        if root is None:
            return
        target = path.absolute()
        if target.parent != root and has_symlink_component(root, target.parent):
            _refuse_linked(target, root)


class CompiledOutputWriter:
    """Persist compiled output with cross-cutting concerns applied.

    ``base_dir`` is the project root that owns the outputs. Outputs below it
    must not be reached through a linked directory; outputs elsewhere (for
    example an explicit ``--output`` path) are not checked.
    """

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = base_dir

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

    def write_many(
        self,
        outputs: Mapping[Path, str],
        *,
        preserve_line_endings: bool | Mapping[Path, bool] = False,
    ) -> ScanVerdict:
        """Validate a batch, then persist it only through atomic writes.

        ``preserve_line_endings`` is for managed-section outputs whose
        hand-authored content must retain its original byte representation. A
        mapping selects that behavior per output while preserving a single
        validate-then-write batch.
        """
        check = _LinkedParentCheck(self._base_dir) if self._base_dir is not None else None
        if check is not None:
            for path in outputs:
                check.reject(path)
        prepared, verdict = self.prepare(outputs)
        for path, final in prepared.items():
            if check is not None:
                check.confirm(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            preserve = (
                preserve_line_endings.get(path, False)
                if isinstance(preserve_line_endings, Mapping)
                else preserve_line_endings
            )
            atomic_write_text(
                path,
                final,
                new_file_mode=umask_file_mode(path),
                normalize_line_endings=not preserve,
            )
        return verdict

    def write(
        self,
        path: Path,
        content: str,
        *,
        preserve_line_endings: bool = False,
    ) -> ScanVerdict:
        """Validate and persist one compiled output."""
        return self.write_many({path: content}, preserve_line_endings=preserve_line_endings)
