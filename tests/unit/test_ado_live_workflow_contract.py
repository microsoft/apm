"""Contracts separating live ADO acceptance from release publication."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.integration import test_ado_e2e
from tests.workflow_contracts import (
    assert_exact_command,
    load_workflow,
    shell_commands,
    shell_tokens,
    workflow_job,
    workflow_step,
)

ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-platform.yml"
AUTH_WORKFLOW = ROOT / ".github" / "workflows" / "auth-acceptance.yml"
INTEGRATION_SCRIPT = ROOT / "scripts" / "test-integration.sh"
LIVE_ADO_SELECTOR = "live and requires_ado_pat"
RELEASE_INTEGRATION_JOBS = ("integration-tests-shard",)


def _walk_nodes(value: Any) -> list[dict[str, Any]]:
    """Return every mapping nested under a parsed workflow."""
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_walk_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_walk_nodes(child))
    return nodes


def _assert_release_excludes_live_ado(workflow: dict[str, Any]) -> None:
    """Require publication integration to be credential-free and non-live."""
    for node in _walk_nodes(workflow):
        env = node.get("env")
        if isinstance(env, dict):
            assert "ADO_APM_PAT" not in env

    for job_id in RELEASE_INTEGRATION_JOBS:
        job = workflow_job(workflow, job_id)
        marker_expression = job["with"].get("integration-markers")
        assert isinstance(marker_expression, str)
        assert marker_expression == "${{ inputs.integration-markers }}"
        assert job["secrets"] == {"GH_CLI_PAT": "${{ secrets.GH_CLI_PAT }}"}
    platforms = json.loads((ROOT / "scripts/release-platforms.json").read_text("ascii"))
    assert all("not live" in row["integration_markers"] for row in platforms)

    reusable = load_workflow(ROOT / ".github/workflows/release-integration.yml")
    windows_step = workflow_step(
        workflow_job(reusable, "integration-tests"),
        "Run integration tests (Windows)",
    )
    assert "-IncludeLiveADO" not in shell_tokens(windows_step)


def _assert_auth_acceptance_selects_live_ado(workflow: dict[str, Any]) -> None:
    """Require opt-in auth acceptance to run the credential-gated live nodes."""
    dispatch_inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["ado_pat_e2e"] == {
        "description": (
            "Run live ADO PAT integration tests (requires ado_repo and AUTH_TEST_ADO_APM_PAT)"
        ),
        "type": "boolean",
        "default": False,
    }

    job = workflow_job(workflow, "auth-tests")
    generic_step = workflow_step(job, "Run auth acceptance tests")
    assert generic_step["env"]["AUTH_TEST_ADO_REPO"] == (
        "${{ inputs.ado_pat_e2e && inputs.ado_repo || '' }}"
    )
    assert generic_step["env"]["ADO_APM_PAT"] == (
        "${{ inputs.ado_pat_e2e && secrets.AUTH_TEST_ADO_APM_PAT || '' }}"
    )

    step = workflow_step(job, "Run live ADO PAT integration acceptance")
    assert step.get("if") == "${{ inputs.ado_pat_e2e == true }}"
    assert step["env"] == {
        "APM_BINARY_PATH": ".venv/bin/apm",
        "APM_TEST_ADO_REPO": "${{ inputs.ado_repo }}",
        "ADO_APM_PAT": "${{ secrets.AUTH_TEST_ADO_APM_PAT }}",
    }
    assert_exact_command(
        shell_commands(step),
        [
            "uv",
            "run",
            "pytest",
            "tests/integration/",
            "-m",
            LIVE_ADO_SELECTOR,
            "-v",
            "--tb=short",
        ],
        label="live ADO auth acceptance step",
    )
    run = step["run"]
    assert "ado_repo is required when ado_pat_e2e is enabled" in run
    assert "AUTH_TEST_ADO_APM_PAT is not configured" in run


def test_ado_e2e_is_live_and_credential_gated() -> None:
    """The real ADO fixture requires both opt-in scheduling and a PAT."""
    assert {mark.name for mark in test_ado_e2e.pytestmark} == {
        "live",
        "requires_ado_pat",
    }


def test_release_integration_excludes_live_ado_and_its_credential() -> None:
    """A rejected third-party PAT cannot block publication jobs."""
    _assert_release_excludes_live_ado(load_workflow(RELEASE_WORKFLOW))


def test_integration_script_owns_explicit_marker_selection() -> None:
    """The shared Unix orchestrator must pass workflow marker expressions safely."""
    script = INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    assert 'marker_args=(-m "$PYTEST_MARK_EXPR")' in script
    assert '${marker_args[@]+"${marker_args[@]}"}' in script
    assert script.index('${extra_args[@]+"${extra_args[@]}"}') < script.index(
        '${marker_args[@]+"${marker_args[@]}"}'
    )


def test_integration_script_exports_explicit_candidate_binary_path() -> None:
    """The integration harness must not rely on PATH-only candidate discovery."""
    script = INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    assert 'export APM_BINARY_PATH="$(pwd)/dist/$BINARY_NAME/apm"' in script


def test_auth_acceptance_explicitly_selects_live_ado_nodes() -> None:
    """The opt-in auth workflow remains the live PAT acceptance owner."""
    _assert_auth_acceptance_selects_live_ado(load_workflow(AUTH_WORKFLOW))


def test_release_ado_secret_mutation_is_rejected() -> None:
    """Adding the expiring ADO PAT back to publication must fail the contract."""
    workflow = deepcopy(load_workflow(RELEASE_WORKFLOW))
    workflow_job(workflow, "integration-tests-shard")["secrets"]["ADO_APM_PAT"] = "secret"

    with pytest.raises(AssertionError):
        _assert_release_excludes_live_ado(workflow)


def test_release_live_selector_mutation_is_rejected() -> None:
    """Publication cannot silently regain live external-service nodes."""
    workflow = deepcopy(load_workflow(RELEASE_WORKFLOW))
    workflow_job(workflow, "integration-tests-shard")["with"]["integration-markers"] = "live"

    with pytest.raises(AssertionError):
        _assert_release_excludes_live_ado(workflow)


def test_auth_acceptance_non_live_selector_mutation_is_rejected() -> None:
    """Auth acceptance cannot silently stop selecting the live ADO nodes."""
    workflow = deepcopy(load_workflow(AUTH_WORKFLOW))
    step = workflow_step(
        workflow_job(workflow, "auth-tests"),
        "Run live ADO PAT integration acceptance",
    )
    step["run"] = step["run"].replace(LIVE_ADO_SELECTOR, "not live")

    with pytest.raises(AssertionError):
        _assert_auth_acceptance_selects_live_ado(workflow)
