"""Build one tracked Python AST inventory for quality topology tests."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scripts.test_file_inventory import tracked_python_paths

SCOPE_MARKER = "RATCHET_TEST_SCOPE"
INVENTORY_CALLS: Counter[Path] = Counter()


@dataclass(frozen=True)
class PythonModuleFacts:
    """Static facts consumed by bounded quality ownership checks."""

    string_literals: frozenset[str]
    ratchet_scope: str | None


def tracked_python_inventory(root: Path) -> dict[str, PythonModuleFacts]:
    """Parse every tracked Python file once without caching across roots."""
    root = root.resolve()
    INVENTORY_CALLS[root] += 1

    inventory: dict[str, PythonModuleFacts] = {}
    for path in tracked_python_paths(root):
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=relative_text,
        )
        scope_values = {
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == SCOPE_MARKER
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        if len(scope_values) > 1:
            raise RuntimeError(
                f"multiple {SCOPE_MARKER} values in {relative_text}: {sorted(scope_values)}"
            )
        inventory[relative.as_posix()] = PythonModuleFacts(
            string_literals=frozenset(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ),
            ratchet_scope=next(iter(scope_values), None),
        )
    return inventory
