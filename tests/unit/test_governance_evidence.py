"""Exercise the dependency-free governance scripts and trusted workflow contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.architecture_linter.checks.contracts_governance import (
    RULE_ID,
    check_governance_authority,
)
from scripts.architecture_linter.facts import FactsProvider

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.component


def test_governance_node_regressions() -> None:
    """Run real deterministic metadata/parser/event scenarios without network."""
    result = subprocess.run(
        ["node", "--test", "tests/scripts/governance_evidence.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_advisory_workflow_executes_only_trusted_default_branch_code() -> None:
    """Privileged reporting cannot run fork code or become a required merge gate."""
    text = (ROOT / ".github/workflows/pr-eligibility.yml").read_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {
        "pull_request_target",
        "issue_comment",
        "issues",
        "merge_group",
        "push",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
        "checks": "write",
    }
    assert workflow["concurrency"]["queue"] == "max"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = workflow["jobs"]["report"]["steps"]
    assert len(steps) == 3
    assert steps[1]["with"]["ref"] == "main"
    assert steps[1]["with"]["persist-credentials"] is False
    assert "repo.default_branch !== 'main'" in steps[0]["with"]["script"]
    assert "['rev-parse', 'HEAD']" in steps[2]["with"]["script"]
    assert steps[2]["with"]["script"].index("rev-parse") < steps[2]["with"]["script"].index(
        "require('./scripts/governance/run.cjs')"
    )
    assert "require('./scripts/governance/run.cjs')" in steps[2]["with"]["script"]
    assert all("run" not in step for step in steps)
    assert "head.repo" not in text
    assert "head.sha" not in text
    assert "npm install" not in text
    assert "secrets." not in text
    assert "eligibility" not in (ROOT / ".github/workflows/merge-gate.yml").read_text()


@pytest.mark.parametrize(
    ("branch", "api_failure", "expected_code"),
    [("main", False, 0), ("feature", False, 1), ("refs/heads/main", False, 1), ("main", True, 1)],
)
def test_trusted_checkout_guard_fails_closed(
    branch: str, api_failure: bool, expected_code: int
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-eligibility.yml").read_text())
    script = workflow["jobs"]["report"]["steps"][0]["with"]["script"]
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const github = {rest: {repos: {get: async () => {
  if (input.apiFailure) throw new Error('API unavailable');
  return {data: {default_branch: input.branch}};
}}}};
new AsyncFunction('github', 'context', input.script)(github, {repo: {}})
  .catch(error => { console.error(error.message); process.exitCode = 1; });
""",
        ],
        input=json.dumps({"script": script, "branch": branch, "apiFailure": api_failure}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_code
    assert bool(result.stderr) == bool(expected_code)


def test_cli_help_is_noninteractive_and_explains_authority_boundary() -> None:
    """The shared tool is directly invokable without hidden package dependencies."""
    result = subprocess.run(
        ["node", "scripts/governance/eligibility.cjs", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "never implementation permission" in result.stdout
    assert "--approval-url" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize(
    ("filename", "original", "replacement"),
    [
        ("authority.cjs", "authorizes_implementation: false", "authorizes_implementation: true"),
        ("eligibility.cjs", "readPolicy(", "parallelReadPolicy("),
        ("run.cjs", "conclusion: 'neutral'", "conclusion: 'success'"),
    ],
)
def test_architecture_guard_rejects_approval_authority_drift(
    tmp_path: Path, filename: str, original: str, replacement: str
) -> None:
    """Mutation breaks show permission and delegation drift cannot pass silently."""
    files = tuple(
        f"scripts/governance/{name}" for name in ("authority.cjs", "eligibility.cjs", "run.cjs")
    )
    for relative in files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    assert not check_governance_authority(FactsProvider(tmp_path, files, None))
    mutated = tmp_path / "scripts/governance" / filename
    mutated.write_text(mutated.read_text().replace(original, replacement))
    findings = check_governance_authority(FactsProvider(tmp_path, files, None))
    assert findings
    assert {finding.rule_id for finding in findings} == {RULE_ID}
