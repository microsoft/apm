"""Offline regression contracts for advisory metadata, rollout, and consumers."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
import yaml

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "packages/apm-triage-panel"
SCRIPT = PACKAGE / "scripts/triage_state.py"
CONTRACT = json.loads((PACKAGE / "assets/label-contract.json").read_text())
PLAN_BATCH = runpy.run_path(str(SCRIPT))["plan_batch"]
AUTOPILOT = ROOT / "packages/apm-issue-autopilot/.apm/skills/apm-issue-autopilot"


def _issue(number: int, labels: list[str] | None = None, author: str = "reporter") -> dict:
    """Make normalized read data; no real issue API is contacted."""
    return {"number": number, "author": author, "labels": labels or [], "eligible": True}


def _plan(issues: list[dict], mode: str = "sweep", labels: list[str] | None = None) -> dict:
    """Exercise the same planner the workflow invokes."""
    return PLAN_BATCH(
        {"mode": mode, "issues": issues, "repository_labels": labels or ["status/triaged"]},
        CONTRACT,
    )


@pytest.mark.parametrize("marker", ["status/triaged", "triage/recommended"])
def test_human_needs_triage_does_not_reenqueue_completed_advice(marker: str) -> None:
    """Both markers suppress sweeps while preserving pending human decision state."""
    issue = _issue(1, [marker, "status/needs-triage"])
    original = deepcopy(issue)
    assert _plan([issue])["selected"] == []
    assert issue == original


@pytest.mark.parametrize("mode", ["label-event", "dispatch"])
@pytest.mark.parametrize("marker", ["status/triaged", "triage/recommended"])
def test_explicit_retriage_works_with_either_completed_marker(mode: str, marker: str) -> None:
    """Fresh advisory requests bypass processing, not human approval."""
    result = _plan([_issue(1, [marker, "status/needs-triage", "triage/requested"])], mode)
    assert result["selected"][0]["number"] == 1
    assert result["selected"][0]["remove_labels"] == ["triage/requested"]
    assert result["implementation_authorized"] is False


def test_skipped_first_page_and_author_quota_do_not_starve_later_issues() -> None:
    """Accumulate pages until the batch is full, rather than stopping at page one."""
    first_page = [_issue(n, ["status/triaged", "status/needs-triage"]) for n in range(1, 101)]
    assert _plan(first_page) == {
        "selected": [],
        "batch_full": False,
        "implementation_authorized": False,
    }
    repeated_author = [_issue(n) for n in range(101, 131)]
    later_authors = [_issue(n, author=f"author-{n}") for n in range(131, 141)]
    result = _plan(first_page + repeated_author + later_authors)
    assert [item["number"] for item in result["selected"]] == [101, 102, *range(131, 139)]
    assert result["batch_full"] is True


def test_compatibility_needs_no_new_labels_and_never_writes_human_state() -> None:
    """Unknown/new labels and human decisions cannot enter a write plan."""
    issue = _issue(1, ["status/needs-triage", "status/accepted", "help wanted", "bug"])
    issue["proposed_labels"] = [
        "type/feature",
        "area/cli",
        "status/deferred",
        "status/accepted",
        "priority/high",
        "good first issue",
        "triage/recommended",
        "invented",
    ]
    original = deepcopy(issue)
    result = _plan([issue], labels=["status/triaged", "type/feature", "area/cli"])
    assert result["selected"] == [
        {"number": 1, "add_labels": ["area/cli", "status/triaged"], "remove_labels": []}
    ]
    assert result["implementation_authorized"] is False
    assert issue == original


def test_missing_active_marker_fails_before_any_emission() -> None:
    """Do not silently switch markers or create absent labels during rollout."""
    with pytest.raises(ValueError, match="Active processing label"):
        _plan([_issue(1)], labels=["triage/recommended"])
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"mode": "sweep", "repository_labels": [], "issues": [_issue(1)]}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no labels created" in result.stderr


@pytest.mark.parametrize("mode", ["sweep", "label-event", "dispatch"])
def test_ineligible_issues_are_never_selected(mode: str) -> None:
    """Explicit retriage must still respect the caller's eligibility filter."""
    issue = _issue(1)
    issue["eligible"] = False
    assert _plan([issue], mode)["selected"] == []


def test_conflicting_classification_and_duplicate_reads_are_bounded() -> None:
    """Ambiguous dimensions fail explicitly; overlapping pages cannot double-post."""
    issue = _issue(1)
    issue["proposed_labels"] = ["type/bug", "type/feature"]
    with pytest.raises(ValueError, match="Conflicting proposed type"):
        _plan([issue], labels=["status/triaged", "type/bug", "type/feature"])
    assert len(_plan([_issue(1), _issue(1)])["selected"]) == 1


def test_deferred_consumer_schema_rejects_legacy_acceptance_and_release_fields() -> None:
    """The internal decision remains advice; no writable human metadata survives."""
    schema = json.loads((AUTOPILOT / "assets/autopilot-triage-schema.json").read_text())
    row = {
        "kind": "autopilot-triage-decision",
        "issue": 1,
        "decision": "defer-later",
        "confidence": "high",
        "red_flags": [],
    }
    jsonschema.validate(row, schema)
    for key, value in {
        "status": "status/accepted",
        "priority": "priority/high",
        "milestone": "v1",
        "preserved_labels": ["help wanted"],
    }.items():
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**row, key: value}, schema)
    skill = (PACKAGE / "SKILL.md").read_text()
    assert "NEVER map `defer-later` to `status/accepted`" in skill
    assert "Labels and silence are not" in skill
    autopilot = (AUTOPILOT / "SKILL.md").read_text()
    assert "Do not implement if explicit human approval or review capacity is missing" in autopilot
    assert "Do not apply `status/accepted`" in autopilot


def test_template_has_proposed_brief_not_an_operative_decision() -> None:
    """Check the machine-readable shape actually shipped in the template."""
    template = (PACKAGE / "assets/triage-template.md").read_text(encoding="ascii")
    payload = json.loads(template.split("```json triage-recommendation\n")[1].split("\n```")[0])
    assert payload["schema_version"] == 2
    assert payload["advisory_only"] is True
    assert set(payload["proposed_brief"]) == {"scope", "done_when", "exclusions", "review_needs"}
    assert not {"decision", "status", "priority", "milestone", "preserved_labels"} & payload.keys()
    assert template.count("<details>") == 6


def test_installed_skill_files_and_recorded_hashes_match_sources() -> None:
    """Exercise the published package and its generated installation contract."""
    lock = yaml.safe_load((ROOT / "apm.lock.yaml").read_text())
    for name, source in [("apm-triage-panel", PACKAGE), ("apm-issue-autopilot", AUTOPILOT)]:
        dep = next(item for item in lock["dependencies"] if item["name"] == name)
        for relative, expected in dep["deployed_file_hashes"].items():
            installed = ROOT / relative
            source_file = source / installed.relative_to(ROOT / ".agents/skills" / name)
            assert installed.read_bytes() == source_file.read_bytes()
            assert expected == f"sha256:{hashlib.sha256(installed.read_bytes()).hexdigest()}"
