"""Mutation proof for each executable-contract authority branch."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
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
    # Split or propagated forms: the binary-selection pre-filter (#5196) must still
    # route each to its detector, so a filter narrower than the detector fails here.
    ContractMutation(
        "binary-selection-implicit-concat",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            "\n\nimport os as _os\n\n\ndef _rogue_binary_selection():\n"
            '    return _os.environ["APM_BINARY" "_PATH"]\n'
        ),
    ),
    ContractMutation(
        "binary-selection-aliased-which",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            "\n\nfrom shutil import which as _locate\n\n\ndef _rogue_binary_selection():\n"
            '    return _locate("ap" "m")\n'
        ),
    ),
    ContractMutation(
        "binary-selection-propagated-venv",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            '\n\nfrom pathlib import Path as _P\n\n_ENV = ".v" "env"\n\n\n'
            "def _rogue_binary_selection():\n"
            '    folder = _ENV\n    return _P(folder) / "bin" / "apm"\n'
        ),
    ),
    ContractMutation(
        "binary-selection-interpreter-sibling",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            "\n\nimport sys as _s\nfrom pathlib import Path as _P\n\n\n"
            "def _rogue_binary_selection():\n"
            '    return _P(_s.executable).with_name("apm")\n'
        ),
    ),
    ContractMutation(
        "binary-selection-runner-module-alias",
        "tests/integration/test_architecture_apply_to_patterns.py",
        append=(
            "\n\nimport tests.utils.apm_lifecycle_runner as _lr\n\n\n"
            "def _rogue_binary_selection():\n    return _lr.ApmLifecycleRunner()\n"
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
