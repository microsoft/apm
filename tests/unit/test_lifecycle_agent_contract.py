"""Keep P8's operational owner connected to its agent consumers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft7Validator

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
RULE = ".apm/instructions/lifecycle.instructions.md"
CONSUMERS = [
    ".apm/agents/apm-ceo.agent.md",
    ".apm/agents/test-coverage-expert.agent.md",
    "packages/shepherd-driver/SKILL.md",
    "packages/shepherd-driver/assets/shepherd-driver-prompt.md",
    "packages/shepherd-driver/assets/conflict-resolution-prompt.md",
    "packages/shepherd-driver/references/mergeability-gate.md",
    "packages/apm-review-panel/SKILL.md",
    "packages/pr-description-skill/assets/scenario-evidence-rubric.md",
    "packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/SKILL.md",
    "packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/assets/fix-prompt.md",
    "packages/apm-issue-autopilot/.apm/skills/apm-issue-autopilot/SKILL.md",
    *[
        f"packages/apm-issue-autopilot/.apm/skills/apm-issue-autopilot/assets/{name}.md"
        for name in (
            "ideate-prompt",
            "task-implement-prompt",
            "solution-pipeline-prompt",
            "acceptance-observer",
        )
    ],
]


def test_lifecycle_rule_is_universally_scoped_and_deployed() -> None:
    """Payloads and indirect changes cannot escape a source-only instruction glob."""
    content = (ROOT / RULE).read_text(encoding="ascii")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert frontmatter["applyTo"] == "**"
    assert (ROOT / ".github/instructions/lifecycle.instructions.md").read_text(
        encoding="ascii"
    ) == content


@pytest.mark.parametrize("path", CONSUMERS)
def test_lifecycle_consumers_load_one_operational_rule(path: str) -> None:
    """Implementation, review, completion and rebase must retain the owner link."""
    assert "lifecycle.instructions.md" in (ROOT / path).read_text(encoding="ascii")


def test_panel_retains_lazy_specialist_reference_in_deployed_bundle() -> None:
    """Progressive disclosure must not remove conditional specialists' inputs."""
    reference = "references/conditional-panelists.md"
    source = ROOT / "packages/apm-review-panel"
    assert reference in (source / "SKILL.md").read_text(encoding="ascii")
    assert (source / reference).read_bytes() == (
        ROOT / ".agents/skills/apm-review-panel" / reference
    ).read_bytes()


def test_panel_template_can_report_unmet_repository_precondition() -> None:
    """Advisory rendering must not conceal a required shipping failure."""
    template = ROOT / "packages/apm-review-panel/assets/recommendation-template.md"
    assert "P8 shipping precondition unsatisfied" in template.read_text(encoding="ascii")


def _panel_evidence() -> dict[str, Any]:
    """Build the existing coverage persona's lifecycle evidence return shape."""
    return {
        "persona": "test-coverage-expert",
        "active": True,
        "summary": "Lifecycle evidence is present.",
        "findings": [
            {
                "severity": "nit",
                "summary": "Required trajectory executed.",
                "rationale": "P8 requires actual lifecycle assertions.",
                "evidence": {
                    "tier": "lifecycle-state-machine",
                    "outcome": "passed",
                    "test_file": "tests/integration/test_generated_lifecycle_state_machine.py",
                    "run_evidence": "Native full report for the candidate.",
                    "assertion_excerpt": "assert state.deployed_bytes == expected",
                },
            }
        ],
    }


@pytest.mark.parametrize("outcome", ["passed", "failed", "unknown", "missing"])
@pytest.mark.parametrize("missing", [None, "test_file", "run_evidence", "assertion_excerpt"])
def test_panel_can_carry_only_complete_passing_lifecycle_evidence(
    missing: str | None,
    outcome: str,
) -> None:
    """The declared persona tier must serialize without accepting empty proof."""
    schema_path = ROOT / "packages/apm-review-panel/assets/panelist-return-schema.json"
    schema = json.loads(schema_path.read_text(encoding="ascii"))
    Draft7Validator.check_schema(schema)
    document = _panel_evidence()
    document["findings"][0]["evidence"]["outcome"] = outcome
    if missing:
        del document["findings"][0]["evidence"][missing]
    errors = list(Draft7Validator(schema).iter_errors(document))
    assert bool(errors) is (outcome == "passed" and missing is not None)
