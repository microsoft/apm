// Test suite for issue-monitor canvas extension
// Run: node --test tests/logic.test.mjs

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
    classifyIssue,
    classifyPrStatus,
    escapeHtml,
    matchPrsToIssues,
    classifyPipeline,
    classifyPanel,
    classifyPrForTable,
    parsePanelCounts,
    parsePanelReview,
    extractFollowUpItems,
} from "../logic.mjs";

// -- classifyIssue --

describe("classifyIssue", () => {
    const base = {
        number: 42,
        title: "Test issue",
        labels: [],
        author: { login: "octocat" },
        url: "https://github.com/microsoft/apm/issues/42",
    };

    it("defaults to chore/P2 when no labels", () => {
        const result = classifyIssue(base);
        assert.equal(result.type, "chore");
        assert.equal(result.priority, "P2");
        assert.equal(result.status, "available");
        assert.equal(result.number, 42);
        assert.equal(result.author, "octocat");
    });

    it("classifies bug type from label", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "bug" }],
        });
        assert.equal(result.type, "bug");
    });

    it("classifies bug type case-insensitively", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "Bug" }],
        });
        assert.equal(result.type, "bug");
    });

    it("classifies feature type from 'feature' label", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "feature" }],
        });
        assert.equal(result.type, "feature");
    });

    it("classifies feature type from 'enhancement' label", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "enhancement" }],
        });
        assert.equal(result.type, "feature");
    });

    it("bug takes precedence over feature when both present", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "bug" }, { name: "feature" }],
        });
        assert.equal(result.type, "bug");
    });

    it("classifies P0 from priority/critical", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/critical" }],
        });
        assert.equal(result.priority, "P0");
    });

    it("classifies P0 from priority/0", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/0" }],
        });
        assert.equal(result.priority, "P0");
    });

    it("classifies P1 from priority/high", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/high" }],
        });
        assert.equal(result.priority, "P1");
    });

    it("classifies P1 from priority/1", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/1" }],
        });
        assert.equal(result.priority, "P1");
    });

    it("classifies P3 from priority/low", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/low" }],
        });
        assert.equal(result.priority, "P3");
    });

    it("keeps P2 for priority/medium (no special case)", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "priority/medium" }],
        });
        assert.equal(result.priority, "P2");
    });

    it("truncates title to 90 chars", () => {
        const longTitle = "A".repeat(120);
        const result = classifyIssue({ ...base, title: longTitle });
        assert.equal(result.title.length, 90);
    });

    it("handles missing author gracefully", () => {
        const result = classifyIssue({ ...base, author: null });
        assert.equal(result.author, "unknown");
    });

    it("handles undefined author gracefully", () => {
        const result = classifyIssue({ ...base, author: undefined });
        assert.equal(result.author, "unknown");
    });

    it("preserves labels array", () => {
        const result = classifyIssue({
            ...base,
            labels: [{ name: "bug" }, { name: "priority/high" }],
        });
        assert.deepEqual(result.labels, ["bug", "priority/high"]);
    });
});

// -- classifyPrStatus --

describe("classifyPrStatus", () => {
    it("returns 'draft' for draft PRs", () => {
        assert.equal(
            classifyPrStatus({ isDraft: true, statusCheckRollup: [], reviewDecision: "" }),
            "draft"
        );
    });

    it("draft takes precedence over everything", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: true,
                statusCheckRollup: [{ conclusion: "FAILURE" }],
                reviewDecision: "APPROVED",
            }),
            "draft"
        );
    });

    it("returns 'ci-failing' when checks fail", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "FAILURE" }],
                reviewDecision: "",
            }),
            "ci-failing"
        );
    });

    it("returns 'ci-failing' for ERROR conclusion", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "ERROR" }],
                reviewDecision: "",
            }),
            "ci-failing"
        );
    });

    it("returns 'ci-failing' for CANCELLED conclusion", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "CANCELLED" }],
                reviewDecision: "",
            }),
            "ci-failing"
        );
    });

    it("ci-failing takes precedence over changes-requested", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "FAILURE" }],
                reviewDecision: "CHANGES_REQUESTED",
            }),
            "ci-failing"
        );
    });

    it("returns 'changes-requested' when review requests changes", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "SUCCESS" }],
                reviewDecision: "CHANGES_REQUESTED",
            }),
            "changes-requested"
        );
    });

    it("returns 'ready-to-merge' when approved and checks pass", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "SUCCESS" }],
                reviewDecision: "APPROVED",
            }),
            "ready-to-merge"
        );
    });

    it("returns 'ci-pending' when approved but checks pending", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ status: "IN_PROGRESS" }],
                reviewDecision: "APPROVED",
            }),
            "ci-pending"
        );
    });

    it("returns 'ci-pending' for QUEUED status", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ status: "QUEUED" }],
                reviewDecision: "",
            }),
            "ci-pending"
        );
    });

    it("returns 'ci-pending' for PENDING status", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ status: "PENDING" }],
                reviewDecision: "",
            }),
            "ci-pending"
        );
    });

    it("returns 'review-pending' with no review and passing checks", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [{ conclusion: "SUCCESS" }],
                reviewDecision: "",
            }),
            "review-pending"
        );
    });

    it("returns 'review-pending' with empty checks and no review", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [],
                reviewDecision: "",
            }),
            "review-pending"
        );
    });

    it("returns 'review-pending' with null/undefined statusCheckRollup", () => {
        assert.equal(
            classifyPrStatus({ isDraft: false, reviewDecision: "" }),
            "review-pending"
        );
    });

    it("returns 'ready-to-merge' with approved and no checks at all", () => {
        assert.equal(
            classifyPrStatus({
                isDraft: false,
                statusCheckRollup: [],
                reviewDecision: "APPROVED",
            }),
            "ready-to-merge"
        );
    });
});

