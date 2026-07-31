// Unit tests for server-handler.mjs with fully mocked dependencies.
// No live server needed -- no side effects on the Copilot harness or GitHub.
// Run: node --test tests/server.test.mjs

import { describe, it, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import {
    createHandler,
    isPathWithinRoot,
    resolveStaticRequest,
} from "../.apm/extensions/issue-monitor/server-handler.mjs";
import { dirname, join, win32 } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const DIST_DIR = join(__dir, "..", ".apm", "extensions", "issue-monitor", "dist");
const REPO_ROOT = join(__dir, "..", "..", "..");

// ---------------------------------------------------------------------------
// Test infrastructure
// ---------------------------------------------------------------------------

const TEST_CSRF_TOKEN = "test-csrf-token-for-unit-tests";

function createMockDeps(overrides = {}) {
    const ghCalls = [];
    const sessionCalls = [];
    const startedSessions = new Set();

    const defaults = {
        ghExec: async (args) => {
            ghCalls.push(args);
            throw new Error("mock: no gh response configured");
        },
        session: { send: (payload) => sessionCalls.push(payload) },
        startedSessions,
        saveSessions: () => {},
        getIssueData: () => [
            { number: 1, title: "Bug A", type: "bug", priority: "P1", author: "alice", status: "available", url: "https://github.com/test/1" },
            { number: 2, title: "Feature B", type: "feature", priority: "P2", author: "bob", status: "planning", url: "https://github.com/test/2" },
        ],
        getPrData: () => [
            { number: 10, title: "Fix A", author: "alice", url: "https://github.com/test/pr/10", prStatus: "review-pending" },
        ],
        getLastUpdated: () => "12:00:00",
        getLastError: () => null,
        repo: "test/repo",
        distDir: DIST_DIR,
        csrfToken: TEST_CSRF_TOKEN,
    };

    const deps = { ...defaults, ...overrides };
    return { deps, ghCalls, sessionCalls, startedSessions };
}

let server;
let baseUrl;
let mockState;

function setupServer(overrides = {}) {
    mockState = createMockDeps(overrides);
    const handler = createHandler(mockState.deps);
    server = createServer(handler);
    return new Promise((resolve) => {
        server.listen(0, "127.0.0.1", () => {
            const addr = server.address();
            baseUrl = `http://127.0.0.1:${addr.port}`;
            resolve();
        });
    });
}

function teardownServer() {
    return new Promise((resolve) => {
        if (server) server.close(resolve);
        else resolve();
    });
}

async function getJSON(path) {
    const res = await fetch(`${baseUrl}${path}`);
    const json = await res.json();
    return { res, json };
}

async function postJSON(path, body) {
    const res = await fetch(`${baseUrl}${path}`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Canvas-Token": TEST_CSRF_TOKEN,
        },
        body: JSON.stringify(body),
    });
    const json = await res.json();
    return { res, json };
}

function listen(localServer) {
    return new Promise((resolve) => {
        localServer.listen(0, "127.0.0.1", () => {
            resolve(`http://127.0.0.1:${localServer.address().port}`);
        });
    });
}

function close(localServer) {
    return new Promise((resolve) => localServer.close(resolve));
}

