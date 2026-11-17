"""Skill-layout analyzer (#3773).

``resolve_skill_layout`` in ``src/apm_cli/integration/skill_layout.py`` owns which
skill layout (converged ``.agents/skills`` or per-client ``--legacy-skill-paths``
roots) a deployment uses. Install-time callers pass the flag through it; every
flagless rebuild of recorded deployments (audit drift replay, uninstall cleanup
and survivor re-integration) resolves targets through ``resolve_audit_targets``
or ``resolve_reintegration_targets``, which recover the recorded layout.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_SKILL_LAYOUT = "install-deployment-skill-layout"
_OWNER = "src/apm_cli/integration/skill_layout.py"
_AUDIT_OWNER = "src/apm_cli/integration/targets.py"
_PRIMITIVE = "apply_legacy_skill_paths"
_BLIND_RESOLVER = "resolve_targets"

# Modules that rebuild or clean recorded skill deployments without the flag.
REBUILD_MODULES: tuple[str, ...] = (
    "src/apm_cli/commands/export_patch.py",
    "src/apm_cli/commands/uninstall/cli.py",
    "src/apm_cli/commands/uninstall/engine.py",
    "src/apm_cli/install/audit_replay.py",
    "src/apm_cli/install/drift.py",
)

# (path, enclosing function) -> why a layout-blind target resolution is safe.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "src/apm_cli/commands/uninstall/cli.py",
        "_publish_native_registration",
    ): "reads native plugin registration capability only; no skill path is derived",
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    _OWNER: (
        "def resolve_skill_layout(",
        "def resolve_reintegration_targets(",
        "recorded_paths=recorded_paths,",
    ),
    _AUDIT_OWNER: (
        "return resolve_reintegration_targets(",
        "recorded_paths=recorded_deployed_paths(lock),",
    ),
}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _enclosing(index, node: ast.AST) -> str:
    anchor = index.definition_anchor(node)
    if anchor is None:
        return ""
    try:
        return index.function_qualname(anchor)
    except RuntimeError:
        return getattr(anchor, "name", "")


def _primitive_uses(index) -> list[ast.AST]:
    uses: list[ast.AST] = []
    for node in index.walk(index.root):
        if isinstance(node, ast.ImportFrom):
            uses.extend(node for alias in node.names if alias.name == _PRIMITIVE)
        elif _name(node) == _PRIMITIVE:
            uses.append(node)
    return uses


def check_skill_layout(provider: FactsProvider) -> tuple[Violation, ...]:
    """Skill layout is decided only by ``resolve_skill_layout``."""
    findings: list[Violation] = []
    for path, fragments in _REQUIRED.items():
        facts, missing = _facts_for(provider, path, GUARD_SKILL_LAYOUT)
        findings.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            findings.append(
                _summary(
                    GUARD_SKILL_LAYOUT,
                    path,
                    "Audit targets must recover the recorded skill layout through "
                    f"resolve_reintegration_targets ({_OWNER})",
                )
            )

    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _OWNER:
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        for node in _primitive_uses(index):
            findings.append(
                violation(
                    GUARD_SKILL_LAYOUT,
                    path,
                    f"'{_PRIMITIVE}' bypasses the skill-layout owner; use "
                    f"resolve_skill_layout ({_OWNER})",
                    line=getattr(node, "lineno", 1),
                    column=getattr(node, "col_offset", 0) + 1,
                )
            )

    for path in REBUILD_MODULES:
        index = provider.tree_index(path)
        if index is None or index.root is None:
            findings.append(_summary(GUARD_SKILL_LAYOUT, path, "Rebuild module is missing"))
            continue
        for node in index.walk(index.root):
            if not isinstance(node, ast.Call) or _name(node.func) != _BLIND_RESOLVER:
                continue
            if (path, _enclosing(index, node)) in ALLOWLIST:
                continue
            findings.append(
                violation(
                    GUARD_SKILL_LAYOUT,
                    path,
                    "Rebuilding recorded deployments with layout-blind resolve_targets; use "
                    f"resolve_reintegration_targets or resolve_audit_targets ({_OWNER})",
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
    return tuple(findings)


__all__ = ["ALLOWLIST", "GUARD_SKILL_LAYOUT", "REBUILD_MODULES", "check_skill_layout"]
