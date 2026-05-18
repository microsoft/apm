"""Instruction-format transform helpers extracted from instruction_integrator.py."""

from __future__ import annotations

import re
from pathlib import Path


def _instruction_target_name(source_file: Path, mapping) -> str:
    """Return the deployed filename for one instruction file."""
    if mapping.format_id not in ("cursor_rules", "claude_rules", "windsurf_rules"):
        return source_file.name
    stem = source_file.name
    if stem.endswith(".instructions.md"):
        stem = stem[: -len(".instructions.md")]
    return f"{stem}{mapping.extension}"


def _copy_instruction_for_format(integrator, fmt: str, source_file: Path, target_path: Path) -> int:
    """Copy one instruction file using the requested target format."""
    if fmt == "cursor_rules":
        return integrator.copy_instruction_cursor(source_file, target_path)
    if fmt == "claude_rules":
        return integrator.copy_instruction_claude(source_file, target_path)
    if fmt == "windsurf_rules":
        return integrator.copy_instruction_windsurf(source_file, target_path)
    return integrator.copy_instruction(source_file, target_path)


def _apply_cursor_rules_format(content: str) -> str:
    """Convert APM instruction content to Cursor Rules ``.mdc`` format.

    Parses existing YAML frontmatter, maps ``applyTo`` -> ``globs``,
    extracts or generates a ``description``, and rewrites the
    frontmatter in Cursor's expected format.
    """
    body = content
    apply_to = ""
    description = ""

    # Parse existing frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if fm_match:
        fm_block = fm_match.group(1)
        body = content[fm_match.end() :]

        for line in fm_block.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("applyTo:"):
                apply_to = line_stripped[len("applyTo:") :].strip().strip("'\"")
            elif line_stripped.startswith("description:"):
                description = line_stripped[len("description:") :].strip().strip("'\"")

    # Generate description from first content sentence if missing
    if not description:
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                description = stripped.split(".")[0].strip()
                break

    # Build Cursor Rules frontmatter
    parts = ["---"]
    if description:
        parts.append(f"description: {description}")
    if apply_to:
        parts.append(f'globs: "{apply_to}"')
    parts.append("---")

    return "\n".join(parts) + "\n\n" + body.lstrip("\n")


def _apply_windsurf_rules_format(content: str) -> str:
    """Convert APM instruction content to Windsurf rules ``.md`` format.

    Parses existing YAML frontmatter via ``yaml.safe_load``, maps
    ``applyTo`` to Windsurf's ``trigger: glob`` + ``globs`` frontmatter.
    Instructions without ``applyTo`` become ``trigger: always_on`` rules.

    Ref: https://docs.windsurf.com/windsurf/cascade/memories
    """
    import yaml

    body = content
    apply_to = ""

    # Parse existing frontmatter with yaml.safe_load (consistent with
    # _write_windsurf_agent_skill and all other frontmatter parsers).
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if fm_match:
        body = content[fm_match.end() :]
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            fm = {}
        apply_to = str(fm.get("applyTo", "")).strip()

    # Build Windsurf rules frontmatter
    parts = ["---"]
    if apply_to:
        # Sanitize: strip newlines to prevent frontmatter injection
        # via crafted applyTo values (e.g. "**\ntrigger: always_on").
        safe_apply_to = apply_to.replace("\n", " ").replace("\r", " ").strip()
        parts.append("trigger: glob")
        parts.append(f'globs: "{safe_apply_to}"')
    else:
        parts.append("trigger: always_on")
    parts.append("---")

    return "\n".join(parts) + "\n\n" + body.lstrip("\n")


def _apply_claude_rules_format(content: str) -> str:
    """Convert APM instruction content to Claude Code rules ``.md`` format.

    Parses existing YAML frontmatter, maps ``applyTo`` to ``paths``
    (YAML list), and rewrites the frontmatter in Claude's expected
    format.  Instructions without ``applyTo`` become unconditional
    rules (no ``paths`` key).

    Ref: https://code.claude.com/docs/en/memory#organize-rules-with-claude%2Frules%2F
    """
    body = content
    apply_to = ""

    # Parse existing frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if fm_match:
        fm_block = fm_match.group(1)
        body = content[fm_match.end() :]

        for line in fm_block.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("applyTo:"):
                apply_to = line_stripped[len("applyTo:") :].strip().strip("'\"")

    # Build Claude rules frontmatter (only when path-scoped)
    if apply_to:
        parts = ["---"]
        parts.append("paths:")
        parts.append(f'  - "{apply_to}"')
        parts.append("---")
        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    # No applyTo -> unconditional rule, return body without frontmatter
    return body.lstrip("\n")
