"""Content scanner for detecting hidden Unicode characters in text files.

Scans for invisible Unicode characters that could embed hidden instructions
in prompt, instruction, and rules files. These characters are invisible to
humans but LLMs tokenize them individually, meaning models can process
instructions that humans cannot see on screen.

This module is intentionally dependency-free (no APM internals) so it can
be tested and used independently.
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple  # noqa: F401, UP035


@dataclass(frozen=True)
class ScanFinding:
    """A single suspicious character found during scanning."""

    file: str
    line: int
    column: int
    char: str
    codepoint: str  # hex, e.g. "U+200B"
    severity: str  # "critical", "warning", "info"
    category: str  # e.g. "tag-character", "bidi-override", "zero-width"
    description: str


# Each entry: (range_start, range_end, severity, category, description)
# range_end is inclusive.
_SUSPICIOUS_RANGES: list[tuple[int, int, str, str, str]] = [
    # ── Critical: no legitimate use in prompt/instruction files ──
    # Unicode tag characters — invisible ASCII mapping
    (
        0xE0001,
        0xE007F,
        "critical",
        "tag-character",
        "Unicode tag character (invisible ASCII mapping)",
    ),
    # Bidirectional override characters
    (0x202A, 0x202A, "critical", "bidi-override", "Left-to-right embedding (LRE)"),
    (0x202B, 0x202B, "critical", "bidi-override", "Right-to-left embedding (RLE)"),
    (0x202C, 0x202C, "critical", "bidi-override", "Pop directional formatting (PDF)"),
    (0x202D, 0x202D, "critical", "bidi-override", "Left-to-right override (LRO)"),
    (0x202E, 0x202E, "critical", "bidi-override", "Right-to-left override (RLO)"),
    (0x2066, 0x2066, "critical", "bidi-override", "Left-to-right isolate (LRI)"),
    (0x2067, 0x2067, "critical", "bidi-override", "Right-to-left isolate (RLI)"),
    (0x2068, 0x2068, "critical", "bidi-override", "First strong isolate (FSI)"),
    (0x2069, 0x2069, "critical", "bidi-override", "Pop directional isolate (PDI)"),
    # Variation selectors — Glassworm supply-chain attack vector.
    # These attach to visible characters, embedding invisible payload bytes
    # that AST-based tools skip entirely.  Sequences of variation selectors
    # can encode arbitrary hidden data/instructions.
    (
        0xE0100,
        0xE01EF,
        "critical",
        "variation-selector",
        "Variation selector (SMP) — no legitimate use in prompt files",
    ),
    # ── Warning: common copy-paste debris but can hide instructions ──
    (0x200B, 0x200B, "warning", "zero-width", "Zero-width space"),
    (0x200C, 0x200C, "warning", "zero-width", "Zero-width non-joiner (ZWNJ)"),
    (0x200D, 0x200D, "warning", "zero-width", "Zero-width joiner (ZWJ)"),
    (0x2060, 0x2060, "warning", "zero-width", "Word joiner"),
    # BMP variation selectors — uncommon in prompt files
    (
        0xFE00,
        0xFE0D,
        "warning",
        "variation-selector",
        "Variation selector (CJK typography variant)",
    ),
    (0xFE0E, 0xFE0E, "warning", "variation-selector", "Text presentation selector"),
    (0x00AD, 0x00AD, "warning", "invisible-formatting", "Soft hyphen"),
    # Bidirectional marks — invisible, no legitimate use in prompt files
    (0x200E, 0x200E, "warning", "bidi-mark", "Left-to-right mark (LRM)"),
    (0x200F, 0x200F, "warning", "bidi-mark", "Right-to-left mark (RLM)"),
    (0x061C, 0x061C, "warning", "bidi-mark", "Arabic letter mark (ALM)"),
    # Invisible math operators — zero-width, no use in prompt files
    (
        0x2061,
        0x2061,
        "warning",
        "invisible-formatting",
        "Function application (invisible operator)",
    ),
    (0x2062, 0x2062, "warning", "invisible-formatting", "Invisible times"),
    (0x2063, 0x2063, "warning", "invisible-formatting", "Invisible separator"),
    (0x2064, 0x2064, "warning", "invisible-formatting", "Invisible plus"),
    # Interlinear annotation markers — can hide text between delimiters
    (0xFFF9, 0xFFF9, "warning", "annotation-marker", "Interlinear annotation anchor"),
    (0xFFFA, 0xFFFA, "warning", "annotation-marker", "Interlinear annotation separator"),
    (0xFFFB, 0xFFFB, "warning", "annotation-marker", "Interlinear annotation terminator"),
    # Deprecated formatting — invisible, deprecated since Unicode 3.0
    (0x206A, 0x206F, "warning", "deprecated-formatting", "Deprecated formatting character"),
    # FEFF as mid-file BOM is handled separately in scan logic
    # ── Info: unusual whitespace, mostly harmless ──
    (0xFE0F, 0xFE0F, "info", "variation-selector", "Emoji presentation selector"),
    (0x00A0, 0x00A0, "info", "unusual-whitespace", "Non-breaking space"),
    (0x2000, 0x200A, "info", "unusual-whitespace", "Unicode whitespace character"),
    (0x205F, 0x205F, "info", "unusual-whitespace", "Medium mathematical space"),
    (0x3000, 0x3000, "info", "unusual-whitespace", "Ideographic space"),
    (0x180E, 0x180E, "info", "unusual-whitespace", "Mongolian vowel separator"),
]

# Pre-build a lookup for O(1) per-character classification.
# Maps codepoint → (severity, category, description)
_CHAR_LOOKUP: dict[int, tuple[str, str, str]] = {}
for _start, _end, _sev, _cat, _desc in _SUSPICIOUS_RANGES:
    for _cp in range(_start, _end + 1):
        _CHAR_LOOKUP[_cp] = (_sev, _cat, _desc)


def _is_emoji_char(ch: str) -> bool:
    """Return True if *ch* is an emoji base character (Unicode category So)."""
    return unicodedata.category(ch) == "So"


def _zwj_in_emoji_context(text: str, idx: int) -> bool:
    """Return True if a ZWJ at *idx* sits between two emoji-like characters.

    Looks backward past FE0F (VS16) and skin-tone modifiers (U+1F3FB–1F3FF)
    because emoji ZWJ sequences frequently interpose these between the base
    character and the joiner, e.g. 👩🏽‍🚀 = 👩 + 🏽 + ZWJ + 🚀.
    """
    # Look backward, skipping VS16 and skin-tone modifiers
    prev = idx - 1
    while prev >= 0:
        cp = ord(text[prev])
        if cp == 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF:
            prev -= 1
            continue
        break

    prev_ok = prev >= 0 and _is_emoji_char(text[prev])

    # Look forward — next char must be an emoji base
    nxt = idx + 1
    next_ok = nxt < len(text) and _is_emoji_char(text[nxt])

    return prev_ok and next_ok


class ContentScanner:
    """Scans text content for hidden or suspicious Unicode characters."""

    @staticmethod
    def scan_text(content: str, filename: str = "") -> list[ScanFinding]:
        """Scan a string for suspicious Unicode characters.

        Returns a list of findings, one per suspicious character, with
        line/column positions (1-based).
        """
        if not content:
            return []

        # Fast path: pure-ASCII content cannot contain any suspicious
        # codepoints.  str.isascii() runs at C speed (<1 µs for typical
        # prompt files) and lets us skip the Python-level character loop
        # for the ~90 %+ of files that are plain ASCII.
        if content.isascii():
            return []

        findings: list[ScanFinding] = []
        lines = content.split("\n")

        for line_idx, line_text in enumerate(lines):
            for col_idx, ch in enumerate(line_text):
                cp = ord(ch)

                # Special case: BOM (U+FEFF) at the very start of the
                # file is standard practice; mid-file is suspicious.
                if cp == 0xFEFF:
                    if line_idx == 0 and col_idx == 0:
                        findings.append(
                            ScanFinding(
                                file=filename,
                                line=1,
                                column=1,
                                char=repr(ch),
                                codepoint="U+FEFF",
                                severity="info",
                                category="bom",
                                description="Byte order mark at start of file",
                            )
                        )
                    else:
                        findings.append(
                            ScanFinding(
                                file=filename,
                                line=line_idx + 1,
                                column=col_idx + 1,
                                char=repr(ch),
                                codepoint="U+FEFF",
                                severity="warning",
                                category="zero-width",
                                description="Byte order mark in middle of file "
                                "(possible hidden content)",
                            )
                        )
                    continue

                entry = _CHAR_LOOKUP.get(cp)
                if entry is not None:
                    sev, cat, desc = entry
                    # ZWJ between emoji is legitimate (e.g. 👨‍👩‍👧)
                    if cp == 0x200D and _zwj_in_emoji_context(line_text, col_idx):
                        sev = "info"
                        desc = "Zero-width joiner (emoji sequence)"
                    findings.append(
                        ScanFinding(
                            file=filename,
                            line=line_idx + 1,
                            column=col_idx + 1,
                            char=repr(ch),
                            codepoint=f"U+{cp:04X}",
                            severity=sev,
                            category=cat,
                            description=desc,
                        )
                    )

        return findings

    @staticmethod
    def scan_file(path: Path) -> list[ScanFinding]:
        """Read a file and scan its content.

        Handles encoding errors gracefully — returns an empty list if the
        file cannot be decoded as UTF-8 (binary files, etc.).
        """
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        return ContentScanner.scan_text(content, filename=str(path))

    @staticmethod
    def has_critical(findings: list[ScanFinding]) -> bool:
        """Return True if any finding has critical severity."""
        return any(f.severity == "critical" for f in findings)

    @staticmethod
    def summarize(findings: list[ScanFinding]) -> dict[str, int]:
        """Return counts by severity level."""
        counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    @staticmethod
    def classify(
        findings: list[ScanFinding],
    ) -> tuple[bool, dict[str, int]]:
        """Combined has_critical + summarize in a single pass.

        Returns (has_critical, severity_counts).
        """
        critical = False
        counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            if f.severity == "critical":
                critical = True
        return critical, counts

    @staticmethod
    def strip_dangerous(content: str) -> str:
        """Remove critical and warning-level characters from content.

        Info-level characters (emoji selectors, non-breaking spaces, unusual
        whitespace) are preserved — they are legitimate and stripping them
        would break content (e.g. ❤️ → ❤).

        ZWJ between emoji characters is treated as info (preserved) to
        keep compound emoji like 👨‍👩‍👧 intact.
        """
        result: list[str] = []
        for i, ch in enumerate(content):
            cp = ord(ch)
            entry = _CHAR_LOOKUP.get(cp)
            if entry is not None:
                sev = entry[0]
                # ZWJ between emoji is info-level — preserve it
                if cp == 0x200D and _zwj_in_emoji_context(content, i):
                    result.append(ch)
                    continue
                if sev in ("critical", "warning"):
                    continue  # strip it
            elif cp == 0xFEFF:
                if i != 0:
                    continue  # mid-file BOM is warning-level — strip
                # leading BOM is info-level — fall through to append
            result.append(ch)
        return "".join(result)
