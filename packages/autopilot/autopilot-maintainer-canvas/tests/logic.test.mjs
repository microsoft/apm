import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
    assertSafeGhArgs,
    assertSpawnAllowed,
    buildSpawnPrompt,
    applyTriageAdvice,
    attachLinkedPrs,
    boardItems,
    canSpawnIssueDelivery,
    classifyIssue,
    classifyPr,
    closingIssueNumbers,
    ghLabelArgs,
    groupOccupancy,
    occupancyFromSessionName,
    occupancyRowsFromCopilotSessions,
    mergeLiveOccupancy,
    sessionTableRows,
    parseTriageConclusion,
    applyLabelPlan,
    planLabelMutation,
    resolveSpawn,
} from "../.apm/extensions/autopilot-maintainer/logic.mjs";

describe("classify lanes", () => {
    it("puts untriaged issues on new, not decide", () => {
        const item = classifyIssue({
            number: 11,
            title: "Fresh",
            labels: ["status/needs-triage"],
        });
        assert.deepEqual(item.lanes, ["new"]);
        assert.equal(item.triageRan, false);
    });

    it("puts advised unaccepted issues on decide", () => {
        const item = classifyIssue({
            number: 12,
            title: "Bug",
            labels: [{ name: "triage/recommended" }, { name: "status/needs-triage" }],
            url: "https://github.com/microsoft/apm/issues/12",
        });
        assert.deepEqual(item.lanes, ["decide"]);
        assert.equal(item.advisory, true);
        assert.equal(item.accepted, false);
        assert.equal(item.needsTriage, true);
    });

    it("puts accepted issues on accepted only", () => {
        const item = classifyIssue({
            number: 13,
            labels: ["status/accepted", "type/bug"],
        });
        assert.deepEqual(item.lanes, ["accepted"]);
    });

    it("splits PR lanes without treating panel-review as accept", () => {
        const item = classifyPr({
            number: 40,
            labels: ["panel-review"],
            isDraft: false,
        });
        assert.equal(item.accepted, false);
        assert.ok(item.lanes.includes("new"));
        assert.equal(item.lanes.includes("accepted"), false);
        assert.equal(item.lanes.includes("decide"), false);
        assert.equal(item.panelReview, true);
    });

    it("puts accepted PRs on accepted even with panel-review", () => {
        const item = classifyPr({
            number: 41,
            labels: ["status/accepted", "panel-review"],
            isDraft: false,
        });
        assert.deepEqual(item.lanes, ["accepted"]);
        assert.equal(item.panelReview, true);
        assert.equal(item.isDraft, false);
    });

    it("puts deferred issues and PRs on deferred only", () => {
        const issue = classifyIssue({
            number: 14,
            labels: ["status/deferred", "triage/recommended"],
        });
        assert.deepEqual(issue.lanes, ["deferred"]);
        assert.equal(issue.deferred, true);
        const pr = classifyPr({
            number: 42,
            labels: ["status/deferred", "status/accepted"],
            isDraft: false,
        });
        assert.deepEqual(pr.lanes, ["deferred"]);
        assert.equal(pr.deferred, true);
    });
});

describe("linked open PRs", () => {
    it("reads closingIssuesReferences and Fixes in the PR body", () => {
        assert.deepEqual(
            closingIssueNumbers({
                closingIssuesReferences: [{ number: 13 }],
                body: "Fixes #13 and also Closes #22",
            }),
            [13, 22],
        );
    });

    it("hides delivery when an accepted issue already has an open PR", () => {
        const issue = classifyIssue({
            number: 13,
            labels: ["status/accepted"],
        });
        const pr = classifyPr({
            number: 41,
            title: "Implement 13",
            url: "https://github.com/microsoft/apm/pull/41",
            labels: ["status/accepted"],
            isDraft: false,
            closingIssuesReferences: [{ number: 13 }],
        });
        attachLinkedPrs([issue], [pr]);
        assert.equal(issue.hasOpenPr, true);
        assert.equal(issue.linkedPrs[0].number, 41);
        assert.equal(issue.linkedPrs[0].accepted, true);
        assert.equal(issue.linkedPrs[0].isDraft, false);
        assert.equal(canSpawnIssueDelivery(issue), false);
        const bare = classifyIssue({ number: 14, labels: ["status/accepted"] });
        attachLinkedPrs([bare], [pr]);
        assert.equal(canSpawnIssueDelivery(bare), true);
    });

    it("drops a PR that already sits on a listed issue", () => {
        const issue = classifyIssue({
            number: 2997,
            labels: ["status/accepted"],
        });
        const linked = classifyPr({
            number: 2949,
            labels: [],
            closingIssuesReferences: [{ number: 2997 }],
        });
        const community = classifyPr({
            number: 88,
            labels: ["status/accepted"],
            closingIssuesReferences: [],
        });
        attachLinkedPrs([issue], [linked, community]);
        const items = boardItems([issue], [linked, community]);
        assert.equal(items.some((item) => item.kind === "pr" && item.number === 2949), false);
        assert.equal(items.some((item) => item.kind === "pr" && item.number === 88), true);
        assert.equal(items.find((item) => item.kind === "issue").linkedPrs[0].number, 2949);
    });
});

