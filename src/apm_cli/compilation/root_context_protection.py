"""Catalog-derived ownership checks for root context compilation outputs."""

from __future__ import annotations

import os
from collections.abc import MutableSequence, MutableSet
from pathlib import Path

from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

from .claude_formatter import CLAUDE_HEADER
from .constants import (
    AGENTS_MD_GENERATED_MARKER,
    DISTRIBUTED_AGENTS_MD_GENERATED_MARKER,
    GEMINI_MD_GENERATED_MARKER,
    decode_utf8_prefix,
    has_generated_marker_header,
)

_AGENTS_ROOT_GENERATED_MARKERS = (
    AGENTS_MD_GENERATED_MARKER,
    DISTRIBUTED_AGENTS_MD_GENERATED_MARKER,
)
_ROOT_CONTEXT_BY_COMPILE_FAMILY = {
    "agents": ("AGENTS.md", _AGENTS_ROOT_GENERATED_MARKERS),
    "claude": ("CLAUDE.md", (CLAUDE_HEADER,)),
    "gemini": ("GEMINI.md", (GEMINI_MD_GENERATED_MARKER,)),
    "vscode": ("AGENTS.md", _AGENTS_ROOT_GENERATED_MARKERS),
}


def catalog_root_context_markers() -> dict[str, tuple[str, ...]]:
    """Return generated root context filenames implied by the target catalog."""
    markers_by_name: dict[str, tuple[str, ...]] = {}
    for profile in KNOWN_TARGETS.values():
        family = profile.compile_family
        if family is None:
            continue
        root_context = _ROOT_CONTEXT_BY_COMPILE_FAMILY.get(family)
        if root_context is None:
            continue
        filename, markers = root_context
        existing = markers_by_name.setdefault(filename, markers)
        if existing != markers:
            raise ValueError(f"Conflicting root context markers for {filename}")
    return dict(sorted(markers_by_name.items()))


def hand_authored_root_context_blocks_write(
    path: Path,
    *,
    base_dir: Path,
    resolved_base_dir: Path,
    protected_paths: MutableSet[Path],
    warnings: MutableSequence[str],
) -> bool:
    """Return whether an existing project-root context file must be retained."""
    canonical_name = None
    normalized_path = Path(os.path.abspath(path))
    lexical_root = Path(os.path.abspath(path.parent)) == resolved_base_dir
    accepted_markers_by_name = catalog_root_context_markers()
    canonical_paths = tuple(resolved_base_dir / filename for filename in accepted_markers_by_name)
    if lexical_root:
        for candidate in canonical_paths:
            if normalized_path == candidate:
                canonical_name = candidate.name
                break
            try:
                if os.path.samestat(path.lstat(), candidate.lstat()):
                    canonical_name = candidate.name
                    break
            except OSError:
                continue
    if canonical_name is None:
        if not path.is_file():
            return False
        try:
            resolved = ensure_path_within(path, base_dir)
            if resolved.parent != resolved_base_dir:
                return False
            for candidate in canonical_paths:
                if candidate.is_file() and path.samefile(candidate):
                    canonical_name = candidate.name
                    break
        except (OSError, PathTraversalError):
            return False
        if canonical_name is None:
            return False
    rel_path = portable_relpath(path, base_dir)
    if lexical_root and path.is_symlink():
        protected_paths.add(path)
        warnings.append(
            f"Protected {rel_path}: root context symlinks are not overwritten. "
            "Replace the symlink with a regular generated file before rerunning."
        )
        return True
    if not path.is_file():
        return False
    try:
        ensure_path_within(path, base_dir)
        with path.open("rb") as handle:
            prefix = decode_utf8_prefix(handle.read(4096))
    except (OSError, PathTraversalError, UnicodeDecodeError) as exc:
        protected_paths.add(path)
        warnings.append(
            f"Skipped {rel_path}: could not verify the APM-generated marker; "
            f"file will not be overwritten ({type(exc).__name__}). "
            "Fix file access, UTF-8 encoding, or path containment, then rerun."
        )
        return True
    accepted_markers = accepted_markers_by_name[canonical_name]
    if has_generated_marker_header(prefix, accepted_markers):
        return False
    protected_paths.add(path)
    warnings.append(
        f"Protected {rel_path}: hand-authored file will not be overwritten. "
        "To regenerate it, delete or rename the file, then re-run 'apm compile'."
    )
    return True
