"""Offline eligibility filters for triage queue fetch; no GitHub I/O."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = (
    ROOT / "packages/autopilot-issue-triage-scheduler/.apm/skills/autopilot-issue-triage-scheduler"
)
SCRIPT = PACKAGE / "scripts/fetch_queue.py"
CONTRACT = json.loads((PACKAGE / "assets/label-contract.json").read_text(encoding="ascii"))
FETCH = runpy.run_path(str(SCRIPT))
REAL_BODY = (
    "Install with --prefix fails when the destination path contains spaces "
    'on Windows. Reproduction: apm install --prefix "C:\\Program Files\\apm".'
)
TEMPLATE_BODY = """<!-- form: bug -->
**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
"""


def _raw(
    number: int,
    *,
    body: str | None = REAL_BODY,
    login: str = "reporter",
    author_type: str = "User",
    state: str = "open",
    locked: bool = False,
    draft: bool = False,
    merged_at: str | None = None,
    labels: list[str] | None = None,
    pull_request: dict | None = None,
) -> dict:
    """Build a REST-shaped item; never a live API payload."""
    item = {
        "number": number,
        "body": body,
        "user": {"login": login, "type": author_type},
        "state": state,
        "locked": locked,
        "draft": draft,
        "labels": [{"name": name} for name in (labels or [])],
    }
    if merged_at is not None:
        item["merged_at"] = merged_at
    if pull_request is not None:
        item["pull_request"] = pull_request
    return item


def test_state_and_body_skips_are_stable() -> None:
    """Closed, locked, bot, empty, and template-only never become eligible."""
    cases = [
        (_raw(1, state="closed"), "closed"),
        (_raw(2, locked=True), "locked"),
        (_raw(3, login="github-actions[bot]", author_type="Bot"), "bot-authored"),
        (_raw(4, body="   "), "empty"),
        (_raw(5, body=TEMPLATE_BODY), "template-only"),
        (_raw(6, merged_at="2026-01-01T00:00:00Z", state="closed"), "merged"),
    ]
    for raw, expected in cases:
        record = FETCH["normalize_record"](raw, "issue")
        assert FETCH["skip_reason"](record, "dispatch") == expected
        assert FETCH["skip_reason"](record, "sweep") == expected


def test_sweep_skips_spam_and_draft_but_explicit_keeps_them() -> None:
    """Explicit requests bypass only spam and draft, not other preconditions."""
    spam = FETCH["normalize_record"](_raw(1, body="http://example.test/" * 40), "issue")
    draft = FETCH["normalize_record"](_raw(2, draft=True), "pr")
    short = FETCH["normalize_record"](_raw(3, body="hi"), "issue")
    assert FETCH["skip_reason"](spam, "sweep") == "spam"
    assert FETCH["skip_reason"](spam, "dispatch") is None
    assert FETCH["skip_reason"](draft, "sweep") == "draft"
    assert FETCH["skip_reason"](draft, "dispatch") is None
    assert FETCH["skip_reason"](short, "sweep") == "spam"
    assert FETCH["skip_reason"](short, "dispatch") is None


BUG_FORM_BODY = """## Describe the bug

A marketplace plugin whose directory ships `skills/name/SKILL.md`
and a root `.mcp.json` never registers those servers.

### Steps to reproduce

1. Place the plugin in the marketplace cache.
2. Run `apm install` without `.claude-plugin/plugin.json`.

```yaml
mcpServers:
  demo: {}
```