// -- escapeHtml --

describe("escapeHtml", () => {
    it("escapes ampersands", () => {
        assert.equal(escapeHtml("a & b"), "a &amp; b");
    });

    it("escapes angle brackets", () => {
        assert.equal(escapeHtml("<script>"), "&lt;script&gt;");
    });

    it("escapes double quotes", () => {
        assert.equal(escapeHtml('a "b" c'), "a &quot;b&quot; c");
    });

    it("handles all special chars together", () => {
        assert.equal(escapeHtml('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;");
    });

    it("converts numbers to strings", () => {
        assert.equal(escapeHtml(42), "42");
    });

    it("handles empty string", () => {
        assert.equal(escapeHtml(""), "");
    });

    it("passes through plain text unchanged", () => {
        assert.equal(escapeHtml("hello world"), "hello world");
    });
});

// -- matchPrsToIssues --

describe("matchPrsToIssues", () => {
    function makeIssue(number) {
        return { number, title: "Issue #" + number, type: "bug", priority: "P2", author: "user", status: "available", url: "https://github.com/microsoft/apm/issues/" + number };
    }

    function makePr(number, title, body, opts) {
        opts = opts || {};
        return {
            number,
            title,
            body: body || "",
            url: "https://github.com/microsoft/apm/pull/" + number,
            state: "OPEN",
            isDraft: opts.isDraft || false,
            reviewDecision: opts.reviewDecision || "",
            statusCheckRollup: opts.statusCheckRollup || [],
        };
    }

    it("matches PR to issue by title reference", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix #100: some bug", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 200);
        assert.equal(issues[0].pr.prStatus, "review-pending");
    });

    it("matches PR to issue by body reference", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix a bug", "Closes #100")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 200);
    });

    it("does not match partial number (#10 should not match #100)", () => {
        const issues = [makeIssue(10)];
        const prs = [makePr(200, "Fix issue #100", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr, undefined);
    });

    it("does not match issue number without # prefix", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix issue 100", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr, undefined);
    });

    it("matches when # is at start of title", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "#100 fix", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 200);
    });

    it("matches when # is at end of body", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix", "relates to #100")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 200);
    });

    it("first matching PR wins (does not overwrite)", () => {
        const issues = [makeIssue(100)];
        const prs = [
            makePr(200, "Fix #100 first", ""),
            makePr(201, "Fix #100 second", ""),
        ];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 200);
    });

    it("matches multiple issues to different PRs", () => {
        const issues = [makeIssue(100), makeIssue(200)];
        const prs = [
            makePr(300, "Fix #100", ""),
            makePr(301, "Fix #200", ""),
        ];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 300);
        assert.equal(issues[1].pr.number, 301);
    });

    it("leaves issues without matching PR untouched", () => {
        const issues = [makeIssue(100), makeIssue(200)];
        const prs = [makePr(300, "Fix #100", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 300);
        assert.equal(issues[1].pr, undefined);
    });

    it("sets correct prStatus for draft PR", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix #100", "", { isDraft: true })];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.prStatus, "draft");
    });

    it("sets correct prStatus for approved PR", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix #100", "", {
            reviewDecision: "APPROVED",
            statusCheckRollup: [{ conclusion: "SUCCESS" }],
        })];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.prStatus, "ready-to-merge");
    });

    it("sets correct prStatus for failing CI", () => {
        const issues = [makeIssue(100)];
        const prs = [makePr(200, "Fix #100", "", {
            statusCheckRollup: [{ conclusion: "FAILURE" }],
        })];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.prStatus, "ci-failing");
    });

    it("handles empty PR list gracefully", () => {
        const issues = [makeIssue(100)];
        matchPrsToIssues(issues, []);
        assert.equal(issues[0].pr, undefined);
    });

    it("handles empty issue list gracefully", () => {
        const prs = [makePr(200, "Fix #100", "")];
        const result = matchPrsToIssues([], prs);
        assert.deepEqual(result, []);
    });

    it("preserves existing pr field on issue (no overwrite)", () => {
        const issues = [makeIssue(100)];
        issues[0].pr = { number: 999, url: "existing", state: "OPEN", prStatus: "draft" };
        const prs = [makePr(200, "Fix #100", "")];
        matchPrsToIssues(issues, prs);
        assert.equal(issues[0].pr.number, 999);
    });
});

