"""Skill directories -> ``.apm/skills/<name>/`` (agentskills.io layout)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from apm_cli.integration.skill_integrator import normalize_skill_name, validate_skill_name
from apm_cli.security.gate import ignore_non_content

from ..model import Finding
from . import ConvertContext, ConvertError, ConvertResult
from .base import emit_markdown, read_markdown, refuse_credentials

_TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".py", ".js", ".ts", ".env"}
)


def assert_no_symlinks(directory: Path) -> None:
    """Refuse to copy a skill that contains any symlink (mirrors plugin_parser)."""
    if directory.is_symlink():
        raise ConvertError("skill directory is a symlink")
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise ConvertError("skill directory contains a symlink")


class SkillDirConverter:
    """Copy a skill directory, fixing its ``name`` to the normalised folder name."""

    id = "passthrough.skill_dir"

    def handles(self, converter_id: str) -> bool:
        return converter_id == self.id

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        source = finding.abs_path
        if source is None or not source.is_dir():
            raise ConvertError("skill source is not a directory")
        assert_no_symlinks(source)
        skill_md = source / "SKILL.md"
        if not skill_md.is_file():
            raise ConvertError("SKILL.md missing")
        total = sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
        if total > ctx.limits.max_file_bytes * 10:
            raise ConvertError("skill directory exceeds the size limit")
        result = ConvertResult()
        for text_file in source.rglob("*"):
            if text_file.is_file() and text_file.suffix in _TEXT_SUFFIXES:
                try:
                    refuse_credentials(text_file.read_text(encoding="utf-8", errors="ignore"))
                except ConvertError as exc:
                    raise ConvertError(f"{text_file.name}: {exc}") from None
        meta, body = read_markdown(skill_md, ctx.limits.max_file_bytes, result)
        expected = dest.name
        ok, _reason = validate_skill_name(expected)
        if not ok:
            raise ConvertError("destination skill name is invalid")
        if dest.exists():
            raise ConvertError("destination already exists")
        shutil.copytree(source, dest, symlinks=False, ignore=ignore_non_content)
        declared = str(meta.get("name", "")).strip()
        if declared != expected:
            meta["name"] = expected
            result.transform("frontmatter.name", "name aligned with the normalised folder name")
            if not meta.get("description"):
                meta["description"] = expected.replace("-", " ")
                result.default("frontmatter.description", "generated from skill name")
            emit_markdown(dest / "SKILL.md", meta, body)
        result.written.append(dest / "SKILL.md")
        result.keep("directory", "copied with symlink and non-content filters")
        return result


def normalized_skill_folder(name: str) -> str:
    return normalize_skill_name(name)


CONVERTERS = (SkillDirConverter(),)
