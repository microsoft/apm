"""Verify compile traversal is owned by the shared inventory."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
INVENTORY = ROOT / "src/apm_cli/compilation/inventory.py"
OPTIMIZER = ROOT / "src/apm_cli/compilation/context_optimizer.py"
DISCOVERY = ROOT / "src/apm_cli/primitives/discovery.py"
DISTRIBUTED = ROOT / "src/apm_cli/compilation/distributed_compiler.py"
AGENTS = ROOT / "src/apm_cli/compilation/agents_compiler.py"


def _has_all(source: str, required: tuple[str, ...]) -> bool:
    """Return whether every required contract fragment appears in source."""
    return all(fragment in source for fragment in required)


def main() -> int:
    """Return nonzero when compile traversal has a duplicate authority."""
    inventory = INVENTORY.read_text(encoding="utf-8")
    optimizer = OPTIMIZER.read_text(encoding="utf-8")
    discovery = DISCOVERY.read_text(encoding="utf-8")
    distributed = DISTRIBUTED.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")

    valid = (
        inventory.count("class CompileInventory") == 1
        and inventory.count("os.walk(") == 1
        and "os.walk(" not in optimizer
        and "os.walk(" not in distributed
        and _has_all(
            optimizer,
            (
                "from .inventory import CompileInventory",
                "inventory = self._inventory or CompileInventory.collect(self.base_dir)",
                "inventory.files_under(self._scan_top_level_roots)",
            ),
        )
        and _has_all(
            discovery,
            (
                "inventory: CompileInventory | None = None",
                "inventory.files_within(base_path)",
            ),
        )
        and _has_all(
            distributed,
            (
                "source_inventory: CompileInventory | None = None",
                "deploy_inventory: CompileInventory | None = None",
                '(entry.path / ".git").is_file()',
                "relative_path.is_relative_to(worktree_root)",
                "for directory_path, (relative_path, files) in sorted(cleanup_directories.items()):",
            ),
        )
        and _has_all(
            agents,
            (
                "self._source_inventory = CompileInventory.collect(",
                "self.source_dir, exclude_patterns=config.exclude",
                "source_inventory=self._source_inventory",
                "deploy_inventory=self._deploy_inventory",
            ),
        )
    )
    if valid:
        return 0

    print("[x] Compile traversal must route through compilation/inventory.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