// -- classifyPipeline --

describe("classifyPipeline", () => {
    it("returns none for empty checks", () => {
        assert.deepEqual(classifyPipeline([]), { status: "none", label: "No CI" });
    });

    it("returns none for null checks", () => {
        assert.deepEqual(classifyPipeline(null), { status: "none", label: "No CI" });
    });

    it("returns green when all checks pass", () => {
        const checks = [
            { conclusion: "SUCCESS", status: "COMPLETED" },
            { conclusion: "SUCCESS", status: "COMPLETED" },
        ];
        assert.deepEqual(classifyPipeline(checks), { status: "green", label: "Passing" });
    });

    it("returns red when any check fails", () => {
        const checks = [
            { conclusion: "SUCCESS", status: "COMPLETED" },
            { conclusion: "FAILURE", status: "COMPLETED" },
        ];
        assert.deepEqual(classifyPipeline(checks), { status: "red", label: "Failing" });
    });

    it("returns red for ERROR conclusion", () => {
        const checks = [{ conclusion: "ERROR", status: "COMPLETED" }];
        assert.deepEqual(classifyPipeline(checks), { status: "red", label: "Failing" });
    });

    it("returns red for CANCELLED conclusion", () => {
        const checks = [{ conclusion: "CANCELLED", status: "COMPLETED" }];
        assert.deepEqual(classifyPipeline(checks), { status: "red", label: "Failing" });
    });

    it("returns yellow when checks are pending", () => {
        const checks = [
            { conclusion: "SUCCESS", status: "COMPLETED" },
            { status: "IN_PROGRESS" },
        ];
        assert.deepEqual(classifyPipeline(checks), { status: "yellow", label: "Running" });
    });

    it("returns yellow for QUEUED status", () => {
        const checks = [{ status: "QUEUED" }];
        assert.deepEqual(classifyPipeline(checks), { status: "yellow", label: "Running" });
    });

    it("red takes precedence over yellow", () => {
        const checks = [
            { conclusion: "FAILURE", status: "COMPLETED" },
            { status: "IN_PROGRESS" },
        ];
        assert.deepEqual(classifyPipeline(checks), { status: "red", label: "Failing" });
    });

    it("ignores SKIPPED checks (treated as success)", () => {
        const checks = [
            { conclusion: "SUCCESS", status: "COMPLETED" },
            { conclusion: "SKIPPED", status: "COMPLETED" },
        ];
        assert.deepEqual(classifyPipeline(checks), { status: "green", label: "Passing" });
    });

    it("returns orange when workflow runs have action_required", () => {
        const checks = [];
        const runs = [{ conclusion: "action_required", status: "completed" }];
        assert.deepEqual(classifyPipeline(checks, runs), { status: "orange", label: "Awaiting Approval" });
    });

    it("orange takes precedence over no checks", () => {
        const runs = [
            { conclusion: "action_required", status: "completed" },
            { conclusion: "action_required", status: "completed" },
        ];
        assert.deepEqual(classifyPipeline(null, runs), { status: "orange", label: "Awaiting Approval" });
    });

    it("orange takes precedence over green checks", () => {
        const checks = [{ conclusion: "SUCCESS", status: "COMPLETED" }];
        const runs = [{ conclusion: "action_required", status: "completed" }];
        assert.deepEqual(classifyPipeline(checks, runs), { status: "orange", label: "Awaiting Approval" });
    });

    it("does not return orange when no action_required runs", () => {
        const checks = [{ conclusion: "SUCCESS", status: "COMPLETED" }];
        const runs = [{ conclusion: "success", status: "completed" }];
        assert.deepEqual(classifyPipeline(checks, runs), { status: "green", label: "Passing" });
    });

    it("handles undefined workflowRuns gracefully", () => {
        const checks = [{ conclusion: "SUCCESS", status: "COMPLETED" }];
        assert.deepEqual(classifyPipeline(checks, undefined), { status: "green", label: "Passing" });
    });
});