describe("label mutations", () => {
    it("accepts by adding status/accepted and clearing deferred", () => {
        const plan = planLabelMutation({
            kind: "issue",
            number: 9,
            action: "accept",
            currentLabels: ["status/deferred", "status/needs-triage"],
        });
        assert.deepEqual(plan.add, ["status/accepted"]);
        assert.deepEqual(plan.remove, ["status/deferred", "status/needs-triage"]);
        const args = ghLabelArgs(plan);
        assert.equal(args[0], "issue");
        assert.equal(args[1], "edit");
        assert.ok(args.includes("--add-label"));
        assert.equal(args.includes("--add-assignee"), false);
    });

    it("refuses defer of accepted without confirm", () => {
        assert.throws(
            () => planLabelMutation({
                kind: "issue",
                number: 9,
                action: "defer",
                currentLabels: ["status/accepted"],
            }),
            /confirmClearAccepted/,
        );
    });

    it("re-applies panel-review on PRs only", () => {
        const plan = planLabelMutation({
            kind: "pr",
            number: 8,
            action: "panel-review",
            currentLabels: ["panel-review"],
        });
        assert.deepEqual(plan.remove, ["panel-review"]);
        assert.deepEqual(plan.add, ["panel-review"]);
        assert.throws(
            () => planLabelMutation({
                kind: "issue",
                number: 8,
                action: "panel-review",
                currentLabels: [],
            }),
            /pull requests only/,
        );
    });

    it("moves an accepted issue off decide without a GitHub refetch", () => {
        const item = classifyIssue({
            number: 12,
            title: "Bug",
            labels: ["triage/recommended", "status/needs-triage"],
        });
        const plan = planLabelMutation({
            kind: "issue",
            number: 12,
            action: "accept",
            currentLabels: item.labels,
        });
        const next = applyLabelPlan(item, plan);
        assert.equal(next.accepted, true);
        assert.deepEqual(next.lanes, ["accepted"]);
        assert.equal(next.needsTriage, false);
    });
});

