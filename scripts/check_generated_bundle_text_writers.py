#!/usr/bin/env python3
"""Require generated bundle metadata to use the deterministic LF writer."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

EXPECTED_LF_WRITES = {
    "src/apm_cli/bundle/agent_plugin_exporter.py": 3,
    "src/apm_cli/bundle/packer.py": 1,
    "src/apm_cli/bundle/plugin_exporter.py": 4,
    "src/apm_cli/core/plugin_manifest.py": 1,
}


def _calls(source: str, attribute: str) -> list[ast.Call]:
    """Return calls whose target name or attribute matches ``attribute``."""
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == attribute)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == attribute)
        )
    ]


def check_generated_bundle_text_writers(root: Path) -> list[str]:
    """Return deterministic-writer policy violations below ``root``."""
    violations: list[str] = []
    for relative_path, expected_lf_writes in EXPECTED_LF_WRITES.items():
        source = (root / relative_path).read_text(encoding="utf-8")
        direct_writes = _calls(source, "write_text")
        lf_writes = _calls(source, "write_text_lf")
        if direct_writes:
            violations.append(f"{relative_path}: direct Path.write_text call found")
        if len(lf_writes) != expected_lf_writes:
            violations.append(
                f"{relative_path}: expected {expected_lf_writes} write_text_lf calls, "
                f"found {len(lf_writes)}"
            )
    return violations


def main() -> int:
    """Run the deterministic generated-bundle writer check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    violations = check_generated_bundle_text_writers(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    print("[+] generated bundle text writers use deterministic LF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
