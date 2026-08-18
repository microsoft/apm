#!/usr/bin/env python3
"""Reject split authority in installed Agent Plugin lifecycle state."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

_SOURCE_LOCATOR_VALIDATOR_FINGERPRINTS = {
    "_is_valid_plugin_source_hostname": (
        "9a687c1b947fd7c8d12c7a4bc997377de6c089a4b0e6adefdce81d5bded4754e"
    ),
    "_validate_standard_plugin_source_url": (
        "7def81b52676dd17767dddfa165519776ec56f537ba97ea7f5fbcd7cb2abe400"
    ),
    "_validate_git_scp_locator": (
        "5455e72b7ca9cd837162dd4d2dc486809473a34e3774da5870562855cc1686f1"
    ),
    "_validate_plugin_source_locator": (
        "a30b5b00c676420accdab9ba14f139a679ad242470a501250cdd0192b451db74"
    ),
}
_SOURCE_LOCATOR_SCP_GRAMMAR_FINGERPRINT = (
    "cd3aaf8e8c727260b0f7dba78cdb8076bb9c27935e196ffc864b01480beebce4"
)


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        ),
        None,
    )
    if node is None:
        return ""
    return ast.get_source_segment(source, node) or ""


def _assignment_source(source: str, name: str) -> str:
    """Return source for one named assignment."""
    tree = ast.parse(source)
    node = next(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in item.targets)
        ),
        None,
    )
    if node is None:
        return ""
    return ast.get_source_segment(source, node) or ""


def _function_node(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return one named function node."""
    tree = ast.parse(source)
    return next(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        ),
        None,
    )


def _function_fingerprint(source: str, name: str) -> str:
    """Return a Python-version-independent source fingerprint."""
    function_source = _function_source(source, name)
    if not function_source:
        return ""
    payload = function_source.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _has_exact_if_expression(node: ast.AST | None, expression: str) -> bool:
    """Return whether *node* contains an if with exactly *expression* as its test."""
    if node is None:
        return False
    expected = ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
    for item in ast.walk(node):
        if isinstance(item, ast.If) and (ast.dump(item.test, include_attributes=False) == expected):
            return True
    return False


