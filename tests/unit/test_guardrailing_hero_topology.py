"""Topology contract for the live packaged guardrailing hero."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.workflow_contracts import (
    load_workflow,
    shell_commands,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HERO_MODULE_PATH = "tests/integration/test_guardrailing_hero_e2e.py"
HERO_MODULE = REPO_ROOT / HERO_MODULE_PATH
RUNTIME_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-runtime.yml"
HERO_NODE = f"{HERO_MODULE_PATH}::TestGuardrailingHeroScenario::test_2_minute_guardrailing_flow"
HERO_STEP = "Run live guardrailing hero"
TRUSTED_EVENT_CONDITION = (
    "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
)
EXPECTED_MARKERS = {
    "e2e",
    "live",
    "requires_apm_binary",
    "requires_e2e_mode",
    "requires_github_token",
}


def _module_pytestmark_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    entries = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    names: set[str] = set()
    for entry in entries:
        assert isinstance(entry, ast.Attribute)
        assert isinstance(entry.value, ast.Attribute)
        assert isinstance(entry.value.value, ast.Name)
        assert entry.value.value.id == "pytest"
        assert entry.value.attr == "mark"
        names.add(entry.attr)
    return names


def test_remote_guardrailing_hero_has_complete_declarative_gates() -> None:
    assert _module_pytestmark_names(HERO_MODULE) == EXPECTED_MARKERS
    source = HERO_MODULE.read_text(encoding="utf-8")
    assert "PRIMARY_TOKEN" not in source
    assert "pytest.mark.skipif" not in source


def test_runtime_workflow_runs_live_hero_once_on_trusted_linux_x64() -> None:
    workflow = load_workflow(RUNTIME_WORKFLOW)
    triggers = workflow["on"]
    assert "pull_request" not in triggers
    assert {"schedule", "workflow_dispatch"} <= set(triggers)

    job = workflow_job(workflow, "live-inference-smoke")
    assert job["runs-on"] == "ubuntu-24.04"
    step = workflow_step(job, HERO_STEP)
    assert step["if"] == TRUSTED_EVENT_CONDITION
    assert "continue-on-error" not in step
    assert workflow_step_index(job, "Setup binary") < workflow_step_index(job, HERO_STEP)

    env = step["env"]
    assert env == {
        "APM_E2E_TESTS": "1",
        "GITHUB_APM_PAT": "${{ secrets.GH_CLI_PAT }}",
    }
    commands = shell_commands(step)
    assert commands == [["uv", "run", "pytest", HERO_NODE, "-m", "live", "-v"]]
    assert "${{" not in step["run"]

    direct_invocations = []
    for workflow_path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        candidate = load_workflow(workflow_path)
        jobs = candidate.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_name, candidate_job in jobs.items():
            if not isinstance(candidate_job, dict):
                continue
            for candidate_step in candidate_job.get("steps", []):
                if not isinstance(candidate_step, dict):
                    continue
                run = candidate_step.get("run")
                if isinstance(run, str) and HERO_NODE in run:
                    direct_invocations.append(
                        (workflow_path.name, job_name, candidate_step.get("name"))
                    )
    assert direct_invocations == [
        ("ci-runtime.yml", "live-inference-smoke", HERO_STEP),
    ]


def test_generic_integration_scripts_do_not_invoke_live_hero() -> None:
    scripts = REPO_ROOT / "scripts"
    references = [
        path.relative_to(REPO_ROOT).as_posix()
        for pattern in ("*.sh", "*.ps1")
        for path in scripts.rglob(pattern)
        if HERO_MODULE_PATH in path.read_text(encoding="utf-8")
    ]
    assert references == []
