"""High-level constitution injection workflow used by compile command."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .constitution import read_constitution
from .constitution_block import find_existing_block, render_block

InjectionStatus = Literal["CREATED", "UPDATED", "UNCHANGED", "SKIPPED", "MISSING"]


def _split_header(content: str) -> tuple[str, str]:
    marker = "\n\n"
    if marker in content:
        idx = content.index(marker)
        return content[: idx + len(marker)], content[idx + len(marker) :]
    return content, ""


def _read_existing_content(output_path: Path) -> str:
    if not output_path.exists():
        return ""
    try:
        return output_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _compose_with_block(header_part: str, block: str, body_part: str) -> str:
    final_content = header_part + block.rstrip() + "\n\n" + body_part.lstrip("\n")
    return final_content if final_content.endswith("\n") else final_content + "\n"


def _extract_hash_value(block: str) -> str | None:
    hash_line = block.splitlines()[1] if len(block.splitlines()) > 1 else ""
    if hash_line.startswith("hash:"):
        parts = hash_line.split()
        if len(parts) >= 2:
            return parts[1]
    return None


def _select_block(existing_content: str, new_block: str) -> tuple[str, str]:
    existing_block = find_existing_block(existing_content)
    if not existing_block:
        return "CREATED", new_block.rstrip()
    if existing_block.raw.rstrip() == new_block.rstrip():
        return "UNCHANGED", existing_block.raw.rstrip()
    return "UPDATED", new_block.rstrip()


class ConstitutionInjector:
    """Encapsulates constitution detection + injection logic."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def inject(
        self, compiled_content: str, with_constitution: bool, output_path: Path
    ) -> tuple[str, InjectionStatus, str | None]:
        """Return final AGENTS.md content after optional injection."""
        existing_content = _read_existing_content(output_path)
        header_part, body_part = _split_header(compiled_content)

        if not with_constitution:
            existing_block = find_existing_block(existing_content)
            if existing_block:
                return (
                    _compose_with_block(header_part, existing_block.raw, body_part),
                    "SKIPPED",
                    None,
                )
            return compiled_content, "SKIPPED", None

        constitution_text = read_constitution(self.base_dir)
        if constitution_text is None:
            existing_block = find_existing_block(existing_content)
            if existing_block:
                return (
                    _compose_with_block(header_part, existing_block.raw, body_part),
                    "MISSING",
                    None,
                )
            return compiled_content, "MISSING", None

        new_block = render_block(constitution_text)
        status, block_to_use = _select_block(existing_content, new_block)
        return (
            _compose_with_block(header_part, block_to_use, body_part),
            status,
            _extract_hash_value(new_block),
        )
