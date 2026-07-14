"""Semantic ownership and least-privilege contracts for the docs workflow."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    assert_unconditional,
    load_workflow,
    shell_tokens,
    workflow_job,
    workflow_step,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"
UNIT_CONTRACT = "tests/unit/test_cli_docs_contract.py"
SUBPROCESS_CONTRACT = "tests/integration/test_cli_docs_contract.py"


def _workflow() -> dict:
    return load_workflow(WORKFLOW)


def _assert_checker_test_step(workflow: dict) -> None:
    build = workflow_job(workflow, "build")
    step = workflow_step(build, "Test CLI registry contract")
    assert_unconditional(build, label="docs build job")
    assert_unconditional(step, label="CLI registry contract step")
    tokens = shell_tokens(step)
    assert tokens[:4] == ["uv", "run", "--frozen", "pytest"]
    assert UNIT_CONTRACT in tokens
    assert SUBPROCESS_CONTRACT in tokens


def test_docs_contract_runs_for_every_cli_registration_owner() -> None:
    """Command names and visibility cannot change without scheduling parity."""
    workflow = _workflow()
    triggers = workflow["on"]["pull_request"]["paths"]

    assert set(triggers) >= {
        "docs/**",
        "src/apm_cli/cli.py",
        "src/apm_cli/commands/**",
        "scripts/check_cli_docs.py",
        UNIT_CONTRACT,
        SUBPROCESS_CONTRACT,
    }


def test_docs_build_has_read_only_permissions() -> None:
    """Pages write and OIDC minting belong only to the deploy job."""
    workflow = _workflow()
    build = workflow_job(workflow, "build")
    deploy = workflow_job(workflow, "deploy")

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in build
    assert deploy["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }


def test_docs_python_operations_are_frozen() -> None:
    """The docs owner must consume the reviewed lock without mutation."""
    workflow = _workflow()
    build = workflow_job(workflow, "build")
    install = workflow_step(build, "Install Python test dependencies")
    checker = workflow_step(build, "Check CLI registry against rendered pages")

    assert shell_tokens(install) == ["uv", "sync", "--frozen", "--extra", "dev"]
    _assert_checker_test_step(workflow)
    assert shell_tokens(checker) == [
        "uv",
        "run",
        "--frozen",
        "python",
        "scripts/check_cli_docs.py",
        "docs/dist",
    ]


@pytest.mark.parametrize("mutation", ("step-disabled", "unit-omitted", "subprocess-commented"))
def test_docs_checker_step_mutations_are_rejected(mutation: str) -> None:
    """Disabled, omitted, or commented contract execution must fail."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build"), "Test CLI registry contract")
    if mutation == "step-disabled":
        step["if"] = False
    elif mutation == "unit-omitted":
        step["run"] = step["run"].replace(UNIT_CONTRACT, "")
    else:
        step["run"] = step["run"].replace(
            SUBPROCESS_CONTRACT,
            f"# {SUBPROCESS_CONTRACT}",
        )

    with pytest.raises(AssertionError):
        _assert_checker_test_step(workflow)
