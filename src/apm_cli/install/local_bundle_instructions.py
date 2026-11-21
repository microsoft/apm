"""Claude instructions from a local bundle converge on AGENTS.md (#4034).

A plugin-native bundle carries instruction sources (``instructions/<name>``)
with ``applyTo`` frontmatter. For a Claude target the #3939 routing owner
decides each one: routed instructions are staged as compile input, exactly
like a compile-only target's, and the rest become ``.claude/rules/`` files
rendered by the Claude rule projection (``applyTo`` -> ``paths:``) instead of
being copied verbatim.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

CLAUDE_RULES_FORMAT = "claude_rules"


def _metadata(text: str) -> dict:
    from ..utils.yaml_io import loads_frontmatter

    metadata = loads_frontmatter(text, preserve_body=True).metadata
    return metadata if isinstance(metadata, dict) else {}


def _apply_to(text: str) -> object:
    return _metadata(text).get("applyTo")


def _is_claude_rule(text: str) -> bool:
    """Return True for a source already in Claude rule format (it has ``paths:``).

    A Claude-packed bundle ships its ``.claude/rules/`` files as
    ``instructions/<name>``; those keep their bytes, never re-rendered. An
    authored ``paths:`` wins over ``applyTo`` (Claude Code reads only ``paths:``).
    """
    return "paths" in _metadata(text)


def _compile_discoverable(source_rel: str) -> bool:
    head, _, name = source_rel.partition("/")
    return head == "instructions" and "/" not in name and name.endswith(".instructions.md")


class ClaudeBundleRouting:
    """Decide, per Claude bundle instruction, whether it compiles into AGENTS.md.

    The #3939 owner decides: ``routes_to_agents_md`` with the live project
    root, and ``claude_agents_md_blocker`` (warned once, keeping the legacy
    ``.claude/rules/`` layout). User scope never routes.
    """

    def __init__(self, project_root: Path, *, user_scope: bool, diagnostics: Any, logger: Any):
        from ..compilation.claude_agents_md import claude_agents_md_blocker

        self._project_root = project_root
        self._user_scope = user_scope
        self._warn: Callable[[str], None] = (
            diagnostics.warn if diagnostics is not None else getattr(logger, "warning", print)
        )
        self._blocker = None if user_scope else claude_agents_md_blocker(project_root)
        self._warned = False

    @staticmethod
    def source_text(
        render_format: str | None, src: Path, read: Callable[[Path], str | None]
    ) -> str | None:
        """Return an APM instruction source when *render_format* is the Claude rule format.

        ``None`` (copy as packed, never staged) for other formats and for a
        source that is already a Claude rule.
        """
        if render_format != CLAUDE_RULES_FORMAT:
            return None
        text = read(src)
        return None if text is None or _is_claude_rule(text) else text

    def staged(self, text: str | None, source_rel: str, *, default: bool) -> bool:
        """Return whether a bundle file is staged; non-Claude files keep *default*.

        A Claude instruction is staged only under a name ``apm compile``
        discovers (``instructions/<name>.instructions.md``); any other name
        keeps its rendered rule so its content is never lost.
        """
        if text is None:
            return default
        return _compile_discoverable(source_rel) and self._stages(text)

    @staticmethod
    def stage_tail(text: str | None, source_rel: str, deploy_rel: str) -> str:
        """Return the path under ``.apm/instructions/`` for a staged file.

        Nested subdirectories under ``instructions/`` are preserved so two
        files with the same basename do not collide (PR #1217). A Claude
        instruction stages under its source name, not the ``rules/`` path
        it would deploy to, so ``apm compile`` discovers it.
        """
        rel = source_rel if text is not None else deploy_rel
        return rel.split("/", 1)[1] if "/" in rel else Path(rel).name

    @staticmethod
    def rendered(text: str | None, staged: bool, normalized: str | None) -> str | None:
        """Return the bytes to deploy: a rendered Claude rule, else *normalized*."""
        return normalized if text is None or staged else render_claude_bundle_rule(text)

    def _stages(self, text: str) -> bool:
        """Return True when *text* is staged as compile input."""
        from ..compilation.claude_agents_md import (
            claude_agents_md_blocker_warning,
            routes_to_agents_md,
        )

        if self._user_scope or not routes_to_agents_md(_apply_to(text), self._project_root):
            return False
        if self._blocker is None:
            return True
        if not self._warned:
            self._warned = True
            self._warn(claude_agents_md_blocker_warning(self._blocker))
        return False


def render_claude_bundle_rule(text: str) -> str:
    """Render a bundle instruction source as a Claude ``.claude/rules/`` file."""
    from ..integration.instruction_integrator import InstructionIntegrator

    return InstructionIntegrator._convert_to_claude_rules(text)
