"""Mutation proof for each executable-contract authority branch."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.xdist_group(name="architecture_contract_compound_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "contracts-tests-executable-contract-authorities"


@dataclass(frozen=True)
class ContractMutation:
    name: str
    path: str
    old: str | None = None
    new: str | None = None
    append: str = ""


MUTATIONS: tuple[ContractMutation, ...] = (
    ContractMutation(
        "binary-selection",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            "\n\nimport os\n\n\ndef _rogue_binary_selection():\n"
            '    return os.environ.get("APM_BINARY_PATH")\n'
        ),
    ),
    ContractMutation(
        "rendered-parity-owner",
        "scripts/check_cli_docs.py",
        "def public_top_level_commands(",
        "def public_top_level_commands_disabled(",
    ),
    ContractMutation(
        "rendered-parity-consumer",
        "scripts/check_test_assertions.py",
        append=("\nfrom scripts.check_cli_docs import public_top_level_commands\n"),
    ),
    ContractMutation(
        "ratchet-consumer",
        "scripts/check_test_assertions.py",
        "from test_file_inventory import tracked_python_paths",
        "from test_file_inventory import tracked_paths",
    ),
    ContractMutation(
        "ratchet-local-inventory",
        "scripts/check_test_assertions.py",
        append=('\n\ndef _rogue_test_inventory(root):\n    return root.rglob("*.py")\n'),
    ),
)


def _mutate(case: ContractMutation) -> str:
    source = (ROOT / case.path).read_text(encoding="utf-8")
    if case.old is not None:
        assert case.new is not None
        assert source.count(case.old) >= 1
        source = source.replace(case.old, case.new, 1)
    source += case.append
    ast.parse(source, filename=case.path)
    return source


@pytest.mark.parametrize("case", MUTATIONS, ids=[case.name for case in MUTATIONS])
def test_each_executable_contract_branch_rejects_its_mutation(
    case: ContractMutation,
) -> None:
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={case.path: _mutate(case)},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