describe("spawn contract", () => {
    it("names workers with Domain stage #n and keeps merge behind confirm", () => {
        const worker = resolveSpawn("worker-issue-delivery", 2902);
        assert.equal(worker.sessionName, "Issue delivery #2902");
        assert.equal(worker.skill, "autopilot-issue-delivery-worker");
        const merge = resolveSpawn("worker-pr-merge", 2741);
        assert.equal(merge.sessionName, "PR merge #2741");
        const occupancy = new Map();
        assert.throws(() => assertSpawnAllowed(merge, occupancy, { confirm: false }), /confirm/);
        assertSpawnAllowed(merge, occupancy, { confirm: true });
    });

    it("refuses a second spawn for the same target", () => {
        const sweep = resolveSpawn("sweep-pr-review");
        const occupancy = new Map([[sweep.target, { busy: true, name: sweep.sessionName }]]);
        assert.throws(() => assertSpawnAllowed(sweep, occupancy), /already working/);
    });

    it("allows spawn when the previous session is idle", () => {
        const sweep = resolveSpawn("sweep-pr-review");
        const occupancy = new Map([[sweep.target, { busy: false, activity: "idle", name: sweep.sessionName }]]);
        assertSpawnAllowed(sweep, occupancy);
    });

    it("asks the parent to create_session and never to merge or assign", () => {
        const sweep = resolveSpawn("sweep-issue-triage");
        const prompt = buildSpawnPrompt(sweep, "autopilot-maintainer");
        assert.match(prompt, /^action: spawn-isolated-session$/m);
        assert.match(prompt, /^tool: create_session$/m);
        assert.match(prompt, /^detached: false$/m);
        assert.match(prompt, /^coordinate_with_creator: false$/m);
        assert.match(prompt, /^skill: autopilot-issue-triage-scheduler$/m);
        assert.match(prompt, /forbid: assign, request-reviewers, merge, labels/);
        assert.equal(/gh pr merge/.test(prompt), false);
        assert.equal(/add-reviewer/.test(prompt), false);
        assert.equal(/Spawn an isolated/.test(prompt), false);
    });

    it("never composes merge from the review scheduler kickoff", () => {
        const sweep = resolveSpawn("sweep-pr-review");
        assert.match(sweep.kickoff, /compose_merge: never/);
    });

    it("starts the merge worker in plan mode", () => {
        const merge = resolveSpawn("worker-pr-merge", 2610);
        const prompt = buildSpawnPrompt(merge, "autopilot-maintainer");
        assert.match(prompt, /^kickoff_mode: plan$/m);
        assert.match(merge.kickoff, /^plan_first: yes$/m);
        assert.match(merge.kickoff, /^auto_merge: no$/m);
        const sweep = resolveSpawn("sweep-issue-triage");
        assert.match(
            buildSpawnPrompt(sweep, "autopilot-maintainer"),
            /^kickoff_mode: interactive$/m,
        );
    });

    it("asks the review worker for panel-mode full on first advisory", () => {
        const worker = resolveSpawn("worker-pr-review", 2610);
        assert.match(worker.kickoff, /^panel-mode: full$/m);
        assert.match(worker.kickoff, /^invocation_mode: session-review$/m);
    });

    it("routes triage comments through autopilot-comment", () => {
        const issueWorker = resolveSpawn("worker-issue-triage", 3017);
        assert.match(issueWorker.kickoff, /^comment_via: autopilot-comment$/m);
        assert.match(issueWorker.kickoff, /^debug: off$/m);
        const prWorker = resolveSpawn("worker-pr-triage", 3011);
        assert.match(prWorker.kickoff, /^comment_via: autopilot-comment$/m);
        const issueSweep = resolveSpawn("sweep-issue-triage");
        assert.match(issueSweep.kickoff, /^worker_comment_via: autopilot-comment$/m);
        const prSweep = resolveSpawn("sweep-pr-triage");
        assert.match(prSweep.kickoff, /^worker_comment_via: autopilot-comment$/m);
    });

    it("keeps scheduler write off and workers write on", () => {
        const sweep = resolveSpawn("sweep-issue-triage");
        assert.match(sweep.kickoff, /^write: off$/m);
        assert.match(sweep.kickoff, /^worker_write: on$/m);
        const worker = resolveSpawn("worker-issue-triage", 12);
        assert.match(worker.kickoff, /^write: on$/m);
        assert.match(worker.kickoff, /^issue: 12$/m);
        assert.equal(/^write: off$/m.test(worker.kickoff), false);
    });
});

describe("triage conclusions", () => {
    it("parses the issue recommendation heading, not the human label", () => {
        const parsed = parseTriageConclusion([
            "<!-- apm-triage-advisory:v2 target=issue#12 watermark=1 -->",
            "## Triage recommendation",
            "",
            "accept",
            "",
            "**Recommendation only, not scope approval.**",
        ].join("\n"));
        assert.equal(parsed.ran, true);
        assert.equal(parsed.conclusion, "accept");
    });

    it("parses PR triage separately from issue headings", () => {
        const parsed = parseTriageConclusion([
            "## PR triage recommendation",
            "",
            "needs-issue",
        ].join("\n"));
        assert.equal(parsed.conclusion, "needs-issue");
    });

    it("uses the latest advisory comment", () => {
        const item = applyTriageAdvice(
            classifyIssue({ number: 12, labels: ["triage/recommended"] }),
            [
                { body: "## Triage recommendation\n\ndefer-later\n" },
                { body: "## Triage recommendation\n\nneeds-design\n" },
            ],
        );
        assert.equal(item.triageRan, true);
        assert.equal(item.triageConclusion, "needs-design");
        assert.deepEqual(item.lanes, ["decide"]);
    });

    it("moves a comment-only triage from new to decide", () => {
        const item = applyTriageAdvice(
            classifyIssue({ number: 14, labels: [] }),
            [{ body: "## Triage recommendation\n\naccept\n" }],
        );
        assert.equal(item.triageRan, true);
        assert.deepEqual(item.lanes, ["decide"]);
    });

    it("keeps deferred items on deferred after triage comments", () => {
        const item = applyTriageAdvice(
            classifyIssue({ number: 15, labels: ["status/deferred"] }),
            [{ body: "## Triage recommendation\n\ndefer-later\n" }],
        );
        assert.equal(item.triageRan, true);
        assert.deepEqual(item.lanes, ["deferred"]);
    });
});

