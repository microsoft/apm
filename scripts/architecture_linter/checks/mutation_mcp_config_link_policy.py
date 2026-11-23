"""MCP client config writers apply one symlink policy before they write.

Guard ``hooks-integrations-mcp-config-link-policy``: every function under
``adapters/client/`` that writes an MCP config file (it journals the write
with ``journal_config_write``) is on a closed allowlist. The writers that own
their link check route it through ``MCPClientAdapter._is_linked_config`` in
``base.py`` (or its project half, ``_is_linked_project_config``), so a config
file that is, or is reached through, a symlink is never written or replaced
by a regular file. A new writer that is not on the allowlist, or a listed
writer that drops its link check, is a violation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.mutation_write_shared import (
    GROUP,
    _has_fixed,
    _python_paths,
    _read_required,
    _require,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "mutation_writes.mcp_config_link_policy"
GUARD_ID = "hooks-integrations-mcp-config-link-policy"

_TREE = "src/apm_cli/adapters/client/"
_OWNER = "src/apm_cli/adapters/client/base.py"
_JOURNAL = "journal_config_write"
_LINK_CHECKS = frozenset({"_is_linked_config", "_is_linked_project_config", "_config_is_linked"})
# Writers that must check the link policy in the writing function itself.
_GUARDED = {
    ("src/apm_cli/adapters/client/base.py", "_write_json_config"),
    ("src/apm_cli/adapters/client/codex.py", "_write_config"),
    ("src/apm_cli/adapters/client/hermes.py", "update_config"),
}
# Writers whose destination is decided by their own path owner (claude-mcp-config-paths,
# jetbrains-copilot-mcp-config-path); they stay listed so the world remains closed.
_PATH_OWNED = {
    ("src/apm_cli/adapters/client/claude.py", "_merge_user_mcp"),
    ("src/apm_cli/adapters/client/intellij.py", "_write_config"),
}
_DELEGATES = {("src/apm_cli/adapters/client/codex.py", "_config_is_linked"): "_is_linked_config"}


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                names.add(name)
    return names


def _functions(tree: ast.Module) -> Iterable[ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _check_mcp_config_link_policy(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every MCP config writer is listed, and the listed ones check the link policy."""
    paths = tuple(sorted(_python_paths(provider, under=_TREE)))
    facts_by_path, failures = _read_required(provider, RULE_ID, paths)
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    owner = facts_by_path.get(_OWNER)
    for needle in ("def _is_linked_config(", "self._is_linked_project_config(config_path)"):
        findings.extend(
            _require(
                owner is not None and _has_fixed(owner, needle),
                RULE_ID,
                _OWNER,
                f"the MCP config link policy owner is missing: {needle}",
            )
        )
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            tree = ast.parse(source_text(facts_by_path[path]))
        except SyntaxError as exc:
            findings.append(violation(RULE_ID, path, f"unparsable source: {exc.msg}"))
            continue
        for function in _functions(tree):
            key = (path, function.name)
            called = _called_names(function)
            if key in _DELEGATES:
                seen.add(key)
                if _DELEGATES[key] not in called:
                    message = f"{function.name} must delegate to {_DELEGATES[key]}"
                    findings.append(violation(RULE_ID, path, message, line=function.lineno))
            if _JOURNAL not in called or function.name == _JOURNAL:
                continue
            seen.add(key)
            if key in _PATH_OWNED:
                continue
            if key not in _GUARDED:
                message = (
                    f"{function.name} writes an MCP config outside the closed writer list; "
                    "route the write through an existing writer that checks _is_linked_config"
                )
                findings.append(violation(RULE_ID, path, message, line=function.lineno))
            elif not called & _LINK_CHECKS:
                message = f"{function.name} must check the link policy (_is_linked_config)"
                findings.append(violation(RULE_ID, path, message, line=function.lineno))
    for path, name in sorted((_GUARDED | _PATH_OWNED | set(_DELEGATES)) - seen):
        findings.append(violation(RULE_ID, path, f"listed MCP config writer {name} not found"))
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(GUARD_ID,),
        description="MCP client config writers check the one symlink policy before writing.",
        check=_check_mcp_config_link_policy,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
