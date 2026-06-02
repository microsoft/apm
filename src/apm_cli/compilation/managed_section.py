"""Managed-section support for AGENTS.md updates (issue #1540).

When ``agents_md.mode: managed_section`` is configured, APM replaces ONLY
the text between configurable start/end markers instead of overwriting the
entire file. This lets teams keep hand-written AGENTS.md content alongside
APM-managed sections.

Behavior contract
-----------------
- Markers appear EXACTLY ONCE each: any duplication raises ``ManagedSectionError``
  with a clear message. Overwriting an ambiguous file is unsafe.
- Markers MUST be present: if either marker is absent the function raises
  ``ManagedSectionError`` with the marker text included so users know exactly
  what to add and where.
- Content OUTSIDE the markers is preserved verbatim.
- The markers themselves are preserved in the output (surrounding the new content).
"""

from __future__ import annotations


class ManagedSectionError(ValueError):
    """Raised when the managed-section markers are missing or duplicated."""


def apply_managed_section(
    existing_content: str,
    new_section_content: str,
    start_marker: str,
    end_marker: str,
) -> str:
    """Replace the managed block between ``start_marker`` and ``end_marker``.

    Args:
        existing_content: Full current content of the AGENTS.md file.
        new_section_content: New content to place between the markers.
        start_marker: The opening marker string (e.g. ``<!-- apm:start -->``).
        end_marker: The closing marker string (e.g. ``<!-- apm:end -->``).

    Returns:
        The updated file content with only the managed section replaced.

    Raises:
        ManagedSectionError: If either marker is absent or appears more than once.
    """
    if not start_marker or not end_marker:
        raise ManagedSectionError("start_marker and end_marker must be non-empty strings.")
    if start_marker == end_marker:
        raise ManagedSectionError(
            f"start_marker and end_marker must be distinct; both are {start_marker!r}."
        )

    start_count = existing_content.count(start_marker)
    end_count = existing_content.count(end_marker)

    if start_count > 1 or end_count > 1:
        duplicated = []
        if start_count > 1:
            duplicated.append(f"start marker ({start_marker!r}) {start_count} time(s)")
        if end_count > 1:
            duplicated.append(f"end marker ({end_marker!r}) {end_count} time(s)")
        raise ManagedSectionError(
            "Managed-section markers must appear exactly once in the file, but found "
            + " and ".join(duplicated)
            + ". Remove the duplicate markers before running APM again."
        )

    if start_count == 0 or end_count == 0:
        missing = []
        if start_count == 0:
            missing.append(f"start marker {start_marker!r}")
        if end_count == 0:
            missing.append(f"end marker {end_marker!r}")
        raise ManagedSectionError(
            f"Managed-section markers not found in the target file "
            f"({', '.join(missing)} absent). "
            "Add the missing marker(s) to AGENTS.md to enable managed-section mode. "
            f"Example:\n  {start_marker}\n  <APM will insert content here>\n  {end_marker}"
        )

    start_idx = existing_content.index(start_marker)
    end_idx = existing_content.index(end_marker)

    if end_idx < start_idx:
        raise ManagedSectionError(
            f"End marker ({end_marker!r}) appears before start marker ({start_marker!r}). "
            "Ensure the start marker comes first in AGENTS.md."
        )

    before = existing_content[: start_idx + len(start_marker)]
    after = existing_content[end_idx:]

    stripped = new_section_content.rstrip("\n")
    middle = f"\n{stripped}\n" if stripped else "\n"

    return f"{before}{middle}{after}"
