"""Instruction format converters extracted from InstructionIntegrator.

Contains the five static converter methods that transform APM instruction
content to target-specific formats:

- ``_convert_to_cursor_rules``    -- applyTo -> globs frontmatter
- ``_convert_to_windsurf_rules``  -- applyTo -> trigger/globs frontmatter
- ``_convert_to_kiro_steering``   -- applyTo -> inclusion/fileMatch frontmatter
- ``_convert_to_claude_rules``    -- applyTo -> paths frontmatter
- ``_convert_to_antigravity_rules`` -- applyTo -> trigger/globs frontmatter

These live in ``_ConverterMixin`` so ``InstructionIntegrator`` can inherit
them while staying within the 800-line hard gate.  Each method is a pure
static function with no side effects; it takes a string and returns a string.

Tests that call ``InstructionIntegrator._convert_to_cursor_rules(content)``
(and similar) continue to work because ``InstructionIntegrator`` inherits
``_ConverterMixin`` and all five names are accessible via normal MRO.
"""

from __future__ import annotations

import re

from apm_cli.utils.patterns import parse_apply_to, yaml_double_quote


class _ConverterMixin:
    """Private mixin providing the five instruction format converters."""

    @staticmethod
    def _convert_to_cursor_rules(content: str) -> str:
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
        globs = parse_apply_to(apply_to)
        if len(globs) == 1:
            parts.append(f"globs: {yaml_double_quote(globs[0])}")
        elif globs:
            parts.append("globs:")
            parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    @staticmethod
    def _convert_to_windsurf_rules(content: str) -> str:
        """Convert APM instruction content to Windsurf rules ``.md`` format.

        Parses existing YAML frontmatter via ``yaml.safe_load``, maps
        ``applyTo`` to Windsurf's ``trigger: glob`` + ``globs`` frontmatter.
        Instructions without ``applyTo`` become ``trigger: always_on`` rules.

        Ref: https://docs.windsurf.com/windsurf/cascade/memories
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        apply_to = ""

        # Parse existing frontmatter with the bounded loader so a hostile
        # frontmatter block in an untrusted package cannot hang the parser.
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception:
                fm = {}
            apply_to = str(fm.get("applyTo", "")).strip()

        # Build Windsurf rules frontmatter
        parts = ["---"]
        # Sanitize: strip newlines to prevent frontmatter injection
        # via crafted applyTo values (e.g. "**\ntrigger: always_on").
        safe_apply_to = apply_to.replace("\n", " ").replace("\r", " ").strip()
        globs = parse_apply_to(safe_apply_to)
        if globs:
            parts.append("trigger: glob")
            if len(globs) == 1:
                parts.append(f"globs: {yaml_double_quote(globs[0])}")
            else:
                parts.append("globs:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        else:
            parts.append("trigger: always_on")
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    @staticmethod
    def _convert_to_kiro_steering(content: str) -> str:
        """Convert APM instructions to Kiro steering format.

        Kiro steering files use ``inclusion: always`` for unconditional
        guidance and ``inclusion: fileMatch`` plus ``fileMatchPattern`` for
        path-scoped guidance. APM's ``applyTo`` frontmatter is the source of
        truth for that scoping.
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        globs = []

        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception:
                fm = {}
            raw_apply_to = fm.get("applyTo", "")
            if isinstance(raw_apply_to, list):
                globs = [
                    s
                    for item in raw_apply_to
                    if (s := str(item).replace("\n", " ").replace("\r", " ").strip())
                ]
            else:
                safe_apply_to = str(raw_apply_to).replace("\n", " ").replace("\r", " ").strip()
                globs = parse_apply_to(safe_apply_to)

        parts = ["---"]
        if globs:
            parts.append("inclusion: fileMatch")
            if len(globs) == 1:
                parts.append(f"fileMatchPattern: {yaml_double_quote(globs[0])}")
            else:
                parts.append("fileMatchPattern:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        else:
            parts.append("inclusion: always")
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    @staticmethod
    def _convert_to_claude_rules(content: str) -> str:
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
        globs = parse_apply_to(apply_to)
        if globs:
            parts = ["---", "paths:"]
            parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
            parts.append("---")
            return "\n".join(parts) + "\n\n" + body.lstrip("\n")

        # No applyTo -> unconditional rule, return body without frontmatter
        return body.lstrip("\n")

    @staticmethod
    def _convert_to_antigravity_rules(content: str) -> str:
        """Convert APM instruction content to Antigravity CLI rules format.

        Parses existing YAML frontmatter, maps ``applyTo`` to Antigravity's
        ``trigger: glob`` + ``globs`` frontmatter.
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        globs = []

        # Parse existing frontmatter
        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to parse instruction frontmatter YAML: %s", e
                )
                fm = {}
            raw_apply_to = fm.get("applyTo", "")
            if isinstance(raw_apply_to, list):
                globs = [
                    s
                    for item in raw_apply_to
                    if (s := str(item).replace("\n", " ").replace("\r", " ").strip())
                ]
            else:
                safe_apply_to = str(raw_apply_to).replace("\n", " ").replace("\r", " ").strip()
                globs = parse_apply_to(safe_apply_to)

        # Build Antigravity rules frontmatter
        parts = ["---"]
        if globs:
            parts.append("trigger: glob")
            if len(globs) == 1:
                parts.append(f"globs: {yaml_double_quote(globs[0])}")
            else:
                parts.append("globs:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
            parts.append("---")
            return "\n".join(parts) + "\n\n" + body.lstrip("\r\n")

        # No applyTo -> unconditional rule, return body without frontmatter
        return body.lstrip("\r\n")