Expected: servers appear in the lockfile. Actual: they are dropped.
"""


def test_markdown_bug_form_is_not_sweep_spam() -> None:
    """Headings, lists, and fences must not wipe reporter prose."""
    record = FETCH["normalize_record"](_raw(2992, body=BUG_FORM_BODY), "issue")
    assert FETCH["_alnum_count"](FETCH["strip_markup"](BUG_FORM_BODY)) >= 20
    assert FETCH["is_spam_shaped"](BUG_FORM_BODY) is False
    assert FETCH["skip_reason"](record, "sweep") is None


def test_identical_run_and_repeated_token_count_as_spam() -> None:
    """Published spam heuristics are encoded, not left to the model."""
    run = FETCH["normalize_record"](_raw(1, body="a" * 50 + " more text here for length"), "issue")
    repeated = FETCH["normalize_record"](_raw(2, body="abc" * 80), "issue")
    assert FETCH["is_spam_shaped"](run["body"]) is True
    assert FETCH["is_spam_shaped"](repeated["body"]) is True
    assert FETCH["skip_reason"](run, "sweep") == "spam"


def test_completed_advice_stays_eligible_for_the_planner() -> None:
    """Fetch does not own completed-advice; plan_batch skips it on sweep."""
    raw = _raw(8, labels=["status/triaged"])
    record = FETCH["normalize_record"](raw, "issue")
    assert FETCH["skip_reason"](record, "sweep") is None
    batch = FETCH["build_batch"]([record], "sweep", ["triage/recommended", "status/triaged"])
    assert batch["issues"][0]["eligible"] is True
    plan = FETCH["plan_batch"](batch, CONTRACT)
    assert plan["selected"] == []


def test_missing_linked_issue_is_not_a_skip() -> None:
    """Community PRs without an issue remain fetch-eligible."""
    record = FETCH["normalize_record"](_raw(9), "pr")
    assert "linked" not in (FETCH["skip_reason"](record, "sweep") or "")
    assert FETCH["skip_reason"](record, "sweep") is None


def test_accumulate_stops_when_plan_is_full(tmp_path: Path) -> None:
    """Oldest-first pages continue past skips until ten selectable items."""
    records = [
        FETCH["normalize_record"](_raw(n, labels=["status/triaged"]), "issue") for n in range(1, 6)
    ]
    records.extend(FETCH["normalize_record"](_raw(n, login="same"), "issue") for n in range(6, 20))
    records.extend(
        FETCH["normalize_record"](_raw(n, login=f"author-{n}"), "issue") for n in range(20, 30)
    )
    batch = FETCH["accumulate_for_plan"](
        records, "sweep", ["triage/recommended", "status/triaged"], CONTRACT
    )
    plan = FETCH["plan_batch"](batch, CONTRACT)
    assert plan["batch_full"] is True
    assert [item["number"] for item in plan["selected"]] == [6, 7, *range(20, 28)]


def test_cli_records_json_skips_gh(tmp_path: Path) -> None:
    """Fixture path is the deterministic agent default and does not need gh."""
    records = tmp_path / "records.json"
    labels = tmp_path / "labels.json"
    records.write_text(json.dumps([_raw(42)]), encoding="ascii")
    labels.write_text(json.dumps(["triage/recommended", "status/triaged"]), encoding="ascii")
    code = FETCH["main"](
        [
            "--kind",
            "issue",
            "--mode",
            "sweep",
            "--records-json",
            str(records),
            "--labels-json",
            str(labels),
        ]
    )
    assert code == 0


def test_cli_subprocess_filters_ineligible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The script entrypoint prints planner JSON on stdout."""
    records = tmp_path / "records.json"
    labels = tmp_path / "labels.json"
    records.write_text(
        json.dumps([_raw(1, body=""), _raw(2), _raw(3, login="dependabot", author_type="Bot")]),
        encoding="ascii",
    )
    labels.write_text(json.dumps(["triage/recommended", "status/triaged"]), encoding="ascii")
    code = FETCH["main"](
        [
            "--kind",
            "issue",
            "--mode",
            "sweep",
            "--records-json",
            str(records),
            "--labels-json",
            str(labels),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    by_number = {item["number"]: item["eligible"] for item in payload["issues"]}
    assert by_number == {1: False, 2: True, 3: False}
    reasons = {item["number"]: item["reason"] for item in payload["skipped"]}
    assert reasons == {1: "empty", 3: "bot-authored"}


def test_list_pages_drops_pulls_for_issue_kind() -> None:
    """Issue fetch must not enqueue pull requests from the issues endpoint."""

    def _runner(_args: list[str]) -> list[dict]:
        return [_raw(1, pull_request={"url": "https://example.test/p/1"}), _raw(2)]

    records = FETCH["list_pages"]("issue", "microsoft/apm", runner=_runner)
    assert [item["number"] for item in records] == [2]


def test_queue_helpers_match_across_schedulers_and_worker_contract() -> None:
    """Issue and PR schedulers ship one helper implementation; worker has no queue scripts."""
    pr = ROOT / "packages/autopilot-pr-triage-scheduler/.apm/skills/autopilot-pr-triage-scheduler"
    worker = ROOT / "packages/autopilot-issue-triage-worker"
    for name in (
        "scripts/fetch_queue.py",
        "scripts/triage_state.py",
        "assets/label-contract.json",
    ):
        assert (PACKAGE / name).read_bytes() == (pr / name).read_bytes()
    assert (PACKAGE / "assets/label-contract.json").read_bytes() == (
        worker / "assets/label-contract.json"
    ).read_bytes()
    assert not (worker / "scripts").exists()