function rawHttpRequest(origin, path) {
    const url = new URL(origin);
    return new Promise((resolve, reject) => {
        const req = httpRequest({
            hostname: url.hostname,
            port: url.port,
            path,
            method: "GET",
        }, (res) => {
            const chunks = [];
            res.on("data", (chunk) => chunks.push(chunk));
            res.on("end", () => {
                resolve({
                    statusCode: res.statusCode,
                    headers: res.headers,
                    body: Buffer.concat(chunks),
                });
            });
        });
        req.on("error", reject);
        req.end();
    });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("GET /api/issues", () => {
    before(() => setupServer());
    after(teardownServer);

    it("returns issues array with lastUpdated", async () => {
        const { json } = await getJSON("/api/issues");
        assert.equal(Array.isArray(json.issues), true);
        assert.equal(json.issues.length, 2);
        assert.equal(json.lastUpdated, "12:00:00");
        assert.equal(json.error, null);
    });

    it("enriches issues with hasSession field", async () => {
        mockState.startedSessions.add(1);
        const { json } = await getJSON("/api/issues");
        assert.equal(json.issues[0].hasSession, true);
        assert.equal(json.issues[1].hasSession, false);
    });

    it("returns correct issue fields", async () => {
        const { json } = await getJSON("/api/issues");
        const issue = json.issues[0];
        assert.equal(issue.number, 1);
        assert.equal(issue.title, "Bug A");
        assert.equal(issue.author, "alice");
    });
});

describe("GET /api/prs", () => {
    before(() => setupServer());
    after(teardownServer);

    it("returns prs array with lastUpdated", async () => {
        const { json } = await getJSON("/api/prs");
        assert.equal(Array.isArray(json.prs), true);
        assert.equal(json.prs.length, 1);
        assert.equal(json.prs[0].number, 10);
        assert.equal(json.lastUpdated, "12:00:00");
    });
});

describe("GET /api/issue/:n", () => {
    before(() => setupServer({
        ghExec: async (args) => {
            if (args[0] === "issue" && args[1] === "view") {
                return JSON.stringify({
                    number: 42, title: "Test issue", body: "Description here",
                    author: { login: "dev1" }, labels: [{ name: "bug" }],
                    state: "OPEN", createdAt: "2025-01-01", updatedAt: "2025-01-02",
                    comments: [{ body: "comment1" }, { body: "comment2" }],
                });
            }
            throw new Error("unexpected gh call");
        },
    }));
    after(teardownServer);

    it("returns issue detail from gh", async () => {
        const { json } = await getJSON("/api/issue/42");
        assert.equal(json.number, 42);
        assert.equal(json.title, "Test issue");
        assert.equal(json.author, "dev1");
        assert.deepEqual(json.labels, ["bug"]);
        assert.equal(json.comments, 2);
    });

    it("returns error for gh failures", async () => {
        // Issue 999 will also use our mock which returns the same data,
        // but let's test with a fresh server that throws
        const state2 = createMockDeps({ ghExec: async () => { throw new Error("not found"); } });
        const handler2 = createHandler(state2.deps);
        const s2 = createServer(handler2);
        await new Promise(r => s2.listen(0, "127.0.0.1", r));
        const url = `http://127.0.0.1:${s2.address().port}`;
        const res = await fetch(`${url}/api/issue/999`);
        const json = await res.json();
        assert.equal(typeof json.error, "string");
        assert.ok(json.error.includes("not found"));
        await new Promise(r => s2.close(r));
    });
});

describe("GET /api/pr/:n", () => {
    before(() => setupServer({
        ghExec: async (args) => {
            if (args[0] === "pr" && args[1] === "view") {
                return JSON.stringify({
                    number: 10, title: "Fix A", body: "PR body",
                    author: { login: "alice" }, labels: [{ name: "enhancement" }],
                    state: "OPEN", isDraft: false, reviewDecision: "APPROVED",
                    headRefName: "feat/fix-a", createdAt: "2025-01-01", updatedAt: "2025-01-02",
                    comments: [{ author: { login: "bot" }, body: "CI passed", createdAt: "2025-01-01T10:00:00Z", url: "" }],
                    reviews: [{ author: { login: "reviewer" }, body: "LGTM", state: "APPROVED", submittedAt: "2025-01-01T11:00:00Z", url: "" }],
                    statusCheckRollup: [{ name: "lint", status: "COMPLETED", conclusion: "success", detailsUrl: "http://ci/1" }],
                });
            }
            if (args[0] === "run" && args[1] === "list") {
                return JSON.stringify([{ databaseId: 100, name: "CI", status: "completed", conclusion: "success" }]);
            }
            throw new Error("unexpected");
        },
    }));
    after(teardownServer);

    it("returns full PR detail with activity, checks, workflow runs", async () => {
        const { json } = await getJSON("/api/pr/10");
        assert.equal(json.number, 10);
        assert.equal(json.title, "Fix A");
        assert.equal(json.author, "alice");
        assert.equal(json.branch, "feat/fix-a");
        assert.equal(json.reviewDecision, "APPROVED");
        assert.equal(json.activity.length, 2); // 1 comment + 1 review
        assert.equal(json.activity[0].kind, "comment");
        assert.equal(json.activity[1].kind, "review");
        assert.equal(json.checks.length, 1);
        assert.equal(json.checks[0].name, "lint");
        assert.equal(json.workflowRuns.length, 1);
    });

    it("skips empty COMMENTED reviews", async () => {
        const state = createMockDeps({
            ghExec: async (args) => {
                if (args[0] === "pr" && args[1] === "view") {
                    return JSON.stringify({
                        number: 5, title: "X", body: "", author: { login: "a" },
                        labels: [], state: "OPEN", isDraft: false, reviewDecision: "",
                        headRefName: "", createdAt: "", updatedAt: "",
                        comments: [],
                        reviews: [{ author: { login: "bot" }, body: "", state: "COMMENTED", submittedAt: "" }],
                        statusCheckRollup: [],
                    });
                }
                return "[]";
            },
        });
        const handler = createHandler(state.deps);
        const s = createServer(handler);
        await new Promise(r => s.listen(0, "127.0.0.1", r));
        const res = await fetch(`http://127.0.0.1:${s.address().port}/api/pr/5`);
        const json = await res.json();
        assert.equal(json.activity.length, 0);
        await new Promise(r => s.close(r));
    });
});

describe("POST /start-session", () => {
    before(() => setupServer());
    after(teardownServer);

    it("returns ok:true, adds to startedSessions, calls session.send", async () => {
        const { json } = await postJSON("/start-session", { number: 42, title: "Test" });
        assert.equal(json.ok, true);
        assert.equal(mockState.startedSessions.has(42), true);
        // session.send is called via setTimeout -- wait a tick
        await new Promise(r => setTimeout(r, 10));
        assert.equal(mockState.sessionCalls.length, 1);
        assert.ok(mockState.sessionCalls[0].prompt.includes("42"));
        assert.ok(mockState.sessionCalls[0].prompt.includes("Test"));
    });

    it("returns ok:false for malformed JSON", async () => {
        const res = await fetch(`${baseUrl}/start-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{{invalid",
        });
        const json = await res.json();
        assert.equal(json.ok, false);
        assert.equal(typeof json.error, "string");
    });
});

describe("POST /open-session", () => {
    before(() => setupServer());
    after(teardownServer);

    it("returns ok:true and calls session.send with navigate prompt", async () => {
        const { json } = await postJSON("/open-session", { number: 7, title: "Nav test" });
        assert.equal(json.ok, true);
        await new Promise(r => setTimeout(r, 10));
        assert.equal(mockState.sessionCalls.length, 1);
        assert.ok(mockState.sessionCalls[0].prompt.includes("Navigate"));
        assert.ok(mockState.sessionCalls[0].prompt.includes("7"));
    });
});

describe("POST /run-panel", () => {
    let ghCalls;
    before(async () => {
        const m = createMockDeps({
            ghExec: async (args) => {
                ghCalls.push(args);
                if (args[0] === "pr" && args[1] === "view") return JSON.stringify({ headRefName: "feat/x" });
                if (args[0] === "run" && args[1] === "list") return JSON.stringify([{ databaseId: 55, conclusion: "action_required" }]);
                if (args[0] === "api") return ""; // approve
                if (args[0] === "pr" && args[1] === "edit") return "";
                return "";
            },
        });
        ghCalls = m.ghCalls;
        const handler = createHandler(m.deps);
        server = createServer(handler);
        await new Promise(r => server.listen(0, "127.0.0.1", r));
        baseUrl = `http://127.0.0.1:${server.address().port}`;
    });
    after(teardownServer);

    it("approves pending runs then adds panel-review label", async () => {
        ghCalls.length = 0;
        const { json } = await postJSON("/run-panel", { number: 99 });
        assert.equal(json.ok, true);
        // Should have called: pr view, run list, api approve, pr edit --add-label
        const labels = ghCalls.filter(c => c.includes("--add-label"));
        assert.equal(labels.length, 1);
        assert.ok(labels[0].includes("panel-review"));
    });
});

describe("POST /approve-pipeline", () => {
    before(async () => {
        await setupServer({
            ghExec: async (args) => {
                if (args[0] === "pr" && args[1] === "checks") {
                    return JSON.stringify([
                        { name: "lint", state: "FAILURE", link: "https://ci.com/runs/111/jobs/1" },
                        { name: "test", state: "SUCCESS", link: "https://ci.com/runs/222/jobs/2" },
                        { name: "build", state: "ERROR", link: "https://ci.com/runs/111/jobs/3" },
                    ]);
                }
                return ""; // rerun calls
            },
        });
    });
    after(teardownServer);

    it("extracts failed run IDs and re-runs them, returns reran count", async () => {
        const { json } = await postJSON("/approve-pipeline", { number: 5 });
        assert.equal(json.ok, true);
        assert.equal(json.reran, 1); // both failures point to run 111
    });
});

describe("POST /approve-pr", () => {
    let calls;
    before(async () => {
        calls = [];
        await setupServer({ ghExec: async (args) => { calls.push(args); return ""; } });
    });
    after(teardownServer);

    it("calls gh pr review --approve and returns ok", async () => {
        const { json } = await postJSON("/approve-pr", { number: 10 });
        assert.equal(json.ok, true);
        const approveCall = calls.find(c => c.includes("--approve"));
        assert.ok(approveCall, "should call gh pr review --approve");
    });
});

describe("POST /approve-workflow-runs", () => {
    before(async () => {
        await setupServer({
            ghExec: async (args) => {
                if (args[0] === "run" && args[1] === "list") {
                    return JSON.stringify([
                        { databaseId: 1, conclusion: "action_required" },
                        { databaseId: 2, conclusion: "success" },
                        { databaseId: 3, conclusion: "action_required" },
                    ]);
                }
                return ""; // api approve
            },
        });
    });
    after(teardownServer);

    it("approves only action_required runs and returns count", async () => {
        const { json } = await postJSON("/approve-workflow-runs", { branch: "feat/x" });
        assert.equal(json.ok, true);
        assert.equal(json.approved, 2);
    });
});

describe("POST /merge-when-ready", () => {
    let calls;
    before(async () => {
        calls = [];
        await setupServer({ ghExec: async (args) => { calls.push(args); return ""; } });
    });
    after(teardownServer);

    it("calls gh pr merge --auto --squash and returns ok", async () => {
        const { json } = await postJSON("/merge-when-ready", { number: 10 });
        assert.equal(json.ok, true);
        const mergeCall = calls.find(c => c.includes("--auto"));
        assert.ok(mergeCall);
        assert.ok(mergeCall.includes("--squash"));
    });
});

// ---------------------------------------------------------------------------
// GET /api/triage
// ---------------------------------------------------------------------------

function makeTriageGqlResponse(issues) {
    return JSON.stringify({
        data: {
            repository: {
                issues: {
                    pageInfo: { hasNextPage: false, endCursor: null },
                    nodes: issues,
                },
            },
        },
    });
}

const TRIAGE_DECISION_BLOCK = `\`\`\`json triage-decision
{
  "decision": "accept",
  "theme": "theme/governance",
  "areas": ["area/audit"],
  "type": "type/bug",
  "status": "status/accepted",
  "priority": "priority/high",
  "milestone": "0.9.x",
  "next_action": "Fix the bug",
  "comment_markdown": "## Analysis\\nAccepted."
}
\`\`\``;

describe("GET /api/triage", () => {
    before(async () => {
        await setupServer({
            ghExec: async (args) => {
                if (args[0] === "api" && args[1] === "graphql") {
                    return makeTriageGqlResponse([
                        {
                            number: 100,
                            title: "Triaged bug",
                            url: "https://github.com/test/issues/100",
                            body: "Issue description",
                            labels: { nodes: [{ name: "bug" }] },
                            comments: {
                                nodes: [
                                    {
                                        body: TRIAGE_DECISION_BLOCK,
                                        createdAt: "2025-01-01T10:00:00Z",
                                        author: { login: "triage-bot", avatarUrl: "" },
                                        isMinimized: false,
                                    },
                                    {
                                        body: "Human follow-up comment",
                                        createdAt: "2025-01-02T10:00:00Z",
                                        author: { login: "alice", avatarUrl: "" },
                                        isMinimized: false,
                                    },
                                ],
                            },
                        },
                        {
                            // Issue without a triage comment -- must be excluded
                            number: 101,
                            title: "Untriaged issue",
                            url: "https://github.com/test/issues/101",
                            body: "",
                            labels: { nodes: [] },
                            comments: { nodes: [] },
                        },
                    ]);
                }
                throw new Error("unexpected gh call");
            },
        });
    });
    after(teardownServer);

    it("returns only issues that have a triage-decision block", async () => {
        const { json } = await getJSON("/api/triage");
        assert.equal(json.items.length, 1, "untriaged issues must be excluded");
        assert.equal(json.total, 1);
    });

    it("parses triage-decision fields correctly", async () => {
        const { json } = await getJSON("/api/triage");
        const item = json.items[0];
        assert.equal(item.number, 100);
        assert.equal(item.title, "Triaged bug");
        assert.equal(item.decision, "accept");
        assert.equal(item.priority, "priority/high");
        assert.equal(item.milestone, "0.9.x");
        assert.equal(item.nextAction, "Fix the bug");
        assert.equal(item.type, "type/bug");
        assert.deepEqual(item.labels, ["bug"]);
    });

    it("enriches items with hasSession from startedSessions", async () => {
        mockState.startedSessions.add(100);
        // Invalidate cache so fresh response is returned
        const handler = createHandler(mockState.deps);
        const s = createServer(handler);
        await new Promise(r => s.listen(0, "127.0.0.1", r));
        const url = `http://127.0.0.1:${s.address().port}`;
        const res = await fetch(`${url}/api/triage`);
        const json = await res.json();
        assert.equal(json.items[0].hasSession, true);
        await new Promise(r => s.close(r));
    });

    it("collects non-triage comments into nonTriageComments", async () => {
        const { json } = await getJSON("/api/triage");
        const item = json.items[0];
        assert.equal(item.nonTriageComments.length, 1);
        assert.equal(item.nonTriageComments[0].author, "alice");
        assert.equal(item.nonTriageComments[0].body, "Human follow-up comment");
    });

    it("returns cached response on second call within TTL", async () => {
        const ghCalls = [];
        const deps = createMockDeps({
            ghExec: async (args) => {
                ghCalls.push(args);
                if (args[0] === "api" && args[1] === "graphql") {
                    return makeTriageGqlResponse([]);
                }
                throw new Error("unexpected");
            },
        });
        const handler = createHandler(deps.deps);
        const s = createServer(handler);
        await new Promise(r => s.listen(0, "127.0.0.1", r));
        const url = `http://127.0.0.1:${s.address().port}`;

        // First call -- populates cache
        await fetch(`${url}/api/triage`);
        const callsAfterFirst = ghCalls.length;

        // Second call -- must be served from cache, no new gh api call
        await fetch(`${url}/api/triage`);
        assert.equal(
            ghCalls.length,
            callsAfterFirst,
            "second /api/triage call within TTL must be served from cache",
        );
        await new Promise(r => s.close(r));
    });
});

describe("Static file serving", () => {
    before(() => setupServer());
    after(teardownServer);

    it("GET / returns HTML with no-cache", async () => {
        const res = await fetch(`${baseUrl}/`);
        assert.equal(res.status, 200);
        assert.ok(res.headers.get("content-type").includes("text/html"));
        assert.equal(res.headers.get("cache-control"), "no-cache");
    });

    it("GET /unknown-route returns HTML (SPA fallback)", async () => {
        const res = await fetch(`${baseUrl}/some/random/path`);
        assert.equal(res.status, 200);
        assert.ok(res.headers.get("content-type").includes("text/html"));
    });

    it("GET /assets/nonexistent returns 404", async () => {
        const res = await fetch(`${baseUrl}/assets/does-not-exist.js`);
        assert.equal(res.status, 404);
    });
});

describe("GET /api/permissions", () => {
    it("returns permissions from gh api and caches them", async () => {
        let callCount = 0;
        await setupServer({
            ghExec: async (args) => {
                if (args[0] === "api" && args[1].includes("repos/")) {
                    callCount++;
                    return JSON.stringify({ pull: true, triage: true, push: false, maintain: false, admin: false });
                }
                throw new Error("unexpected gh call");
            },
        });

        const res1 = await fetch(`${baseUrl}/api/permissions`);
        assert.equal(res1.status, 200);
        const data1 = await res1.json();
        assert.equal(data1.push, false);
        assert.equal(data1.triage, true);
        assert.equal(data1.pull, true);
        assert.equal(callCount, 1);

        // Second call should use cache (no extra gh call)
        const res2 = await fetch(`${baseUrl}/api/permissions`);
        const data2 = await res2.json();
        assert.deepEqual(data2, data1);
        assert.equal(callCount, 1);

        await teardownServer();
    });

    it("returns safe defaults when gh api fails", async () => {
        await setupServer({
            ghExec: async () => { throw new Error("auth failed"); },
        });

        const res = await fetch(`${baseUrl}/api/permissions`);
        assert.equal(res.status, 200);
        const data = await res.json();
        assert.equal(data.pull, true);
        assert.equal(data.push, false);
        assert.equal(data.admin, false);

        await teardownServer();
    });
});

describe("POST /create-follow-up-issues", () => {
    it("creates issues from deferred panel review items", async () => {
        const createdIssues = [];
        await setupServer({
            ghExec: async (args) => {
                if (args[0] === "issue" && args[1] === "create") {
                    const titleIdx = args.indexOf("--title");
                    const title = titleIdx >= 0 ? args[titleIdx + 1] : "";
                    createdIssues.push(title);
                    return "https://github.com/microsoft/apm/issues/999";
                }
                return "{}";
            },
        });

        const panelReview = {
            deferred: "- Add retry logic\n- Improve error messages",
            recommendation: "- Consider caching",
        };
        const res = await fetch(`${baseUrl}/create-follow-up-issues`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
            },
            body: JSON.stringify({ number: 42, panelReview }),
        });
        assert.equal(res.status, 200);
        const data = await res.json();
        assert.equal(data.ok, true);
        assert.equal(data.created.length, 3);
        assert.equal(createdIssues.length, 3);

        await teardownServer();
    });

    it("returns empty created array when no follow-up items", async () => {
        await setupServer({ ghExec: async () => "{}" });

        const res = await fetch(`${baseUrl}/create-follow-up-issues`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
            },
            body: JSON.stringify({ number: 1, panelReview: { recommendation: "All good." } }),
        });
        const data = await res.json();
        assert.equal(data.ok, true);
        assert.equal(data.created.length, 0);

        await teardownServer();
    });
});

