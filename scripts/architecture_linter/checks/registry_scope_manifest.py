"""Scope manifest path guard of the ``apm-home-resolution`` owner (#4087).

``core/scope.py`` is the single answer to "which ``apm.yml`` is the manifest"
for a directory and for a command scope. At user scope (``-g``) the deploy
root is ``$HOME`` but the manifest, its ``policy:`` settings and its
``executables`` grants live in ``$APM_HOME``; user scope has no git remote.

======================================  ==================================
Owner guard                             What it rejects
======================================  ==================================
registry-delegation-scope-manifest-     * any ``apm.yml`` path derivation
path                                      (``x / "apm.yml"``,
                                          ``x / APM_YML_FILENAME``,
                                          ``Path("apm.yml")``,
                                          ``joinpath``/``os.path.join``)
                                          outside core/scope.py;
                                        * a second definer of the owner
                                          helpers, or a revived
                                          root-to-manifest sibling;
                                        * policy readers handed the deploy
                                          root (``get_deploy_root(...)`` or
                                          ``Path.home()``) instead of the
                                          scope manifest root;
                                        * a -g reachable policy consumer
                                          that stops naming the scope
                                          manifest root;
                                        * git-remote policy discovery that
                                          bypasses the one user-scope
                                          decision in the owner.
======================================  ==================================

Facts-only: no rule reads a file or parses source itself.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import (
    _SRC,
    GROUP,
    _python_paths,
    _read_required,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "registry_delegation.scope_manifest_path"
GUARD_ID = "registry-delegation-scope-manifest-path"

_OWNER = "src/apm_cli/core/scope.py"
_DISCOVERY = "src/apm_cli/policy/discovery.py"
_MANIFEST_NAMES = frozenset({"apm.yml"})
_MANIFEST_CONSTANT = "APM_YML_FILENAME"
_OWNER_HELPERS = (
    "manifest_file",
    "manifest_root",
    "scope_manifest_path",
    "is_user_scope_root",
    "policy_discovery_uses_git_remote",
    "get_manifest_path",
)
# Root-to-manifest helpers that once lived beside the owner; they must not return.
_RETIRED_SIBLINGS = frozenset({"project_manifest_path"})
# Readers that take the scope manifest root as their first argument.
_POLICY_READERS = frozenset(
    {
        "discover_policy_with_chain",
        "discover_policy",
        "discover_trust_policy",
        "classify_project_policy",
        "resolve_trust_policy",
        "read_project_fetch_failure_default",
        "read_project_policy_hash_pin",
        "run_policy_preflight",
        "run_mcp_policy_preflight",
        "admit_bundle",
    }
)
_POLICY_ROOT_KEYWORDS = frozenset({"project_root", "policy_root"})
_DISCOVERY_SKIP = "if not policy_discovery_uses_git_remote(project_root):"
# Policy consumers reachable under -g hand policy the scope manifest root.
_SCOPE_ROOT_CONSUMERS: dict[str, tuple[str, ...]] = {
    "src/apm_cli/install/phases/policy_gate.py": (
        "return read_project_fetch_failure_default(_policy_root(ctx))",
        "return discover_policy_with_chain(_policy_root(ctx))",
        (
            "return manifest_root(ctx.project_root, "
            'user_scope=is_user_scope(getattr(ctx, "scope", None)))'
        ),
    ),
    "src/apm_cli/commands/update.py": (
        "manifest_root(get_deploy_root(scope), user_scope=scope is _Scope.USER)",
    ),
    "src/apm_cli/commands/uninstall/cli.py": (
        "project_root=str(manifest_root(deploy_root, user_scope=is_user_scope(scope))),",
    ),
    "src/apm_cli/commands/install.py": (
        "policy_root=_scope_manifest_root(Path.cwd(), user_scope=scope is _InstallScope.USER),",
        "_scope_manifest_root(_bundle_project_root, user_scope=global_)",
    ),
    "src/apm_cli/install/commit_gate.py": (
        "project_root=manifest_root(project_root, user_scope=user_scope),",
    ),
    "src/apm_cli/install/local_bundle_handler.py": (
        "policy_root=manifest_root(source_root, user_scope=global_),",
    ),
    "src/apm_cli/security/trust.py": ("route = classify_project_policy(policy_root)",),
    "src/apm_cli/commands/policy.py": (
        "project_root = manifest_root(Path.cwd(), user_scope=global_)",
    ),
}


def _is_manifest_name(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in _MANIFEST_NAMES
    if isinstance(node, ast.Name):
        return node.id == _MANIFEST_CONSTANT
    return isinstance(node, ast.Attribute) and node.attr == _MANIFEST_CONSTANT


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _derivation(node: ast.AST) -> str | None:
    """Describe an ad hoc manifest-path derivation at *node*, if any."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if _is_manifest_name(node.right):
            return "joins apm.yml onto a directory"
        return None
    if not isinstance(node, ast.Call):
        return None
    name = _callee(node)
    if name in {"Path", "PurePath", "PosixPath", "WindowsPath"} and any(
        _is_manifest_name(arg) for arg in node.args
    ):
        return "builds a cwd-relative apm.yml path"
    if name in {"joinpath", "join"} and any(_is_manifest_name(arg) for arg in node.args):
        return "joins apm.yml onto a directory"
    return None