// -- classifyPanel --

describe("classifyPanel", () => {
    it("returns none for empty labels", () => {
        assert.deepEqual(classifyPanel([]), { status: "none", label: "Not requested" });
    });

    it("returns none for null labels", () => {
        assert.deepEqual(classifyPanel(null), { status: "none", label: "Not requested" });
    });

    it("returns none when no panel-related labels", () => {
        assert.deepEqual(classifyPanel([{ name: "bug" }]), { status: "none", label: "Not requested" });
    });

    it("returns yellow when panel-review label present", () => {
        assert.deepEqual(classifyPanel([{ name: "panel-review" }]), { status: "yellow", label: "Requested" });
    });

    it("returns green when both panel-review and status/accepted", () => {
        const labels = [{ name: "panel-review" }, { name: "status/accepted" }];
        assert.deepEqual(classifyPanel(labels), { status: "green", label: "Accepted" });
    });

    it("handles string labels", () => {
        assert.deepEqual(classifyPanel(["panel-review"]), { status: "yellow", label: "Requested" });
    });

    it("handles mixed label formats", () => {
        assert.deepEqual(classifyPanel(["panel-review", { name: "status/accepted" }]),
            { status: "green", label: "Accepted" });
    });
});

// -- classifyPrForTable --

describe("classifyPrForTable", () => {
    function makePr(overrides) {
        return {
            number: 100,
            title: "Test PR",
            url: "https://github.com/microsoft/apm/pull/100",
            author: { login: "dev" },
            isDraft: false,
            reviewDecision: "",
            statusCheckRollup: [{ conclusion: "SUCCESS", status: "COMPLETED" }],
            labels: [],
            headRefName: "feat/test",
            ...overrides,
        };
    }

    it("extracts basic fields", () => {
        const result = classifyPrForTable(makePr({}));
        assert.equal(result.number, 100);
        assert.equal(result.title, "Test PR");
        assert.equal(result.author, "dev");
        assert.equal(result.branch, "feat/test");
    });

    it("truncates title to 90 chars", () => {
        const long = "A".repeat(120);
        const result = classifyPrForTable(makePr({ title: long }));
        assert.equal(result.title.length, 90);
    });

    it("classifies pipeline status", () => {
        const result = classifyPrForTable(makePr({}));
        assert.equal(result.pipeline.status, "green");
        assert.equal(result.pipeline.label, "Passing");
    });

    it("classifies pipeline as red on failure", () => {
        const result = classifyPrForTable(makePr({
            statusCheckRollup: [{ conclusion: "FAILURE", status: "COMPLETED" }],
        }));
        assert.equal(result.pipeline.status, "red");
    });

    it("classifies panel from labels", () => {
        const result = classifyPrForTable(makePr({
            labels: [{ name: "panel-review" }],
        }));
        assert.equal(result.panel.status, "yellow");
        assert.equal(result.panel.label, "Requested");
    });

    it("classifies review status", () => {
        const result = classifyPrForTable(makePr({ reviewDecision: "APPROVED" }));
        assert.equal(result.prStatus, "ready-to-merge");
    });

    it("classifies draft PR", () => {
        const result = classifyPrForTable(makePr({ isDraft: true }));
        assert.equal(result.prStatus, "draft");
        assert.equal(result.isDraft, true);
    });

    it("handles string author", () => {
        const result = classifyPrForTable(makePr({ author: "stringUser" }));
        assert.equal(result.author, "stringUser");
    });

    it("handles missing author", () => {
        const result = classifyPrForTable(makePr({ author: undefined }));
        assert.equal(result.author, "unknown");
    });

    it("extracts label names from objects", () => {
        const result = classifyPrForTable(makePr({
            labels: [{ name: "bug" }, { name: "panel-review" }],
        }));
        assert.deepEqual(result.labels, ["bug", "panel-review"]);
    });
});

