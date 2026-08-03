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
    - ``HandAuthoredOverwriteError`` (a ``RuntimeError``) is raised before
      mutation when ``protect_hand_authored`` is set and a target already
      exists without an APM generated-marker. Like the stabilization guard
      it is not an ``OSError``, so write-site ``except OSError`` blocks let
      it propagate to the compile command, which renders actionable guidance.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path

from ..security.gate import BLOCK_POLICY, ScanVerdict, SecurityGate
from ..utils.atomic_io import atomic_write_text
from .build_id import stabilize_build_id
from .constants import APM_GENERATED_MARKER_PREFIX, BUILD_ID_PLACEHOLDER

# Head-only scan window for the ownership marker. Mirrors the read-side
# convention used by ``--clean`` orphan cleanup
# (``distributed_compiler._file_has_apm_marker``) so both surfaces agree on
# what "APM-generated" means.
_MARKER_SCAN_BYTES = 4096


class CompiledOutputPolicyError(RuntimeError):
    """Raised before mutation when compiled output violates blocking policy."""

    def __init__(self, verdict: ScanVerdict):
        super().__init__(
            f"Compiled output blocked: {verdict.critical_count} critical "
            "hidden-character finding(s)"
        )
        self.verdict = verdict


class HandAuthoredOverwriteError(RuntimeError):
    """Raised before mutation when a write would clobber a hand-authored file.

    A target that already exists on disk without an APM generated-marker was
    written by a human; overwriting it silently is the footgun this guard
    exists to prevent. The offending paths are carried on the exception so
    callers can render actionable guidance (migrate into a local primitive,
    or re-run with ``--force``). This is a ``RuntimeError`` (not ``OSError``)
    so the existing ``except OSError`` blocks around write sites do not
    swallow it -- it must surface to the user, not be logged-and-continued.
    """

    def __init__(self, paths: Iterable[Path]):
        self.paths: list[Path] = list(paths)
        names = ", ".join(sorted({p.name for p in self.paths}))
        super().__init__(f"Refusing to overwrite hand-authored file(s): {names}")


def file_is_hand_authored(path: Path) -> bool:
    """Return True when ``path`` exists but carries no APM generated-marker.

    APM stamps every root context file it owns with an
    ``APM_GENERATED_MARKER_PREFIX`` line at the top, so a target that exists
    without that marker in its head was authored by a human and must not be
    silently overwritten. Semantics:

    * missing file        -> ``False`` (nothing to protect; safe to create)
    * marker in head      -> ``False`` (APM-owned; safe to regenerate)
    * no marker in head   -> ``True``  (hand-authored; protect)
    * unreadable file     -> ``True``  (cannot confirm ownership; do not clobber)

    The head-only scan (first ``_MARKER_SCAN_BYTES`` bytes) matches the
    orphan-cleanup read path so recompiles of APM-owned files never trip the
    guard while genuine hand-authored files always do. Read in binary and
    decode the byte prefix (mirroring
    ``distributed_compiler._file_has_apm_marker``) so the scan window is a
    true byte bound -- reading text characters could pull in more than
    ``_MARKER_SCAN_BYTES`` bytes for non-ASCII content and diverge from the
    cleanup semantics.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_MARKER_SCAN_BYTES).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return APM_GENERATED_MARKER_PREFIX not in head


def hand_authored_overwrite_message(paths: Iterable[Path]) -> str:
    """Build the actionable error/preview message for hand-authored collisions."""
    names = sorted({Path(p).name for p in paths})
    if len(names) == 1:
        subject = f"{names[0]} exists and was not generated by APM"
    else:
        subject = f"{', '.join(names)} exist and were not generated by APM"
    return (
        f"{subject} -- refusing to overwrite. Migrate the content into a local "
        'primitive (.apm/instructions/local.instructions.md with applyTo: "**") so '
        "compile merges it, then re-run -- or pass --force to overwrite (a .bak copy "
        "of the original is written first)."
    )


def _backup_hand_authored(path: Path) -> Path:
    """Copy an existing hand-authored file to ``<name>.bak`` before overwrite.

    Preserves the original bytes verbatim (no encoding round-trip) so the
    backup restores exactly what the user wrote. ``CLAUDE.md`` -> ``CLAUDE.md.bak``.
    """
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(path.read_bytes())
    return backup


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

    def write_many(
        self,
        outputs: Mapping[Path, str],
        *,
        force: bool = False,
        protect_hand_authored: bool = False,
    ) -> ScanVerdict:
        """Validate the whole batch, then persist only through atomic writes.

        When ``protect_hand_authored`` is set, any target that already exists
        without an APM generated-marker is treated as hand-authored: the whole
        batch is refused (:class:`HandAuthoredOverwriteError`) before any file
        is written, unless ``force`` is also set -- in which case each such
        file is copied to ``<name>.bak`` before being overwritten.

        Protection is opt-in so that managed-section writes (which
        deliberately edit a hand-authored file in place) and low-level callers
        keep the raw overwrite contract. Callers that own the primary output
        target (CLAUDE.md / AGENTS.md / GEMINI.md and per-client variants)
        pass ``protect_hand_authored=True``.
        """
        prepared, verdict = self.prepare(outputs)
        hand_authored = (
            [path for path in prepared if file_is_hand_authored(path)]
            if protect_hand_authored
            else []
        )
        if hand_authored and not force:
            # Raise before any mutation so the batch is all-or-nothing.
            raise HandAuthoredOverwriteError(hand_authored)
        backup_targets = set(hand_authored)  # non-empty only when force is set
        for path, final in prepared.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if path in backup_targets:
                _backup_hand_authored(path)
            atomic_write_text(path, final)
        return verdict

    def write(
        self,
        path: Path,
        content: str,
        *,
        force: bool = False,
        protect_hand_authored: bool = False,
    ) -> ScanVerdict:
        """Validate and persist one compiled output."""
        return self.write_many(
            {path: content}, force=force, protect_hand_authored=protect_hand_authored
        )