def _is_deploy_root(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _callee(node)
    if name == "get_deploy_root":
        return True
    return (
        name == "home"
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Path"
    )


def _policy_root_argument(node: ast.Call) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg in _POLICY_ROOT_KEYWORDS:
            return keyword.value
    return node.args[0] if node.args else None


def _owner_findings(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    facts_by_path, failures = _read_required(
        provider, RULE_ID, (_OWNER, _DISCOVERY, *_SCOPE_ROOT_CONSUMERS)
    )
    if failures:
        return list(failures)
    for path, needles in _SCOPE_ROOT_CONSUMERS.items():
        text = source_text(facts_by_path[path])
        findings.extend(
            violation(
                RULE_ID,
                path,
                "policy must read the scope manifest root (core.scope.manifest_root); "
                f"missing: {needle}",
            )
            for needle in needles
            if needle not in text
        )
    owner = facts_by_path[_OWNER]
    defined = {d.name for d in owner.definitions if d.kind == "function"}
    findings.extend(
        violation(RULE_ID, _OWNER, f"core/scope.py must define the scope manifest owner {name}")
        for name in _OWNER_HELPERS
        if name not in defined
    )
    if _DISCOVERY_SKIP not in source_text(facts_by_path[_DISCOVERY]):
        findings.append(
            violation(
                RULE_ID,
                _DISCOVERY,
                "git-remote policy discovery must ask the scope owner "
                "(core.scope.policy_discovery_uses_git_remote) before reading a remote",
            )
        )
    return findings


def _file_findings(provider: FactsProvider, path: str) -> list[Violation]:
    _facts, read_failures = checked_facts(provider, path, RULE_ID, require_python=True)
    if read_failures:
        return list(read_failures)
    facts = provider.file_facts(path)
    findings: list[Violation] = []
    if path != _OWNER:
        findings.extend(
            violation(
                RULE_ID,
                path,
                f"{d.name} must be defined only by core/scope.py (scope manifest owner)",
                line=d.line,
            )
            for d in facts.definitions
            if d.kind == "function" and d.name in _OWNER_HELPERS
        )
    findings.extend(
        violation(
            RULE_ID,
            path,
            f"{d.name} re-derives the scope manifest from a root; use "
            "core.scope.scope_manifest_path",
            line=d.line,
        )
        for d in facts.definitions
        if d.kind == "function" and d.name in _RETIRED_SIBLINGS
    )
    index = provider.tree_index(path)
    if index is None:
        return findings
    for node in index.nodes:
        if path != _OWNER:
            reason = _derivation(node)
            if reason is not None:
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        f"{reason}; route manifest paths through core.scope "
                        "(manifest_file / get_manifest_path / scope_manifest_path)",
                        line=getattr(node, "lineno", 1),
                        column=getattr(node, "col_offset", 0) + 1,
                    )
                )
        if (
            isinstance(node, ast.Call)
            and _callee(node) in _POLICY_READERS
            and _is_deploy_root(_policy_root_argument(node))
        ):
            findings.append(
                violation(
                    RULE_ID,
                    path,
                    f"{_callee(node)} is handed the deploy root; pass the scope manifest "
                    "root (core.scope.manifest_root), which is $APM_HOME at user scope",
                    line=getattr(node, "lineno", 1),
                    column=getattr(node, "col_offset", 0) + 1,
                )
            )
    return findings


def _check_scope_manifest_path(provider: FactsProvider) -> Iterable[Violation]:
    """Every scope manifest path routes through the core/scope.py owner."""
    findings = _owner_findings(provider)
    for path in _python_paths(provider, under=_SRC):
        findings.extend(_file_findings(provider, path))
    return findings


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(GUARD_ID,),
        description=(
            "apm.yml paths and the scope manifest root (APM_HOME at user scope) must "
            "route through core/scope.py; policy readers never take the deploy root."
        ),
        check=_check_scope_manifest_path,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
