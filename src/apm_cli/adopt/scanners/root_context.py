"""Scanner for root context files each harness reads implicitly."""

from __future__ import annotations

from collections.abc import Iterable

from apm_cli.integration.targets import KNOWN_TARGETS

from ..model import HarnessKind, RawFinding, Scope
from ..registry import ScanContext, ScanRule, findings_for_rule

# (tool, relative path, kind)
_PROJECT_FILES: tuple[tuple[str, str, HarnessKind], ...] = (
    ("root", "AGENTS.md", HarnessKind.ROOT_CONTEXT),
    ("claude", "CLAUDE.md", HarnessKind.ROOT_CONTEXT),
    ("claude", ".claude/CLAUDE.md", HarnessKind.ROOT_CONTEXT),
    ("gemini", "GEMINI.md", HarnessKind.ROOT_CONTEXT),
    ("cursor", ".cursorrules", HarnessKind.ROOT_CONTEXT),
    ("root", "STYLE.md", HarnessKind.STYLE),
)

_PRIVATE_PROJECT_FILES: tuple[tuple[str, str], ...] = (
    ("claude", "CLAUDE.local.md"),
    ("claude", ".claude/CLAUDE.local.md"),
)

_USER_FILES: tuple[tuple[str, str], ...] = (
    ("codex", ".codex/AGENTS.md"),
    ("gemini", ".gemini/GEMINI.md"),
    ("opencode", ".config/opencode/AGENTS.md"),
    ("cursor", ".cursor/AGENTS.md"),
    ("copilot", ".copilot/AGENTS.md"),
)


def _rule(tool: str, rel: str, kind: HarnessKind, format_id: str = "root_context") -> ScanRule:
    return ScanRule(
        tool=tool, kind=kind, relative_glob=rel, format_id=format_id, primitive="instructions"
    )


class RootContextScanner:
    """Find ``AGENTS.md``, ``CLAUDE.md`` and friends."""

    name = "root-context"

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]:
        rules: list[ScanRule] = []
        if ctx.scope is Scope.PROJECT:
            rules.extend(_rule(tool, rel, kind) for tool, rel, kind in _PROJECT_FILES)
            rules.extend(
                _rule(tool, rel, HarnessKind.ROOT_CONTEXT, format_id="private")
                for tool, rel in _PRIVATE_PROJECT_FILES
            )
        else:
            claude = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
            if claude is not None:
                rules.append(
                    _rule("claude", f"{claude.root_dir}/CLAUDE.md", HarnessKind.ROOT_CONTEXT)
                )
            rules.extend(_rule(tool, rel, HarnessKind.ROOT_CONTEXT) for tool, rel in _USER_FILES)
        for rule in rules:
            yield from findings_for_rule(ctx, rule)
