"""User-scope root-context compilation engine.

Reads global (apply_to-less) instructions from ~/.apm/apm_modules and
writes each active target's user-scope root context file:
  claude  -> ~/.claude/CLAUDE.md  (or $CLAUDE_CONFIG_DIR/CLAUDE.md)
  codex   -> ~/.codex/AGENTS.md
  gemini  -> ~/.gemini/GEMINI.md
  copilot -> ~/.copilot/AGENTS.md
  vscode  -> ~/.copilot/AGENTS.md (same deploy root at user scope)
  cursor  -> ~/.cursor/AGENTS.md
  opencode -> ~/.config/opencode/AGENTS.md

Files are ONLY written when:
1. The target supports user scope (for_scope returns non-None)
2. The target has a recognised compile_family with a root-file mapping
3. Global instructions exist in the module tree
4. The existing file either does not exist OR carries the generated marker

Hand-authored files (no marker) are left untouched.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging as _logging_module

    from ..integration.targets import TargetProfile
    from ..primitives.models import Instruction

# Root filename by compile_family.  Targets whose compile_family is not in
# this map do not produce a root file (e.g. family=None for agent-skills).
_ROOT_FILENAME: dict[str, str] = {
    "claude": "CLAUDE.md",
    "agents": "AGENTS.md",
    "vscode": "AGENTS.md",
    "gemini": "GEMINI.md",
}


@dataclass(frozen=True)
class UserRootCompileResult:
    """Result for one user-scope root context compilation target."""

    target: str
    path: Path | None
    status: str


def _resolve_deploy_root(profile: TargetProfile) -> Path:
    """Return the absolute deploy root for a user-scoped TargetProfile.

    After for_scope(user_scope=True):
    * profile.resolved_deploy_root is set   -> use it directly
    * otherwise                             -> Path.home() / profile.root_dir
    """
    if profile.resolved_deploy_root is not None:
        return profile.resolved_deploy_root
    return Path.home() / profile.root_dir


def _finalize_build_id(content: str) -> str:
    """Replace the BUILD_ID_PLACEHOLDER sentinel with a 12-char content hash.

    The hash is computed over all lines EXCEPT the placeholder line so the
    result is deterministic (not self-referential).
    """
    from .constants import BUILD_ID_PLACEHOLDER

    lines = content.splitlines()
    try:
        idx = lines.index(BUILD_ID_PLACEHOLDER)
    except ValueError:
        return content

    hash_input_lines = [line for i, line in enumerate(lines) if i != idx]
    build_id = hashlib.sha256("\n".join(hash_input_lines).encode("utf-8")).hexdigest()[:12]
    lines[idx] = f"<!-- Build ID: {build_id} -->"
    return "\n".join(lines) + "\n"


def _generate_content(instructions: list[Instruction]) -> str:
    """Generate the root context file content from a list of global instructions.

    Embeds the APM-generated marker and a deterministic Build ID so that
    subsequent runs can detect APM-owned files and apply overwrite protection.

    ASCII-only: no Unicode in the generated skeleton; instruction *content*
    is passed through as-is (callers are responsible for encoding checks).
    """
    from .agents_compiler import _COPILOT_ROOT_GENERATED_MARKER
    from .constants import BUILD_ID_PLACEHOLDER

    sections: list[str] = [
        _COPILOT_ROOT_GENERATED_MARKER,
        BUILD_ID_PLACEHOLDER,
        "",
    ]

    for instruction in instructions:
        sections.append(instruction.content.strip())
        sections.append("")

    return _finalize_build_id("\n".join(sections))


def discover_global_instructions(
    source_root: Path,
    *,
    logger: _logging_module.Logger | None = None,
) -> list[Instruction]:
    """Return global (apply_to-less) instructions under ``source_root/apm_modules``.

    Returns an empty list when the ``apm_modules`` tree is absent or carries no
    global instructions.  Results are sorted by file path for determinism so
    callers (the compile engine and the install-time hint) agree on ordering.
    """
    from ..primitives.discovery import discover_primitives

    log = logger or logging.getLogger(__name__)

    apm_modules = source_root / "apm_modules"
    if not apm_modules.is_dir():
        log.debug(
            "user_root_context: apm_modules dir not found at %s -- no global instructions",
            apm_modules,
        )
        return []

    primitives = discover_primitives(str(apm_modules))
    return sorted(
        [instr for instr in primitives.instructions if not instr.apply_to],
        key=lambda instr: str(instr.file_path),
    )


def compile_user_root_contexts(
    targets: Iterable[TargetProfile],
    source_root: Path,
    *,
    dry_run: bool = False,
    logger: _logging_module.Logger | None = None,
) -> list[UserRootCompileResult]:
    """Compile user-scope root context files from global (apply_to-less) instructions.

    Iterates over *targets*, skipping any that:
    * do not support user scope (for_scope returns None)
    * have no recognised compile_family root-file mapping

    For each remaining target the function discovers global instructions from
    ``source_root / "apm_modules"``, generates content, and writes the root
    file -- unless the existing file is hand-authored (no marker).

    Args:
        targets: Iterable of TargetProfile instances to process.
        source_root: Root of the user's APM installation tree,
            e.g. ``Path.home() / ".apm"``.
        dry_run: When True, no files are written or directories created.
            The returned status values reflect what *would* happen.
        logger: Optional logger.  Falls back to ``logging.getLogger(__name__)``.

    Returns:
        A list of UserRootCompileResult entries, one per target that was
        evaluated.  Each entry contains ``target``, ``path``, and ``status``.

        Status values:
        * ``"written"``              -- file was created or updated
        * ``"unchanged"``            -- file already matches generated content
        * ``"would-write"``          -- dry_run; file would have been written
        * ``"skipped-no-instructions"`` -- no global instructions found
        * ``"skipped-hand-authored"`` -- existing file has no APM marker
        * ``"error:<msg>"``          -- OS error during read or write
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within
    from .agents_compiler import _COPILOT_ROOT_GENERATED_MARKER

    log = logger or logging.getLogger(__name__)

    results: list[UserRootCompileResult] = []

    apm_modules = source_root / "apm_modules"
    if not apm_modules.is_dir():
        log.debug(
            "user_root_context: apm_modules dir not found at %s -- no root files written",
            apm_modules,
        )
        return results

    global_instructions = discover_global_instructions(source_root, logger=log)

    for target in targets:
        # Resolve to user scope; None == target does not support user scope
        scoped = target.for_scope(user_scope=True)
        if scoped is None:
            log.debug("user_root_context: %s does not support user scope -- skipping", target.name)
            continue

        family = scoped.compile_family
        if family not in _ROOT_FILENAME:
            log.debug(
                "user_root_context: %s compile_family=%r has no root-file mapping -- skipping",
                scoped.name,
                family,
            )
            continue

        if not global_instructions:
            log.debug(
                "user_root_context: no global instructions found in %s -- skipping %s",
                apm_modules,
                scoped.name,
            )
            results.append(UserRootCompileResult(scoped.name, None, "skipped-no-instructions"))
            continue

        deploy_root = _resolve_deploy_root(scoped)
        root_filename = _ROOT_FILENAME[family]
        try:
            output_path = ensure_path_within(deploy_root / root_filename, deploy_root)
        except PathTraversalError as exc:
            log.warning("user_root_context: unsafe output path for %s: %s", scoped.name, exc)
            results.append(
                UserRootCompileResult(scoped.name, deploy_root / root_filename, f"error:{exc}")
            )
            continue

        content = _generate_content(global_instructions)

        # -- overwrite protection --
        if output_path.exists():
            try:
                existing = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("user_root_context: cannot read %s: %s", output_path, exc)
                results.append(UserRootCompileResult(scoped.name, output_path, f"error:{exc}"))
                continue

            if _COPILOT_ROOT_GENERATED_MARKER not in existing:
                log.info(
                    "user_root_context: %s is hand-authored (no APM marker) -- not overwriting",
                    output_path,
                )
                results.append(
                    UserRootCompileResult(scoped.name, output_path, "skipped-hand-authored")
                )
                continue

            if existing == content:
                log.debug("user_root_context: %s is unchanged", output_path)
                results.append(UserRootCompileResult(scoped.name, output_path, "unchanged"))
                continue

        if dry_run:
            log.debug("user_root_context: [dry-run] would write %s", output_path)
            results.append(UserRootCompileResult(scoped.name, output_path, "would-write"))
            continue

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            log.debug("user_root_context: wrote %s", output_path)
            results.append(UserRootCompileResult(scoped.name, output_path, "written"))
        except OSError as exc:
            log.warning("user_root_context: failed to write %s: %s", output_path, exc)
            results.append(UserRootCompileResult(scoped.name, output_path, f"error:{exc}"))

    return results
