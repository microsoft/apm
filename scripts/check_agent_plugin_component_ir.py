#!/usr/bin/env python3
"""Static boundary check for the canonical Agent Plugin component IR."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_PORTABLE_FIELDS = ("skills", "mcp_servers")


def _module(root: Path, relative: str) -> tuple[str, ast.Module]:
    source = (root / relative).read_text(encoding="utf-8")
    return source, ast.parse(source)


def _class_fields(tree: ast.Module, name: str) -> tuple[str, ...]:
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name
    )
    return tuple(
        node.target.id
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _has_call(tree: ast.AST, owner: str, method: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner
        and node.func.attr == method
        for node in ast.walk(tree)
    )


def check(root: Path) -> list[str]:
    _ir_source, ir_tree = _module(root, "src/apm_cli/agent_plugins/ir.py")
    loader_source, loader_tree = _module(root, "src/apm_cli/agent_plugins/loader.py")
    assets_source, assets_tree = _module(root, "src/apm_cli/agent_plugins/assets.py")
    validation_source, _validation_tree = _module(root, "src/apm_cli/models/validation.py")
    sources_source, _sources_tree = _module(root, "src/apm_cli/install/sources.py")
    hook_contract_source, _hook_contract_tree = _module(root, "src/apm_cli/hook_contract.py")
    hook_ir_source, _hook_ir_tree = _module(root, "src/apm_cli/integration/hook_ir.py")
    _projection_source, projection_tree = _module(
        root,
        "src/apm_cli/agent_plugins/projection.py",
    )
    failures: list[str] = []

    if _class_fields(ir_tree, "AgentPluginComponents") != _PORTABLE_FIELDS:
        failures.append("portable AgentPluginComponents fields changed")
    if _class_fields(ir_tree, "AgentPluginAsset") != (
        "path",
        "source",
        "sha256",
        "size",
        "executable_mode",
    ):
        failures.append("asset integrity facts changed")
    if "if stat.S_ISLNK" not in assets_source or not _has_call(
        assets_tree,
        "hashlib",
        "sha256",
    ):
        failures.append("asset symlink or digest enforcement changed")
    if "ensure_path_within" not in assets_source:
        failures.append("asset containment enforcement changed")
    if (
        "entry_count = self._entry_count" in assets_source
        or "set(self._assets)" in assets_source
        or "self._reserve_bytes(len(chunk))" not in assets_source
        or "for entry in directory.iterdir():" not in assets_source
        or "self._reserve_entry()\n                entries.append(entry)" not in assets_source
        or "sorted(directory.iterdir()" in assets_source
    ):
        failures.append("attempted inventory work may be refunded or scale quadratically")
    if "root_entries = asset_inventory.list_component_candidates(root)" not in loader_source or any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "iterdir"
        for node in ast.walk(_function(loader_tree, "_has_exact_entry"))
    ):
        failures.append("component candidates may be rescanned outside the package work budget")
    if (
        "cached = self._assets.get(relative)" not in assets_source
        or "ensure_path_within_resolved(path, self._root)" not in assets_source
        or "ensure_path_within_resolved(path, root)" not in assets_source
        or "cached_payload = self._payloads.get(relative)" not in assets_source
    ):
        failures.append("asset cache or pre-resolved containment fast path changed")
    if (
        "HOOK_COMMAND_KEYS: tuple[str, ...]" not in hook_contract_source
        or "HOOK_COMMAND_KEYS: tuple[str, ...]" in hook_ir_source
    ):
        failures.append("neutral hook command grammar must stay owned by hook_contract")
    if (
        "primary.disposition is _CandidateDisposition.ABSENT" not in loader_source
        or "disposition=_CandidateDisposition.REJECTED" not in loader_source
    ):
        failures.append("rejected hook-relative candidates may fall through to root fallback")
    forbidden_scan_methods = {"glob", "iterdir", "read_bytes", "read_text", "rglob"}
    if any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_scan_methods
        for node in ast.walk(projection_tree)
    ):
        failures.append("projection rescans source files instead of consuming IR")
    if (
        "agent_plugin_detection: AgentPluginDetection | None = None" not in validation_source
        or "result.agent_plugin = plugin" not in validation_source
        or "agent_plugin_detection=native_detection" not in sources_source
        or "detection.manifest_path.parent.resolve() != package_root" not in validation_source
    ):
        failures.append("same-root detection reuse or cross-root rejection changed")
    return failures


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parents[1]
    failures = check(root)
    for failure in failures:
        print(f"[x] {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
