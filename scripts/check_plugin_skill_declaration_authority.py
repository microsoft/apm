"""Enforce parser-owned membership for legacy plugin skill deployment."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )
    if function is None:
        return ""
    return ast.get_source_segment(source, function) or ""


def main(root: Path) -> int:
    parser = root / "src/apm_cli/deps/plugin_parser.py"
    integrator = root / "src/apm_cli/integration/skill_integrator.py"
    parser_source = parser.read_text(encoding="utf-8")
    routing_source = _function_source(integrator, "skill_source_paths")

    valid = (
        parser_source.count("def normalized_plugin_skill_sources(") == 1
        and parser_source.count("def _map_plugin_artifacts(") == 1
        and "normalized_plugin_skill_sources(package_path)" in routing_source
        and 'package_path / "skills"' not in routing_source
        and "manifest.get(" not in routing_source
    )
    if valid:
        return 0
    print(
        "[x] Plugin skill declaration membership must remain owned by "
        "plugin_parser._map_plugin_artifacts"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) == 2 else Path.cwd()))