// -- parsePanelCounts --

describe("parsePanelCounts", () => {
    const panelComment = `## APM Review Panel
| Persona | B | R | N | Takeaway |
|---|---|---|---|---|
| Python Architect | 0 | 1 | 3 | Some text |
| CLI Logging Expert | 0 | 2 | 2 | Some text |
| Supply Chain Security Expert | 1 | 3 | 1 | Some text |
| Test Coverage Expert | 0 | 1 | 0 | Some text |`;

    it("parses B/R/N totals from panel comment body", () => {
        const result = parsePanelCounts([{ body: panelComment }]);
        assert.deepEqual(result, { b: 1, r: 7, n: 6 });
    });

    it("returns null when no comments", () => {
        assert.equal(parsePanelCounts([]), null);
        assert.equal(parsePanelCounts(null), null);
    });

    it("returns null when no panel table in comments", () => {
        assert.equal(parsePanelCounts([{ body: "just a regular comment" }]), null);
    });

    it("handles string comments", () => {
        const result = parsePanelCounts([panelComment]);
        assert.deepEqual(result, { b: 1, r: 7, n: 6 });
    });

    it("finds panel comment among multiple comments", () => {
        const result = parsePanelCounts([
            { body: "LGTM" },
            { body: panelComment },
            { body: "Thanks!" },
        ]);
        assert.deepEqual(result, { b: 1, r: 7, n: 6 });
    });

    it("returns null for table with zero totals", () => {
        const empty = `| Persona | B | R | N | Takeaway |
|---|---|---|---|---|`;
        assert.equal(parsePanelCounts([{ body: empty }]), null);
    });

    it("handles all-zero rows correctly", () => {
        const allZero = `| Persona | B | R | N | Takeaway |
|---|---|---|---|---|
| Expert A | 0 | 0 | 1 | Nit only |`;
        const result = parsePanelCounts([{ body: allZero }]);
        assert.deepEqual(result, { b: 0, r: 0, n: 1 });
    });
});

// -- parsePanelReview --

const SAMPLE_PANEL_COMMENT = `## APM Review Panel: \`ship_with_followups\`

> PR #1763 now aligns check with per-entry effective host routing.

cc @leocamello -- advisory pass ready.

### Panel summary

| Persona | B | R | N | Takeaway |
|---|---|---|---|---|
| Python Architect | 0 | 1 | 0 | Shared helper kills drift. |
| CLI Logging Expert | 0 | 0 | 2 | Cosmetic only. |
| Supply Chain Security Expert | 0 | 0 | 1 | Token handling safe. |

> B = blocking-severity findings, R = recommended, N = nits.

### Recommendation

Ship after maintainer review. The PR is mergeable.

### Deferred (out-of-scope follow-ups)

- (panel) Add shared validation for path segments.

### Folded in this run

- (panel) Move changelog entry -- resolved in 1b8efa3.
- (panel) Reuse AuthResolver -- resolved in 1b8efa3.

### CI

All checks successful.

### Convergence

2 outer iteration(s); 2 Copilot round(s). Final panel verdict: ship_with_followups.`;

