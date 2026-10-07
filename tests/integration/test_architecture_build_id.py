"""Architecture guard for the Build ID implementation owner."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.inventory import build_inventory
from scripts.architecture_linter.registry import load_registry
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).parents[2]
OWNER = ROOT / "src/apm_cli/compilation/build_id.py"
CONSUMER = ROOT / "src/apm_cli/compilation/agents_compiler.py"
OWNER_REL = "src/apm_cli/compilation/build_id.py"
CONSUMER_REL = "src/apm_cli/compilation/agents_compiler.py"
RULE_ID = "registry_delegation.build_id_verification"


def test_compiler_routes_build_id_creation_and_verification_to_owner() -> None:
    owner = ast.parse(OWNER.read_text(encoding="utf-8"))
    consumer = ast.parse(CONSUMER.read_text(encoding="utf-8"))

    owner_functions = {
        node.name
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"stabilize_build_id", "has_valid_build_id", "has_build_id_line"} <= owner_functions

    imported = {
        alias.name
        for node in consumer.body
        if isinstance(node, ast.ImportFrom) and node.module == "build_id"
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(consumer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"stabilize_build_id", "has_valid_build_id", "has_build_id_line"} <= imported
    assert {"stabilize_build_id", "has_valid_build_id", "has_build_id_line"} <= called
    assert "_has_valid_build_id" not in {
        node.name for node in ast.walk(consumer) if isinstance(node, ast.FunctionDef)
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hashlib"
        and node.func.attr == "sha256"
        for node in ast.walk(consumer)
    )


def test_build_id_owner_and_semantic_guard_are_registered() -> None:
    inventory = build_inventory(ROOT)
    registry = load_registry(ROOT / ".apm/architecture/owners", inventory.files)
    owner = next(entry for entry in registry.owners if entry.id == "build-id-content-verification")

    assert owner.selectors == (OWNER_REL,)
    assert owner.guards == ("registry-delegation-build-id-verification",)
    assert RULE_ID in {rule.id for rule in registered_rules()}


def test_build_id_guard_rejects_parallel_hashing() -> None:
    source = CONSUMER.read_text(encoding="utf-8")
    mutated = source.replace(
        "if has_build_id_line(existing) and not has_valid_build_id(existing):",
        "if has_build_id_line(existing) and not has_valid_build_id(existing):\n"
        "                hashlib.sha256(b'parallel').hexdigest()",
    )
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={CONSUMER_REL: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID
        and violation.path == CONSUMER_REL
        and "must not duplicate Build ID parsing or hashing" in violation.message
        for violation in report.violations
    )
