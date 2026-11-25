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

import stat
from collections.abc import Mapping
from pathlib import Path

from ..security.gate import BLOCK_POLICY, ScanVerdict, SecurityGate
from ..utils.atomic_io import atomic_write_text, umask_file_mode
from ..utils.path_security import has_symlink_component, symlinked_deploy_refusal
from ..utils.paths import portable_relpath
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


class CompiledOutputSpecialFileError(OSError):
    """Raised before any read when a compiled output path is not a regular file (#5187)."""


def refuse_unsafe_output(path: Path, base_dir: Path, *, managed: bool) -> None:
    """Refuse an output that is a FIFO, socket, device or directory before reading it.

    A managed-section output must also not be a symlink. Reading a FIFO would block.
    """
    try:
        mode = path.lstat().st_mode
    except OSError:
        return
    if stat.S_ISLNK(mode):
        if managed:
            from .managed_section import ManagedSectionError

            raise ManagedSectionError(
                f"{path} is a symlink. Managed-section output must be a regular file."
            )
        try:
            # A link is read through: what it names must be a regular file too (#5187).
            mode = path.stat().st_mode
        except OSError:
            return
    if not stat.S_ISREG(mode):
        from ..utils.diagnostics import printable_ascii_text

        # Named as spelled: a link's own name, never the file it points to.
        parent = portable_relpath(path.parent, base_dir)
        shown = printable_ascii_text(path.name if parent == "." else f"{parent}/{path.name}")
        raise CompiledOutputSpecialFileError(
            f"Refusing to compile: {shown} is not a regular file "
            "(a FIFO, socket, device or directory), so it was not read."
        )


def output_error_message(action: str, path: Path, exc: Exception) -> str:
    """Return a special-file refusal as is; wrap any other failure with *action* and *path*."""
    if isinstance(exc, CompiledOutputSpecialFileError):
        return str(exc)
    return f"{action} {path}: {exc}"


def _output_root(base_dir: Path, target: Path) -> Path | None:
    """Return the ancestor of *target* that spells *base_dir*, if any.

    The project may be spelled resolved or through a linked alias on either
    side; only an output outside every spelling (an explicit ``--output``
    elsewhere) has no root.
    """
    spellings = {base_dir.absolute(), base_dir.resolve()}
    for ancestor in target.parents:
        if ancestor in spellings:
            return ancestor
    resolved_root = base_dir.resolve()
    for ancestor in target.parents:
        if ancestor.resolve() == resolved_root:
            return ancestor
    return None


def _reject_linked_parent(base_dir: Path, path: Path) -> None:
    target = path.absolute()
    root = _output_root(base_dir, target)
    if root is not None and has_symlink_component(root, target.parent):
        # Lexical relative path: resolving would follow the link being reported.
        shown = target.relative_to(root).as_posix()
        raise CompiledOutputLinkError(symlinked_deploy_refusal(shown))


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
        if self._base_dir is not None:
            for path in outputs:
                _reject_linked_parent(self._base_dir, path)
        prepared, verdict = self.prepare(outputs)
        for path, final in prepared.items():
            if self._base_dir is not None:
                # Re-checked right before the write: a directory replaced by a
                # link after the batch preflight must still be refused.
                _reject_linked_parent(self._base_dir, path)
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
        if self._base_dir is not None and prepared:
            from ..core.agents_md_compile_record import record_compiled_outputs

            # The re-anchor scan reads every written output, wherever --output put it (#5235).
            record_compiled_outputs(self._base_dir, prepared)
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
