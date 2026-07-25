"""Contract checks for shepherd-driver functional-evidence eval fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
EVALS = ROOT / "tests/fixtures/shepherd_driver/functional_evidence_evals.json"
PROMPT = ROOT / "packages/shepherd-driver/assets/shepherd-driver-prompt.md"
GATE = ROOT / "packages/shepherd-driver/scripts/owner_touch_gate.py"
REQUIRED_SCENARIOS = {
    "positive-owner-touch",
    "missing-functional-evidence",
    "false-self-classification",
    "unrelated-diff",
    "owner-table-drift",
}


def _manifest() -> dict[str, Any]:
    """Load the deterministic eval manifest."""
    return json.loads(EVALS.read_text(encoding="ascii"))


def test_content_evals_cover_required_fail_closed_scenarios() -> None:
    """The eval inventory must retain all Shepherd evidence scenarios."""
    manifest = _manifest()
    scenarios = {item["id"]: item for item in manifest["content_evals"]}

    assert set(scenarios) == REQUIRED_SCENARIOS
    for scenario in scenarios.values():
        with_skill = set(scenario["with_skill"]["must_include"])
        without_skill = set(scenario["without_skill"]["must_not_include"])
        assert with_skill & without_skill


def test_bundle_materializes_all_with_skill_eval_anchors() -> None:
    """The shipped prompt and gate must expose each value-delta anchor."""
    bundle = PROMPT.read_text(encoding="ascii") + GATE.read_text(encoding="ascii")
    anchors = {
        anchor
        for scenario in _manifest()["content_evals"]
        for anchor in scenario["with_skill"]["must_include"]
    }

    missing = sorted(anchor for anchor in anchors if anchor not in bundle)
    assert missing == []


def test_trigger_evals_keep_fixed_sixty_forty_split() -> None:
    """Forced-entrypoint trigger examples keep the Genesis 60/40 split."""
    trigger_evals = _manifest()["trigger_evals"]

    for polarity in ("should_trigger", "should_not_trigger"):
        examples = trigger_evals[polarity]
        assert len(examples["train"]) == 6
        assert len(examples["val"]) == 4
        assert len(examples["train"]) + len(examples["val"]) == 10

    assert _manifest()["invocation_mode"] == "forced"