describe("parsePanelReview", () => {
    it("returns null for null/undefined input", () => {
        assert.equal(parsePanelReview(null), null);
        assert.equal(parsePanelReview(undefined), null);
        assert.equal(parsePanelReview([]), null);
    });

    it("returns null when no panel review comment exists", () => {
        const result = parsePanelReview([{ body: "Just a regular comment" }]);
        assert.equal(result, null);
    });

    it("extracts verdict from header", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT, author: { login: "panel-bot" }, createdAt: "2025-01-15T10:00:00Z" }]);
        assert.equal(result.verdict, "ship_with_followups");
    });

    it("extracts summary blockquote", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.summary.includes("aligns check"));
    });

    it("parses all personas with correct B/R/N", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.equal(result.personas.length, 3);
        assert.equal(result.personas[0].name, "Python Architect");
        assert.equal(result.personas[0].b, 0);
        assert.equal(result.personas[0].r, 1);
        assert.equal(result.personas[0].n, 0);
        assert.equal(result.personas[1].name, "CLI Logging Expert");
        assert.equal(result.personas[1].n, 2);
        assert.equal(result.personas[2].name, "Supply Chain Security Expert");
    });

    it("computes correct totals", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.deepEqual(result.totals, { b: 0, r: 1, n: 3 });
    });

    it("extracts recommendation section", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.recommendation.includes("Ship after maintainer review"));
    });

    it("extracts deferred section", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.deferred.includes("shared validation"));
    });

    it("extracts folded section", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.folded.includes("Move changelog"));
        assert.ok(result.folded.includes("Reuse AuthResolver"));
    });

    it("extracts CI section", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.ci.includes("All checks successful"));
    });

    it("extracts convergence", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT }]);
        assert.ok(result.convergence.includes("2 outer iteration"));
    });

    it("preserves author and createdAt", () => {
        const result = parsePanelReview([{ body: SAMPLE_PANEL_COMMENT, author: { login: "copilot-panel" }, createdAt: "2025-06-15T12:00:00Z" }]);
        assert.equal(result.author, "copilot-panel");
        assert.equal(result.createdAt, "2025-06-15T12:00:00Z");
    });

    it("uses the LAST panel review comment when multiple exist", () => {
        const older = `## APM Review Panel: \`do_not_ship\`\n\n> Old review.\n\n### Panel summary\n\n| Persona | B | R | N | Takeaway |\n|---|---|---|---|---|\n| Architect | 2 | 0 | 0 | Blocker found. |\n\n### Recommendation\n\nDo not ship.`;
        const result = parsePanelReview([
            { body: older, createdAt: "2025-01-01T00:00:00Z" },
            { body: SAMPLE_PANEL_COMMENT, createdAt: "2025-01-15T00:00:00Z" },
        ]);
        assert.equal(result.verdict, "ship_with_followups");
        assert.equal(result.personas.length, 3);
    });

    it("handles ship verdict", () => {
        const shipComment = `## APM Review Panel: \`ship\`\n\n> Clean.\n\n### Panel summary\n\n| Persona | B | R | N | Takeaway |\n|---|---|---|---|---|\n| Architect | 0 | 0 | 0 | All good. |\n\n### Recommendation\n\nShip.`;
        const result = parsePanelReview([{ body: shipComment }]);
        assert.equal(result.verdict, "ship");
        assert.deepEqual(result.totals, { b: 0, r: 0, n: 0 });
    });
});

// -- extractFollowUpItems --

describe("extractFollowUpItems", () => {
    it("returns empty array for null input", () => {
        assert.deepEqual(extractFollowUpItems(null, 100), []);
    });

    it("extracts deferred items as follow-ups", () => {
        const review = {
            deferred: "- Add retry logic to the HTTP client\n- Improve error messages for auth failures",
        };
        const items = extractFollowUpItems(review, 42);
        assert.equal(items.length, 2);
        assert.ok(items[0].title.includes("Add retry logic"));
        assert.ok(items[0].body.includes("PR #42"));
        assert.ok(items[0].body.includes("Deferred"));
        assert.deepEqual(items[0].labels, ["follow-up"]);
        assert.ok(items[1].title.includes("Improve error messages"));
    });

    it("extracts recommendation items as follow-ups", () => {
        const review = {
            recommendation: "- Consider adding input validation\n- Add telemetry for cache hits",
        };
        const items = extractFollowUpItems(review, 99);
        assert.equal(items.length, 2);
        assert.ok(items[0].title.includes("Consider adding input validation"));
        assert.ok(items[0].body.includes("Recommended improvement"));
    });

    it("skips already-addressed recommendation items", () => {
        const review = {
            recommendation: "- This was already fixed in the PR\n- Add caching -- done\n- Need better logging",
        };
        const items = extractFollowUpItems(review, 10);
        assert.equal(items.length, 1);
        assert.ok(items[0].title.includes("Need better logging"));
    });

    it("combines deferred and recommendation items", () => {
        const review = {
            deferred: "- Future work item",
            recommendation: "- Active recommendation",
        };
        const items = extractFollowUpItems(review, 5);
        assert.equal(items.length, 2);
    });

    it("returns empty for review with no list items", () => {
        const review = {
            deferred: "Nothing to defer.",
            recommendation: "All good.",
        };
        const items = extractFollowUpItems(review, 1);
        assert.equal(items.length, 0);
    });

    it("truncates long titles to 80 chars", () => {
        const longText = "A".repeat(120);
        const review = { deferred: `- ${longText}` };
        const items = extractFollowUpItems(review, 1);
        assert.ok(items[0].title.length <= 93); // "[FOLLOW-UP] " prefix (13) + 80 chars
    });
});