def _function_statements(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    """Return function statements without the optional docstring."""
    statements = list(node.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements.pop(0)
    return statements


def _has_positive_locator_flow(node: ast.AST | None) -> bool:
    """Require URL, Git SCP, and malformed-prefix gates in executable order."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    statements = _function_statements(node)
    if len(statements) != 3 or not all(isinstance(item, ast.If) for item in statements):
        return False
    url_branch, git_branch, path_branch = statements
    expected_url = ast.dump(
        ast.parse(
            "_validate_standard_plugin_source_url(source_kind, locator)",
            mode="eval",
        ).body,
        include_attributes=False,
    )
    expected_git = ast.dump(
        ast.parse('source_kind == "git"', mode="eval").body,
        include_attributes=False,
    )
    expected_path = ast.dump(
        ast.parse(
            "_PLUGIN_URI_SCHEME_RE.match(locator) and not PureWindowsPath(locator).drive",
            mode="eval",
        ).body,
        include_attributes=False,
    )
    if ast.dump(url_branch.test, include_attributes=False) != expected_url:
        return False
    if len(url_branch.body) != 1 or not isinstance(url_branch.body[0], ast.Return):
        return False
    if ast.dump(git_branch.test, include_attributes=False) != expected_git:
        return False
    if len(git_branch.body) != 2 or not isinstance(git_branch.body[1], ast.Return):
        return False
    scp_call = git_branch.body[0]
    if not (
        isinstance(scp_call, ast.Expr)
        and isinstance(scp_call.value, ast.Call)
        and isinstance(scp_call.value.func, ast.Name)
        and scp_call.value.func.id == "_validate_git_scp_locator"
    ):
        return False
    return ast.dump(path_branch.test, include_attributes=False) == expected_path


def _definition_paths(root: Path, *, class_name: str = "", function_name: str = "") -> list[str]:
    """Return source paths defining one guarded class or function."""
    paths: list[str] = []
    for path in sorted((root / "src/apm_cli").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if class_name and isinstance(node, ast.ClassDef) and node.name == class_name:
                paths.append(path.relative_to(root).as_posix())
            if function_name and isinstance(node, ast.FunctionDef) and node.name == function_name:
                paths.append(path.relative_to(root).as_posix())
    return paths


def main() -> int:
    """Validate the canonical identity, record, root, and ledger owners."""
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parents[1]
    lockfile = (root / "src/apm_cli/deps/lockfile.py").read_text(encoding="utf-8")
    runtime = (root / "src/apm_cli/install/agent_plugin_runtime.py").read_text(encoding="utf-8")
    state = (root / "src/apm_cli/install/agent_plugin_state.py").read_text(encoding="utf-8")
    ledger = (root / "src/apm_cli/core/deployment_ledger.py").read_text(encoding="utf-8")
    violations: list[str] = []

    if _definition_paths(root, class_name="InstalledPluginRecord") != [
        "src/apm_cli/deps/lockfile.py"
    ] or _definition_paths(root, class_name="InstalledPluginRecordCodec") != [
        "src/apm_cli/deps/lockfile.py"
    ]:
        violations.append("installed plugin record and codec must have one lockfile owner")
    if _definition_paths(root, function_name="storage_key") != ["src/apm_cli/deps/lockfile.py"]:
        violations.append("installed plugin storage identity must have one lockfile owner")
    if runtime.count("plugin = load_agent_plugin(Path(bundle_info.source_dir))") != 1:
        violations.append("runtime storage identity must come from canonical AgentPlugin IR")
    if "source_identity" in runtime or "package_id" in runtime:
        violations.append("runtime storage identity must never use source or basename identity")
    if "Path(bundle_info.source_dir).name" in runtime or "bundle_info.source_dir.name" in runtime:
        violations.append("runtime storage identity must never use a source basename")
    if "def root_values(" not in lockfile or (
        "InstalledPluginRecordCodec.root_values(identity.name, scope)" not in state
    ):
        violations.append("PLUGIN_ROOT and PLUGIN_DATA must route through the lockfile codec")
    source_locator_node = _function_node(lockfile, "_validate_plugin_source_locator")
    source_url_node = _function_node(lockfile, "_validate_standard_plugin_source_url")
    source_scp_node = _function_node(lockfile, "_validate_git_scp_locator")
    if any(
        _function_fingerprint(lockfile, name) != expected
        for name, expected in _SOURCE_LOCATOR_VALIDATOR_FINGERPRINTS.items()
    ):
        violations.append("installed plugin source locator validators must match guarded grammar")
    scp_grammar = _assignment_source(lockfile, "_PLUGIN_GIT_SCP_RE")
    if (
        hashlib.sha256(scp_grammar.encode("utf-8")).hexdigest()
        != _SOURCE_LOCATOR_SCP_GRAMMAR_FINGERPRINT
    ):
        violations.append("installed plugin SCP path grammar must match guarded syntax")
    if not _has_positive_locator_flow(source_locator_node):
        violations.append("installed plugin source locators must route through positive grammar")
    if (
        not _has_exact_if_expression(source_url_node, "parsed.password is not None")
        or not _has_exact_if_expression(source_url_node, 'parsed.username != "git"')
        or not _has_exact_if_expression(source_url_node, 'source_kind != "git"')
    ):
        violations.append("installed plugin URL locators must reject credential userinfo")
    if not _has_exact_if_expression(source_scp_node, 'username != "git"'):
        violations.append("installed plugin SCP locators must allow only canonical git login")

    replacement = _function_source(ledger, "prepare_owner_replacement")
    if "records.pop(key, None)" not in replacement or ".union(" in replacement:
        violations.append("native plugin ledger ownership must replace, never union")
    if "active_prior.active_owner != owner" not in replacement or (
        "actively owned by an unrelated owner" not in replacement
    ):
        violations.append("native plugin ledger ownership must reject unrelated takeover")
    if 'if "installed_plugins" in data:' not in lockfile or (
        'InstalledPluginRecordCodec.validate_rows(data["installed_plugins"])' not in lockfile
    ):
        violations.append("malformed installed plugin lock state must fail closed")
    required_version = _function_source(lockfile, "_required_lockfile_version")
    if "if self.installed_plugins:" not in required_version or 'return "3"' not in required_version:
        violations.append("installed plugin lifecycle state must require lockfile version 3")

    projection = _function_source(state, "project_installed_plugin_record")
    if "plugin = validation.agent_plugin" not in projection or (
        "package.agent_plugin is not plugin" not in projection
    ):
        violations.append("installed state projection must retain canonical ValidationResult IR")
    if "read_text(" in projection or "load_agent_plugin(" in projection:
        violations.append("installed state projection must not re-read plugin manifests")
    if "InstalledPluginRecordCodec.build(" not in projection:
        violations.append("installed state construction must route through the lockfile codec")
    managed_path = _function_source(state, "_managed_path")
    if "resolved_candidate = ensure_path_within(" not in managed_path or (
        "current = resolved_candidate" not in managed_path
    ):
        violations.append("managed plugin paths must walk canonical contained ancestors")

    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    print("[+] Agent Plugin lifecycle state authority clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