// ---------------------------------------------------------------------------
// CSRF protection tests
// ---------------------------------------------------------------------------

describe("CSRF protection", () => {
    before(() => setupServer({ ghExec: async () => "" }));
    after(teardownServer);

    it("rejects POST without X-Canvas-Token header", async () => {
        const res = await fetch(`${baseUrl}/start-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ number: 1, title: "test" }),
        });
        assert.equal(res.status, 403);
        const json = await res.json();
        assert.ok(json.error.includes("CSRF"));
    });

    it("rejects POST with wrong CSRF token", async () => {
        const res = await fetch(`${baseUrl}/approve-pr`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": "wrong-token",
            },
            body: JSON.stringify({ number: 1 }),
        });
        assert.equal(res.status, 403);
        const json = await res.json();
        assert.ok(json.error.includes("CSRF"));
    });

    it("rejects POST with cross-origin Origin header", async () => {
        const res = await fetch(`${baseUrl}/approve-pr`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
                "Origin": "https://evil.com",
            },
            body: JSON.stringify({ number: 1 }),
        });
        assert.equal(res.status, 403);
        const json = await res.json();
        assert.ok(json.error.includes("cross-origin"));
    });

    it("allows POST with correct token and localhost origin", async () => {
        const res = await fetch(`${baseUrl}/approve-pr`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
                "Origin": "http://127.0.0.1:3000",
            },
            body: JSON.stringify({ number: 1 }),
        });
        // Should succeed (or fail with gh error, but not 403)
        assert.notEqual(res.status, 403);
    });

    it("does not require CSRF token for GET endpoints", async () => {
        const res = await fetch(`${baseUrl}/api/issues`);
        assert.equal(res.status, 200);
    });
});

describe("POST body size limits", () => {
    before(() => setupServer());
    after(teardownServer);

    it("returns 413 for oversized payloads on default POST endpoints", async () => {
        const oversizedBody = { number: 1, title: "x".repeat(70 * 1024) };
        const res = await fetch(`${baseUrl}/start-session`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
            },
            body: JSON.stringify(oversizedBody),
        });
        assert.equal(res.status, 413);
        const json = await res.json();
        assert.equal(json.ok, false);
        assert.ok(String(json.error).includes("limit"));
    });

    it("returns 413 for oversized refine-comment drafts", async () => {
        const oversizedRefine = { type: "issue", number: 1, title: "big", draft: "y".repeat(1024 * 1024 + 1) };
        const res = await fetch(`${baseUrl}/refine-comment`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Canvas-Token": TEST_CSRF_TOKEN,
            },
            body: JSON.stringify(oversizedRefine),
        });
        assert.equal(res.status, 413);
        const json = await res.json();
        assert.equal(json.ok, false);
        assert.ok(String(json.error).includes("limit"));
    });
});

// ---------------------------------------------------------------------------
// Path traversal protection
// ---------------------------------------------------------------------------

describe("Path traversal protection", () => {
    const SIBLING_SENTINEL = "outside-sibling-sentinel";
    const LINK_SENTINEL = "outside-link-sentinel";
    const SAFE_ASSET = Buffer.from("console.log('safe asset');\n", "utf8");
    let workDir;
    let distDir;
    let assetServer;
    let assetBaseUrl;
    let linkSetupError;
    let mimeLinkSetupError;

    before(async () => {
        workDir = await mkdtemp(join(__dir, ".static-assets-"));
        distDir = join(workDir, "dist");
        const assetsDir = join(distDir, "assets");
        const siblingDir = join(workDir, "dist-evil");
        const outsideDir = join(workDir, "outside");

        await mkdir(join(assetsDir, "nested"), { recursive: true });
        await mkdir(join(assetsDir, "dist-evil"), { recursive: true });
        await mkdir(siblingDir, { recursive: true });
        await mkdir(outsideDir, { recursive: true });
        await writeFile(join(assetsDir, "nested", "app..js"), SAFE_ASSET);
        await writeFile(join(assetsDir, "dist-evil", "inside.txt"), "inside-prefix", "utf8");
        await writeFile(join(siblingDir, "secret.txt"), SIBLING_SENTINEL, "utf8");
        await writeFile(join(outsideDir, "secret.txt"), LINK_SENTINEL, "utf8");

        try {
            await symlink(
                outsideDir,
                join(assetsDir, "outside-link"),
                process.platform === "win32" ? "junction" : "dir",
            );
        } catch (error) {
            if (process.platform !== "win32" || !["EACCES", "EPERM"].includes(error.code)) {
                throw error;
            }
            linkSetupError = error;
        }
        try {
            await symlink(
                join(assetsDir, "nested", "app..js"),
                join(assetsDir, "alias.css"),
                "file",
            );
        } catch (error) {
            if (process.platform !== "win32" || !["EACCES", "EPERM"].includes(error.code)) {
                throw error;
            }
            mimeLinkSetupError = error;
        }

        const state = createMockDeps({ distDir });
        assetServer = createServer(createHandler(state.deps));
        assetBaseUrl = await listen(assetServer);
    });

    after(async () => {
        if (assetServer) await close(assetServer);
        if (workDir) await rm(workDir, { recursive: true, force: true });
    });

    function assertRejected(response, expectedStatus, ...sentinels) {
        const body = response.body.toString("utf8");
        for (const sentinel of sentinels) {
            assert.equal(body.includes(sentinel), false, `outside sentinel leaked: ${sentinel}`);
        }
        assert.equal(body.includes(workDir), false, "response leaked a filesystem path");
        assert.equal(response.statusCode, expectedStatus);
        assert.equal(body, expectedStatus === 400 ? "Bad request" : "Forbidden");
    }

    it("serves valid nested and prefix-sharing asset names exactly", async () => {
        const nested = await rawHttpRequest(assetBaseUrl, "/assets/nested/app..js");
        assert.equal(nested.statusCode, 200);
        assert.deepEqual(nested.body, SAFE_ASSET);
        assert.equal(nested.headers["content-type"], "text/javascript");
        assert.equal(
            nested.headers["cache-control"],
            "public, max-age=31536000, immutable",
        );

        const cacheBusted = await rawHttpRequest(
            assetBaseUrl,
            "/assets/nested/app..js?v=123",
        );
        assert.equal(cacheBusted.statusCode, 200);
        assert.deepEqual(cacheBusted.body, SAFE_ASSET);

        const prefix = await rawHttpRequest(assetBaseUrl, "/assets/dist-evil/inside.txt");
        assert.equal(prefix.statusCode, 200);
        assert.equal(prefix.body.toString("utf8"), "inside-prefix");
        assert.equal(prefix.headers["content-type"], "application/octet-stream");
    });

    it("returns a stable 404 for nonexistent assets", async () => {
        const response = await rawHttpRequest(assetBaseUrl, "/assets/nonexistent.js");
        assert.equal(response.statusCode, 404);
        assert.equal(response.body.toString("utf8"), "Not found");

        const fileAsDirectory = await rawHttpRequest(
            assetBaseUrl,
            "/assets/nested/app..js/child.js",
        );
        assert.equal(fileAsDirectory.statusCode, 404);
        assert.equal(fileAsDirectory.body.toString("utf8"), "Not found");

        const missingIndex = await rawHttpRequest(assetBaseUrl, "/");
        assert.equal(missingIndex.statusCode, 404);
        assert.equal(missingIndex.headers["content-type"], "text/plain; charset=utf-8");
        assert.equal(missingIndex.body.toString("utf8"), "Not found");
    });

    it("uses the requested extension for MIME after canonical resolution", async (t) => {
        if (mimeLinkSetupError) {
            t.diagnostic(`file symlink setup denied (${mimeLinkSetupError.code}); path seam still runs`);
            assert.ok(["EACCES", "EPERM"].includes(mimeLinkSetupError.code));
            return;
        }

        const response = await rawHttpRequest(assetBaseUrl, "/assets/alias.css");
        assert.equal(response.statusCode, 200);
        assert.deepEqual(response.body, SAFE_ASSET);
        assert.equal(response.headers["content-type"], "text/css");
    });

    it("blocks sibling prefix traversal before outside bytes are read", async () => {
        const response = await rawHttpRequest(
            assetBaseUrl,
            "/assets/../../dist-evil/secret.txt",
        );
        assertRejected(response, 403, SIBLING_SENTINEL);
    });

    it("blocks encoded traversal before outside bytes are read", async () => {
        const response = await rawHttpRequest(
            assetBaseUrl,
            "/assets/%2e%2e/%2e%2e/dist-evil/secret.txt",
        );
        assertRejected(response, 403, SIBLING_SENTINEL);
    });

    it("rejects malformed percent encodings and keeps serving requests", async () => {
        for (const path of [
            "/assets/%",
            "/assets/%zz",
            "/assets/%E0%A4%A",
            "/%zz-non-asset",
        ]) {
            const response = await rawHttpRequest(assetBaseUrl, path);
            assertRejected(response, 400, SIBLING_SENTINEL, LINK_SENTINEL);
        }

        const healthy = await rawHttpRequest(assetBaseUrl, "/assets/nested/app..js");
        assert.equal(healthy.statusCode, 200);
        assert.deepEqual(healthy.body, SAFE_ASSET);
    });

    it("rejects mixed, NUL, absolute, drive, UNC, and repeated encodings", async () => {
        const unsafePaths = [
            "/assets/",
            "/assets/..\\..\\dist-evil\\secret.txt",
            "/assets/..%5c..%5cdist-evil%5csecret.txt",
            "/assets/%00secret.txt",
            "/assets/%2fetc%2fpasswd",
            "/assets/C:%5cWindows%5cwin.ini",
            "/assets/%5c%5cserver%5cshare%5cfile.txt",
            "/assets/%252e%252e/%252e%252e/dist-evil/secret.txt",
        ];
        for (const path of unsafePaths) {
            const response = await rawHttpRequest(assetBaseUrl, path);
            assertRejected(response, 403, SIBLING_SENTINEL, LINK_SENTINEL);
        }
    });

    it("rejects symlinked or junction assets that resolve outside dist", async (t) => {
        if (linkSetupError) {
            t.diagnostic(`junction setup denied (${linkSetupError.code}); Windows seam still runs`);
            assert.ok(["EACCES", "EPERM"].includes(linkSetupError.code));
            return;
        }

        const response = await rawHttpRequest(
            assetBaseUrl,
            "/assets/outside-link/secret.txt",
        );
        assertRejected(response, 403, LINK_SENTINEL);
    });

    it("applies canonical containment with Windows semantics on every platform", () => {
        const root = "C:\\dashboard\\dist";
        const candidate = win32.resolve(root, "assets", "outside-link", "secret.txt");
        const canonicalPaths = new Map([
            [win32.resolve(root), "C:\\dashboard\\dist"],
            [candidate, "C:\\dashboard\\outside\\secret.txt"],
        ]);
        const result = resolveStaticRequest(
            "/assets/outside-link/secret.txt",
            root,
            {
                pathApi: win32,
                realpathSync: (path) => canonicalPaths.get(path),
            },
        );

        assert.equal(result.kind, "forbidden");
        assert.equal(
            isPathWithinRoot(root, "C:\\dashboard\\dist-evil\\secret.txt", win32),
            false,
        );
        assert.equal(
            isPathWithinRoot(root, "C:\\dashboard\\dist\\assets\\app..js", win32),
            true,
        );

        const aliasCandidate = win32.resolve(root, "assets", "alias.css");
        canonicalPaths.set(aliasCandidate, "C:\\dashboard\\dist\\assets\\nested\\app.js");
        const alias = resolveStaticRequest("/assets/alias.css", root, {
            pathApi: win32,
            realpathSync: (path) => canonicalPaths.get(path),
        });
        assert.equal(alias.kind, "asset");
        assert.equal(alias.filePath, "C:\\dashboard\\dist\\assets\\nested\\app.js");
        assert.equal(alias.mimePath, aliasCandidate);
    });
});

describe("Package source ownership", () => {
    it("keeps generated extension copies out of tracked source", async () => {
        const tracked = execFileSync(
            "git",
            [
                "-C",
                REPO_ROOT,
                "ls-files",
                "--",
                "packages/apm-contributor-dashboard/.github/extensions/**",
            ],
            { encoding: "utf8" },
        );
        assert.equal(tracked.trim(), "");

        const ignoreFile = await readFile(
            join(REPO_ROOT, "packages", "apm-contributor-dashboard", ".gitignore"),
            "utf8",
        );
        assert.match(ignoreFile, /^\.github\/extensions\/$/m);
    });
});
