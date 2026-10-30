"""Offline eligibility filters for triage queue fetch; no GitHub I/O."""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = (
    ROOT
    / "packages/autopilot/autopilot-issue-triage-scheduler/.apm/skills/autopilot-issue-triage-scheduler"
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
        "comments": 0,
    }
    if merged_at is not None:
        item["merged_at"] = merged_at
    if pull_request is not None:
        item["pull_request"] = pull_request
    return item


def test_state_and_body_skips_are_stable() -> None:
    """Closed, locked, empty, and template-only never become eligible."""
    cases = [
        (_raw(1, state="closed"), "closed"),
        (_raw(2, locked=True), "locked"),
        (_raw(4, body="   "), "empty"),
        (_raw(5, body=TEMPLATE_BODY), "template-only"),
        (_raw(6, merged_at="2026-01-01T00:00:00Z", state="closed"), "merged"),
    ]
    for raw, expected in cases:
        record = FETCH["normalize_record"](raw, "issue")
        assert FETCH["skip_reason"](record, "dispatch") == expected
        assert FETCH["skip_reason"](record, "sweep") == expected


def test_bot_authored_issues_and_prs_stay_eligible() -> None:
    """Copilot, dependabot, and Actions items must enter the triage queue."""
    logins = (
        ("github-actions[bot]", "Bot"),
        ("dependabot", "Bot"),
        ("copilot", "Bot"),
        ("Copilot", "Bot"),
    )
    for kind in ("issue", "pr"):
        for login, author_type in logins:
            record = FETCH["normalize_record"](_raw(10, login=login, author_type=author_type), kind)
            assert FETCH["skip_reason"](record, "sweep") is None
            assert FETCH["skip_reason"](record, "dispatch") is None


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
    """Records-json still leaves completed-advice eligible; plan_batch skips it."""
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
        json.dumps([_raw(1, body=""), _raw(2), _raw(3, state="closed")]),
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
    assert reasons == {1: "empty", 3: "closed"}


def test_list_pages_sweep_excludes_completed_advice_via_search() -> None:
    """Live sweep lists through search minus triage/recommended and status/triaged."""
    seen: list[list[str]] = []

    def _runner(args: list[str]) -> dict:
        seen.append(args)
        return {"items": [_raw(2993), _raw(8, labels=["status/triaged"])]}

    records = FETCH["list_pages"](
        "issue",
        "microsoft/apm",
        runner=_runner,
        exclude_labels=["triage/recommended", "status/triaged"],
    )
    assert seen and seen[0][0] == "api"
    assert seen[0][1] == "--paginate"
    query = seen[0][2]
    assert query.startswith("/search/issues?")
    assert "is%3Aissue" in query
    assert "triage%2Frecommended" in query or "triage/recommended" in query
    assert "status%2Ftriaged" in query or "status/triaged" in query
    assert [item["number"] for item in records] == [2993, 8]


def test_list_pages_drops_pulls_for_issue_kind() -> None:
    """Issue fetch must not enqueue pull requests from the issues endpoint."""

    def _runner(_args: list[str]) -> list[dict]:
        return [_raw(1, pull_request={"url": "https://example.test/p/1"}), _raw(2)]

    records = FETCH["list_pages"]("issue", "microsoft/apm", runner=_runner)
    assert [item["number"] for item in records] == [2]


def test_queue_helpers_match_across_schedulers_and_worker_contract() -> None:
    """Issue and PR schedulers share queue helpers; the worker has no queue scripts."""
    pr = (
        ROOT
        / "packages/autopilot/autopilot-pr-triage-scheduler/.apm/skills/autopilot-pr-triage-scheduler"
    )
    worker = ROOT / "packages/autopilot/autopilot-issue-triage-worker"
    for name in (
        "scripts/fetch_queue.py",
        "scripts/triage_state.py",
        "assets/label-contract.json",
    ):
        assert (PACKAGE / name).read_bytes() == (pr / name).read_bytes()
    assert (PACKAGE / "assets/label-contract.json").read_bytes() == (
        worker / "assets/label-contract.json"
    ).read_bytes()
    assert not (worker / "scripts/fetch_queue.py").exists()
    assert not (worker / "scripts/triage_state.py").exists()
    assert (worker / "scripts/check_required_agents.py").is_file()


