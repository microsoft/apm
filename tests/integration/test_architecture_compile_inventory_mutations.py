"""Mutation proof for each compile nested-repository ownership consumer."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.xdist_group(name="architecture_compile_inventory_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "registry_delegation.compile_inventory_authority"


@dataclass(frozen=True)
class CompileMutation:
    name: str
    path: str
    old: str
    new: str


MUTATIONS: tuple[CompileMutation, ...] = (
    CompileMutation(
        "inventory-nested-boundary",
        "src/apm_cli/compilation/inventory.py",
        'if path != root and (".git" in file_names or ".git" in child_dirs):',
        "if False:",
    ),
    CompileMutation(
        "agents-nested-boundary",
        "src/apm_cli/compilation/agents_compiler.py",
        "nested_root = deploy_inventory.nested_repository_root_for(agents_path.parent)",
        "nested_root = None",
    ),
    CompileMutation(
        "distributed-nested-boundary",
        "src/apm_cli/compilation/distributed_compiler.py",
        "if deploy_inventory.nested_repository_root_for(directory_path) is not None:",
        "if False:",
    ),
    CompileMutation(
        "discovery-inventory-collection",
        "src/apm_cli/primitives/discovery.py",
        "inventory = CompileInventory.collect(base_path, exclude_patterns=exclude_patterns)",
        "inventory = None",
    ),
    CompileMutation(
        "discovery-nested-boundary",
        "src/apm_cli/primitives/discovery.py",
        "if inventory is not None and inventory.nested_repository_root_for(directory) is not None:",
        "if False:",
    ),
)


def _mutate(case: CompileMutation) -> str:
    """Return one syntactically valid, surgical compile-boundary mutation."""
    source = (ROOT / case.path).read_text(encoding="utf-8")
    assert source.count(case.old) == 1
    mutated = source.replace(case.old, case.new, 1)
    ast.parse(mutated, filename=case.path)
    return mutated


@pytest.mark.parametrize("case", MUTATIONS, ids=[case.name for case in MUTATIONS])
def test_compile_inventory_rule_rejects_boundary_mutation(case: CompileMutation) -> None:
    """Every consumer must continue routing nested-repository decisions."""
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={case.path: _mutate(case)},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
