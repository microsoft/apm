"""Skills-root derivation analyzer.

``TargetProfile.skills_rel_root`` / ``TargetProfile.skills_deploy_path`` in
``src/apm_cli/integration/targets.py`` own where skills land. Install and
integration code must consume them instead of re-deriving ``<root>/skills``.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _python_paths,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import Violation

_GUARD_SKILLS_ROOT = "install-deployment-skills-root-derivation"
_SCAN_PREFIXES = (f"{_SRC_PREFIX}integration/", f"{_SRC_PREFIX}install/")
_OWNER = "src/apm_cli/integration/targets.py"

_ROOT_NAMES = frozenset(
    {
        "root_dir",
        "deploy_root",
        "target_root",
        "target_base",
        "project_root",
        "user_root",
        "target_dir",
    }
)
# ``.apm/skills`` is package source layout, not a deployed skills root.
_SOURCE_DIRS = frozenset({".apm"})
_FSTRING_SKILLS = re.compile(r"/skills(/|$)")
_PATH_CALLS = frozenset({"Path", "PurePath", "PurePosixPath", "os.path.join", "posixpath.join"})

# (path, enclosing function qualname) -> why this is not a skills-root derivation.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "src/apm_cli/integration/skill_integrator.py",
        "SkillIntegrator._integrate_native_skill",
    ): "counts sub-skills against the primary Copilot root; not a per-target derivation",
    (
        "src/apm_cli/install/deployed_paths.py",
        "skill_summary_paths",
    ): "display labels and fallback for external roots that have no TargetProfile root",
    (
        "src/apm_cli/install/skill_path_migration.py",
        "detect_legacy_skill_deployments",
    ): "matches legacy lockfile prefixes and the fixed .agents convergence destination",
    (
        "src/apm_cli/install/template.py",
        "_agent_plugin_target_skip_message",
    ): "builds a remote dependency reference, not a filesystem path",
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_skills(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "skills"


def _deploy_rooted(index: TreeIndex, node: ast.AST) -> bool:
    """Return whether `node` names a target root or a hidden-directory literal."""
    for item in index.walk(node):
        if isinstance(item, ast.Name) and item.id in _ROOT_NAMES:
            return True
        if isinstance(item, ast.Attribute) and item.attr in _ROOT_NAMES:
            return True
        if (
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and item.value.startswith(".")
            and item.value not in _SOURCE_DIRS
            and len(item.value) > 1
        ):
            return True
    return False


def _derivation(index: TreeIndex, node: ast.AST) -> bool:
    """Return whether `node` open-codes a ``<root>/skills`` path."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_skills(node.right) and _deploy_rooted(index, node.left)
    if isinstance(node, ast.Call) and _dotted(node.func) in _PATH_CALLS:
        for position, arg in enumerate(node.args):
            if position and _is_skills(arg):
                return any(_deploy_rooted(index, earlier) for earlier in node.args[:position])
        return False
    if isinstance(node, ast.JoinedStr):
        has_value = any(isinstance(part, ast.FormattedValue) for part in node.values)
        return has_value and any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and _FSTRING_SKILLS.search(part.value)
            for part in node.values
        )
    return False


def _symbol(index: TreeIndex, node: ast.AST) -> str:
    anchor = index.definition_anchor(node)
    if anchor is None:
        return ""
    try:
        return index.function_qualname(anchor)
    except RuntimeError:
        return getattr(anchor, "name", "")


def check_skills_root_derivation(provider: FactsProvider) -> tuple[Violation, ...]:
    """Skills roots come from TargetProfile, not open-coded ``<root>/skills``."""
    rule_id = _GUARD_SKILLS_ROOT
    findings: list[Violation] = []
    for prefix in _SCAN_PREFIXES:
        for path in _python_paths(provider, prefix):
            if path == _OWNER:
                continue
            facts = provider.file_facts(path)
            index = provider.tree_index(path)
            if index is None or index.root is None:
                continue
            lines = getattr(facts, "lines", ())
            for node in index.walk(index.root):
                if not _derivation(index, node):
                    continue
                if (path, _symbol(index, node)) in ALLOWLIST:
                    continue
                lineno = getattr(node, "lineno", 1)
                if 0 < lineno <= len(lines) and EXEMPT_MARKER in lines[lineno - 1]:
                    continue
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "Open-coded '<root>/skills' derivation; use "
                        "TargetProfile.skills_rel_root or skills_deploy_path "
                        f"({_OWNER})",
                        line=lineno,
                        column=getattr(node, "col_offset", 0) + 1,
                    )
                )
    return tuple(findings)


__all__ = ["ALLOWLIST", "_GUARD_SKILLS_ROOT", "check_skills_root_derivation"]