def test_trusted_gh_uses_cli_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue helpers resolve gh through get_gh_executable, not shutil.which."""
    source = (PACKAGE / "scripts/fetch_queue.py").read_text(encoding="ascii")
    assert "shutil.which" not in source
    assert "get_gh_executable" in source
    monkeypatch.setattr(
        "apm_cli.utils.git_env.get_gh_executable",
        lambda: "/trusted/bin/gh",
    )
    assert FETCH["_trusted_gh"]() == "/trusted/bin/gh"


def _scheduled_runner(
    issues: dict[int, dict], comments: dict[int, list[dict]], calls: list[list[str]]
) -> Callable[[list[str]], Any]:
    """Serve paginated REST evidence without contacting GitHub."""

    def run(args: list[str]) -> Any:
        calls.append(args)
        parsed = urlparse(args[-1])
        if parsed.path == "/search/issues":
            query = parse_qs(parsed.query)
            assert query["sort"] == ["created"]
            assert query["order"] == ["asc"]
            for label in ("status/accepted", "status/declined"):
                assert f'-label:"{label}"' in query["q"][0]
            return {
                "items": list(issues.values()),
                "incomplete_results": False,
                "total_count": len(issues),
            }
        number = int(parsed.path.split("/")[5])
        if parsed.path.endswith("/comments"):
            assert "--paginate" in args and "--slurp" in args
            rows = comments.get(number, [])
            return [rows[:100], rows[100:]]
        return issues[number]

    return run


def _scheduled_batch(
    issues: dict[int, dict], comments: dict[int, list[dict]], calls: list[list[str]]
) -> dict:
    """Exercise live-fetch selection with injected REST responses."""
    records = FETCH["scheduled_records"](
        "microsoft/apm", CONTRACT, runner=_scheduled_runner(issues, comments, calls)
    )
    return FETCH["accumulate_for_plan"](
        records, "sweep", ["triage/recommended"], CONTRACT, scheduled_sweep=True
    )


def test_scheduled_sweep_skips_decisions_before_quota_and_five_item_cap() -> None:
    """Decisions on late comment pages cannot crowd out undecided issues."""
    issues = {n: _raw(n, login=f"author-{n}", labels=[]) for n in range(1, 11)}
    issues[1]["labels"] = [{"name": "status/accepted"}]
    issues[2]["labels"] = [{"name": "status/declined"}]
    comments = {
        3: [{"id": n, "body": "Earlier context", "user": {"login": "human"}} for n in range(100)]
        + [
            {
                "id": 100,
                "body": "<!-- apm-scope:v1 -->\nDecision: approve",
                "user": {"login": "human"},
            }
        ],
    }
    issues[4]["body"] = REAL_BODY + "\n<!-- apm-scope:v1 -->"
    for number, rows in comments.items():
        issues[number]["comments"] = len(rows)
    calls: list[list[str]] = []
    batch = _scheduled_batch(issues, comments, calls)
    plan = FETCH["plan_batch"](batch, CONTRACT)
    assert [item["number"] for item in plan["selected"]] == [5, 6, 7, 8, 9]
    assert plan["batch_full"] is True
    assert plan["implementation_authorized"] is False
    assert [item["number"] for item in batch["contexts"]] == [5, 6, 7, 8, 9]
    assert all(item["comments"] == [] for item in batch["contexts"])
    assert all(urlparse(call[-1]).path != "/repos/microsoft/apm/issues/10" for call in calls)


@pytest.mark.parametrize(
    "broken", ["failed-page", "incomplete-search", "malformed-comment", "missing-comment"]
)
def test_scheduled_sweep_fails_closed_on_incomplete_evidence(broken: str) -> None:
    """No partial batch is returned as an exhausted or verified queue."""
    issue = _raw(1)
    issue["comments"] = 1
    base = _scheduled_runner({1: issue}, {}, [])

    def runner(args: list[str]) -> Any:
        path = urlparse(args[-1]).path
        if path == "/search/issues" and broken == "incomplete-search":
            return {"items": [issue], "incomplete_results": True, "total_count": 1}
        if path.endswith("/comments"):
            if broken == "failed-page":
                raise ValueError("comment page unavailable")
            if broken == "malformed-comment":
                return [[{"id": 1, "body": None}]]
        return base(args)

    with pytest.raises(ValueError):
        FETCH["accumulate_for_plan"](
            FETCH["scheduled_records"]("microsoft/apm", CONTRACT, runner=runner),
            "sweep",
            ["triage/recommended"],
            CONTRACT,
            scheduled_sweep=True,
        )


def test_scheduled_sweep_records_json_requires_complete_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline input cannot silently bypass scheduled evidence acquisition."""
    path = tmp_path / "records.json"
    path.write_text(json.dumps([_raw(1)]))
    assert (
        FETCH["main"](
            ["--kind", "issue", "--mode", "sweep", "--scheduled-sweep", "--records-json", str(path)]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "scheduled" in captured.err.lower()


@pytest.mark.parametrize(
    "kind,mode", [("pr", "sweep"), ("issue", "dispatch"), ("issue", "label-event")]
)
def test_scheduled_profile_cannot_change_other_invocations(
    kind: str, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The opt-in cannot change explicit requests or the PR pool."""
    assert FETCH["main"](["--kind", kind, "--mode", mode, "--scheduled-sweep"]) == 1
    assert capsys.readouterr().out == ""


def test_scheduled_search_continues_past_a_full_decided_page() -> None:
    """A page of exclusions is not an exhausted queue."""
    issues = {n: _raw(n, labels=["status/accepted"]) for n in range(1, 101)}
    issues[101] = _raw(101)
    base = _scheduled_runner(issues, {}, [])

    def runner(args: list[str]) -> Any:
        parsed = urlparse(args[-1])
        if parsed.path == "/search/issues":
            page = int(parse_qs(parsed.query)["page"][0])
            items = list(issues.values())[:100] if page == 1 else [issues[101]]
            return {"items": items, "incomplete_results": False, "total_count": 101}
        return base(args)

    batch = FETCH["accumulate_for_plan"](
        FETCH["scheduled_records"]("microsoft/apm", CONTRACT, runner=runner),
        "sweep",
        ["triage/recommended"],
        CONTRACT,
        scheduled_sweep=True,
    )
    assert [item["number"] for item in FETCH["plan_batch"](batch, CONTRACT)["selected"]] == [101]
    assert batch["contexts"][0]["number"] == 101


def test_scheduled_cli_fetches_labels_and_complete_selected_contexts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow's actual command produces both selection and worker evidence."""
    base = _scheduled_runner({1: _raw(1)}, {}, [])

    def runner(args: list[str]) -> Any:
        if urlparse(args[-1]).path == "/repos/microsoft/apm/labels":
            assert "--slurp" in args
            return [[{"name": "triage/recommended"}], [{"name": "type/bug"}]]
        return base(args)

    monkeypatch.setitem(FETCH["main"].__globals__, "_gh_json", runner)
    assert (
        FETCH["main"](
            ["--kind", "issue", "--mode", "sweep", "--scheduled-sweep", "--repo", "microsoft/apm"]
        )
        == 0
    )
    batch = json.loads(capsys.readouterr().out)
    assert batch["repository_labels"] == ["triage/recommended", "type/bug"]
    assert batch["scheduled_sweep"] is True
    assert batch["contexts"] == [{**_raw(1), "comments": []}]


@pytest.mark.parametrize("payload", [[], [{}], [[{}]], [[{"name": ""}]], [[{"name": 1}]]])
def test_scheduled_label_fetch_fails_closed_on_incomplete_pages(payload: list) -> None:
    """Malformed page or label evidence cannot become a valid inventory."""
    with pytest.raises(ValueError, match="incomplete"):
        FETCH["list_repo_labels"]("microsoft/apm", runner=lambda _: payload, complete=True)


@pytest.mark.parametrize("overflow", [False, True])
def test_scheduled_search_fails_closed_on_truncated_results(overflow: bool) -> None:
    """A short page or search-window ceiling cannot report queue exhaustion."""

    def runner(args: list[str]) -> Any:
        parsed = urlparse(args[-1])
        if parsed.path == "/search/issues":
            page = int(parse_qs(parsed.query)["page"][0])
            numbers = range((page - 1) * 100 + 1, page * 100 + 1) if overflow else range(1, 2)
            return {
                "items": [_raw(n, labels=["status/accepted"]) for n in numbers],
                "incomplete_results": False,
                "total_count": 1001 if overflow else 2,
            }
        return _raw(int(parsed.path.split("/")[5]), labels=["status/accepted"])

    with pytest.raises(ValueError, match="1000-result window" if overflow else "ended early"):
        list(FETCH["scheduled_records"]("microsoft/apm", CONTRACT, runner=runner))


def test_scheduled_counts_and_plan_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry counts skipped candidates without recomputing the final plan."""
    original = FETCH["plan_batch"]
    plans: list[dict] = []

    def planner(batch: dict, contract: dict) -> dict:
        plans.append(batch)
        return original(batch, contract)

    monkeypatch.setitem(FETCH["accumulate_for_plan"].__globals__, "plan_batch", planner)
    batch = _scheduled_batch({1: _raw(1, labels=["status/accepted"]), 2: _raw(2)}, {}, [])
    assert batch["selection_counts"] == {"scanned": 2, "selected": 1}
    assert len(plans) == 2


def test_empty_scheduled_batch_has_zero_counts() -> None:
    """An empty complete search remains distinguishable from a failed read."""
    batch = FETCH["accumulate_for_plan"](
        [], "sweep", ["triage/recommended"], CONTRACT, scheduled_sweep=True
    )
    assert batch["selection_counts"] == {"scanned": 0, "selected": 0}
    assert batch["contexts"] == []
