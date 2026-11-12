"""Build ID stabilization for compiled outputs.

Formatters insert ``BUILD_ID_PLACEHOLDER`` (a sentinel marker line) into
their generated content. Before persisting that content to disk, callers
must replace the placeholder with a deterministic 12-char SHA256 hash so
the file stays byte-stable across rebuilds with identical input.

The hash is computed over the content with the placeholder line *removed*
so the hash is not self-referential -- it would otherwise change every
time the placeholder string itself changed.

This module is the single source of truth for that replacement; all
compiled-output write sites must route through ``CompiledOutputWriter``
(see ``output_writer.py``) which calls this helper.
"""

import hashlib
import re

from .constants import BUILD_ID_PLACEHOLDER

# The truncated hash detects edits; it is not a provenance or security attestation.
_BUILD_ID_LINE_RE = re.compile(r"^<!-- Build ID: ([0-9a-f]{12}) -->$")


def stabilize_build_id(content: str) -> str:
    """Replace BUILD_ID_PLACEHOLDER with a deterministic 12-char SHA256 hash.

    Idempotent: returns ``content`` unchanged if no placeholder is present.
    Preserves a trailing newline if the input had one.
    """
    lines = content.splitlines()
    try:
        idx = lines.index(BUILD_ID_PLACEHOLDER)
    except ValueError:
        return content

    hash_input_lines = [line for i, line in enumerate(lines) if i != idx]
    build_id = hashlib.sha256("\n".join(hash_input_lines).encode("utf-8")).hexdigest()[:12]

    return content.replace(BUILD_ID_PLACEHOLDER, f"<!-- Build ID: {build_id} -->", 1)


def _find_build_id(lines: list[str]) -> tuple[int, re.Match[str]] | None:
    """Return (index, match) for the single Build ID line, or None."""
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _BUILD_ID_LINE_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def has_valid_build_id(content: str) -> bool:
    """Check that generated content has not changed since its Build ID was made."""
    matches = list(re.finditer(r"^<!-- Build ID: [0-9a-f]{12} -->$", content, re.MULTILINE))
    if len(matches) != 1:
        return False
    match = matches[0]
    template = content[: match.start()] + BUILD_ID_PLACEHOLDER + content[match.end() :]
    return stabilize_build_id(template) == content


def has_build_id_line(content: str) -> bool:
    """Return whether content contains exactly one Build ID line (regardless of hash match).

    Presence alone never authorizes cleanup. Call ``has_valid_build_id`` to
    verify content before deleting a generated file, including legacy output.
    """
    return _find_build_id(content.splitlines()) is not None