describe("occupancy from Copilot session names", () => {
    it("maps scheduler and worker names and ignores archived or unrelated titles", () => {
        assert.deepEqual(occupancyFromSessionName("Issue triage scheduler"), {
            target: "scheduler:issue-triage",
            name: "Issue triage scheduler",
            kind: "scheduler",
        });
        assert.deepEqual(occupancyFromSessionName("PR triage #2793"), {
            target: "worker:pr-triage:2793",
            name: "PR triage #2793",
            kind: "worker",
        });
        assert.equal(occupancyFromSessionName("Triage issue 2972"), null);
        assert.equal(occupancyFromSessionName("#2990 issue-triage-worker"), null);
        const rows = occupancyRowsFromCopilotSessions([
            { name: "PR triage #12", sessionId: "sess-1", isRunning: true },
            { name: "Issue triage scheduler", sessionId: "sess-2", isRunning: false },
            { name: "PR triage #99", sessionId: "gone", isRunning: false, archivedAt: "2026-09-17T15:14:00Z" },
            { name: "Random chat", sessionId: "nope", isRunning: true },
        ]);
        assert.equal(rows.length, 2);
        assert.equal(rows[0].activity, "working");
        assert.equal(rows[1].activity, "idle");
        assert.equal(rows[1].target, "scheduler:issue-triage");
    });

    it("drops stalled pending rows when Copilot no longer lists the session", () => {
        const occupancy = new Map([
            ["scheduler:issue-triage", { pending: true, name: "Issue triage scheduler", busy: true, activity: "pending" }],
            ["worker:pr-triage:1", { pending: false, name: "stale", busy: true, activity: "idle" }],
        ]);
        mergeLiveOccupancy(occupancy, [
            { target: "worker:pr-triage:12", activity: "idle", name: "PR triage #12", sessionId: "live" },
        ]);
        assert.equal(occupancy.has("worker:pr-triage:1"), false);
        assert.equal(occupancy.has("scheduler:issue-triage"), false);
        assert.equal(occupancy.get("worker:pr-triage:12").sessionId, "live");
    });
});

describe("occupancy grouping", () => {
    it("nests workers under the matching scheduler family", () => {
        const groups = groupOccupancy([
            { target: "worker:issue-triage:12", name: "Issue triage #12", skill: "autopilot-issue-triage-worker" },
            { target: "scheduler:issue-triage", name: "Issue triage scheduler", skill: "autopilot-issue-triage-scheduler" },
            { target: "worker:pr-merge:40", name: "PR merge #40", skill: "autopilot-pr-merge-worker" },
        ]);
        assert.equal(groups[0].family, "issue-triage");
        assert.equal(groups[0].scheduler.name, "Issue triage scheduler");
        assert.equal(groups[0].workers[0].name, "Issue triage #12");
        assert.equal(groups[1].family, "pr-merge");
        assert.equal(groups[1].scheduler, null);
        assert.equal(groups[1].workers[0].name, "PR merge #40");
    });

    it("labels parent/child rows from creator_session_id", () => {
        const canvasId = "11111111-1111-4111-8111-111111111111";
        const schedulerId = "22222222-2222-4222-8222-222222222222";
        const groups = groupOccupancy([
            {
                target: "scheduler:issue-triage",
                name: "Issue triage scheduler",
                sessionId: schedulerId,
                creatorSessionId: canvasId,
            },
            {
                target: "worker:issue-triage:12",
                name: "Issue triage #12",
                sessionId: "33333333-3333-4333-8333-333333333333",
                creatorSessionId: schedulerId,
            },
            {
                target: "worker:pr-merge:40",
                name: "PR merge #40",
                sessionId: "44444444-4444-4444-8444-444444444444",
                creatorSessionId: canvasId,
            },
        ]);
        const rows = sessionTableRows(groups, { canvasSessionId: canvasId });
        assert.equal(rows[0].role, "scheduler");
        assert.equal(rows[0].parentName, "this canvas");
        assert.equal(rows[0].childCount, 1);
        assert.equal(rows[1].role, "worker");
        assert.equal(rows[1].child, true);
        assert.equal(rows[1].parentName, "Issue triage scheduler");
        assert.equal(rows[2].role, "worker");
        assert.equal(rows[2].child, false);
        assert.equal(rows[2].parentName, "this canvas");
    });
});

describe("gh safety", () => {
    it("rejects assignee reviewer and merge flags", () => {
        assert.throws(() => assertSafeGhArgs(["issue", "edit", "1", "--add-assignee", "@me"]), /forbidden/);
        assert.throws(() => assertSafeGhArgs(["pr", "edit", "1", "--add-reviewer", "me"]), /forbidden/);
        assert.throws(() => assertSafeGhArgs(["pr", "merge", "1"]), /forbidden/);
    });
});
