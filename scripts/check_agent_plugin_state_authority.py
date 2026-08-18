#!/usr/bin/env python3
"""Reject split authority in installed Agent Plugin lifecycle state."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


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
