import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
    applyOccupancyReport,
    createHandler,
    hydrateOccupancy,
    loadOccupancyFile,
    saveOccupancyFile,
    serializeOccupancy,
    syncOccupancyFromCopilot,
} from "../.apm/extensions/autopilot-maintainer/server-handler.mjs";
import { readCopilotAppSessions } from "../.apm/extensions/autopilot-maintainer/copilot-sessions.mjs";
import { DatabaseSync } from "node:sqlite";

const CSRF = "test-csrf-token";

function createMock(overrides = {}) {
    const ghCalls = [];
    const sessionCalls = [];
    const occupancy = new Map();
    const issues = [
        {
            kind: "issue",
            number: 12,
            title: "Needs a decision",
            url: "https://github.com/microsoft/apm/issues/12",
            labels: ["status/needs-triage", "triage/recommended"],
            accepted: false,
            deferred: false,
            advisory: true,
            needsTriage: true,
            lanes: ["decide"],
        },
        {
            kind: "issue",
            number: 13,
            title: "Accepted bug",
            url: "https://github.com/microsoft/apm/issues/13",
            labels: ["status/accepted"],
            accepted: true,
            lanes: ["accepted"],
        },
    ];
    const prs = [
        {
            kind: "pr",
            number: 40,
            title: "Ready",
            url: "https://github.com/microsoft/apm/pull/40",
            labels: ["status/accepted"],
            accepted: true,
            isDraft: false,
            lanes: ["accepted", "merge-ready"],
        },
    ];
    const deps = {
        csrfToken: CSRF,
        occupancy,
        repo: "microsoft/apm",
        ghExec: async (args) => {
            ghCalls.push(args);
            return "";
        },
        session: { send: (payload) => sessionCalls.push(payload) },
        getIssues: () => issues,
        getPrs: () => prs,
        getLastUpdated: () => "now",
        getLastError: () => null,
        refreshData: async () => ({ ok: true, error: null }),
        readCopilotAppSessions: () => [],
        ...overrides,
    };
    return { deps, ghCalls, sessionCalls, occupancy };
}

async function withServer(overrides, fn) {
    const mock = createMock(overrides);
    const handler = createHandler(mock.deps);
    const server = createServer(handler);
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address();
    const baseUrl = `http://127.0.0.1:${port}`;
    try {
        await fn({ baseUrl, ...mock });
    } finally {
        await new Promise((resolve) => server.close(resolve));
    }
}

async function postJson(baseUrl, path, body, headers = {}) {
    const res = await fetch(baseUrl + path, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "x-canvas-token": CSRF,
            ...headers,
        },
        body: JSON.stringify(body),
    });
    return { res, json: await res.json() };
}

describe("server handler", () => {
    it("serves the control surface", async () => {
        await withServer({}, async ({ baseUrl }) => {
            const res = await fetch(baseUrl + "/");
            const html = await res.text();
            assert.equal(res.ok, true);
            assert.match(html, /Autopilot maintainer/);
            assert.match(html, /Sweep issue triage/);
            assert.match(html, /id: "deferred"/);
            assert.match(html, /Not triaged/);
            assert.match(html, /Triaged:/);
            assert.match(html, /id="theme-select"/);
            assert.match(html, /option value="auto" selected/);
            assert.match(html, /data-sessions-toggle/);
            assert.match(html, /SESSIONS_OPEN_KEY/);
            assert.match(html, /sessions-table/);
            assert.equal(/data-archive/.test(html), false);
            assert.equal(/Archive session\?/.test(html), false);
            assert.equal(/Approve and merge/i.test(html), false);
            const scripts = html.split("<script>").slice(1).map((chunk) => chunk.split("</script>")[0]);
            assert.ok(scripts.length >= 2);
            for (const script of scripts) new Function(script);
        });
    });

    it("rejects label writes without CSRF", async () => {
        await withServer({}, async ({ baseUrl }) => {
            const { res, json } = await postJson(baseUrl, "/label", {
                kind: "issue",
                number: 12,
                action: "accept",
            }, { "x-canvas-token": "nope" });
            assert.equal(res.status, 403);
            assert.equal(json.ok, false);
        });
    });

    it("accepts via gh issue edit labels only", async () => {
        await withServer({}, async ({ baseUrl, ghCalls }) => {
            const { json } = await postJson(baseUrl, "/label", {
                kind: "issue",
                number: 12,
                action: "accept",
            });
            assert.equal(json.ok, true);
            assert.equal(json.item.accepted, true);
            assert.deepEqual(json.item.lanes, ["accepted"]);
            assert.equal(ghCalls.length, 1);
            assert.deepEqual(ghCalls[0].slice(0, 5), ["issue", "edit", "12", "--repo", "microsoft/apm"]);
            assert.equal(ghCalls[0].includes("--add-assignee"), false);
            assert.equal(ghCalls[0].includes("merge"), false);
            const state = await fetch(baseUrl + "/api/state").then((res) => res.json());
            const issue = state.issues.find((row) => row.number === 12);
            assert.equal(issue.accepted, true);
            assert.deepEqual(issue.lanes, ["accepted"]);
        });
    });

    it("spawns isolated create_session prompts and refuses duplicates", async () => {
        await withServer({}, async ({ baseUrl, sessionCalls }) => {
            const first = await postJson(baseUrl, "/spawn", { action: "sweep-issue-triage" });
            assert.equal(first.json.ok, true);
            assert.equal(first.json.pending, true);
            assert.match(sessionCalls[0].prompt, /tool: create_session/);
            assert.match(sessionCalls[0].prompt, /do_not_run_here: yes/);
            assert.match(sessionCalls[0].prompt, /skill: autopilot-issue-triage-scheduler/);
            const state = await fetch(baseUrl + "/api/state").then((res) => res.json());
            assert.equal(state.occupancy[0].pending, true);
            assert.equal(state.occupancy[0].target, "scheduler:issue-triage");
            assert.equal(state.sessions[0].family, "issue-triage");
            assert.equal(state.sessions[0].scheduler.name, "Issue triage scheduler");
            const second = await postJson(baseUrl, "/spawn", { action: "sweep-issue-triage" });
            assert.equal(second.res.status, 409);
            assert.match(second.json.error, /already working/);
        });
    });

    it("requires confirm before merge-worker spawn", async () => {
        await withServer({}, async ({ baseUrl, sessionCalls }) => {
            const denied = await postJson(baseUrl, "/spawn", {
                action: "worker-pr-merge",
                number: 40,
            });
            assert.equal(denied.res.status, 409);
            assert.equal(sessionCalls.length, 0);
            const ok = await postJson(baseUrl, "/spawn", {
                action: "worker-pr-merge",
                number: 40,
                confirm: true,
            });
            assert.equal(ok.json.ok, true);
            assert.match(ok.json.skill, /autopilot-pr-merge-worker/);
            assert.match(sessionCalls[0].prompt, /PR merge #40/);
        });
    });

    it("has no archive endpoint", async () => {
        await withServer({}, async ({ baseUrl, sessionCalls }) => {
            const { res, json } = await postJson(baseUrl, "/archive", {
                session_id: "66666666-6666-4666-8666-666666666666",
                target: "scheduler:issue-triage",
                confirm: true,
            });
            assert.equal(res.status, 404);
            assert.equal(json.ok, false);
            assert.equal(sessionCalls.length, 0);
        });
    });
});

describe("occupancy reports", () => {
    it("clears a target when busy is false", () => {
        const occupancy = new Map();
        applyOccupancyReport(occupancy, { target: "scheduler:issue-triage", busy: true, name: "Issue triage scheduler" });
        assert.equal(occupancy.get("scheduler:issue-triage").busy, true);
        applyOccupancyReport(occupancy, { target: "scheduler:issue-triage", busy: false });
        assert.equal(occupancy.has("scheduler:issue-triage"), false);
    });

    it("refreshes occupancy from Copilot workspaces without session.send", async () => {
        const occupancy = new Map([
            ["worker:pr-triage:1", { busy: true, activity: "idle", name: "stale", pending: false }],
        ]);
        await withServer({ occupancy }, async ({ baseUrl, sessionCalls }) => {
            const { json } = await postJson(baseUrl, "/sync-occupancy", {});
            assert.equal(json.ok, true);
            assert.equal(sessionCalls.length, 0);
            assert.equal(occupancy.has("worker:pr-triage:1"), false);
        });
    });

    it("reads live unarchived workspaces from a Copilot data.db fixture", () => {
        const dir = mkdtempSync(join(tmpdir(), "apm-copilot-db-"));
        const dbPath = join(dir, "data.db");
        const db = new DatabaseSync(dbPath);
        db.exec(`
            CREATE TABLE workspaces (
              id TEXT PRIMARY KEY,
              name TEXT,
              session_id TEXT,
              archived_at TEXT,
              creator_session_id TEXT
            );
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY,
              is_running INTEGER
            );
            INSERT INTO sessions (id, is_running) VALUES ('s-run', 1), ('s-idle', 0), ('s-arch', 0);
            INSERT INTO workspaces (id, name, session_id, archived_at, creator_session_id) VALUES
              ('w1', 'PR triage #12', 's-run', NULL, 's-idle'),
              ('w2', 'Issue triage scheduler', 's-idle', NULL, 'canvas'),
              ('w3', 'PR triage #99', 's-arch', '2026-09-17T15:14:00Z', NULL),
              ('w4', 'Random chat', 's-idle', NULL, NULL);
        `);
        db.close();
        const occupancy = new Map();
        syncOccupancyFromCopilot(occupancy, {
            readCopilotAppSessions: () => readCopilotAppSessions(dbPath),
        });
        assert.equal(occupancy.get("worker:pr-triage:12").activity, "working");
        assert.equal(occupancy.get("worker:pr-triage:12").creatorSessionId, "s-idle");
        assert.equal(occupancy.get("scheduler:issue-triage").activity, "idle");
        assert.equal(occupancy.get("scheduler:issue-triage").creatorSessionId, "canvas");
        assert.equal(occupancy.has("worker:pr-triage:99"), false);
    });

    it("preserves occupancy when Copilot data.db cannot be read", () => {
        const occupancy = new Map();
        applyOccupancyReport(occupancy, {
            target: "scheduler:issue-triage",
            busy: true,
            name: "Issue triage scheduler",
        });
        const result = syncOccupancyFromCopilot(occupancy, {
            readCopilotAppSessions: () => {
                throw new Error("database is locked");
            },
        });
        assert.equal(result.ok, false);
        assert.equal(occupancy.get("scheduler:issue-triage").name, "Issue triage scheduler");
    });

    it("throws when an existing Copilot data.db is unreadable", () => {
        const dir = mkdtempSync(join(tmpdir(), "apm-copilot-db-bad-"));
        const dbPath = join(dir, "data.db");
        writeFileSync(dbPath, "not a sqlite database");
        assert.throws(() => readCopilotAppSessions(dbPath), /unreadable/);
    });

    it("keeps idle workers listed until busy is false", () => {
        const occupancy = new Map();
        applyOccupancyReport(occupancy, {
            target: "worker:pr-triage:12",
            busy: true,
            activity: "idle",
            name: "PR triage #12",
            session_id: "sess-idle",
        });
        const row = occupancy.get("worker:pr-triage:12");
        assert.equal(row.busy, true);
        assert.equal(row.activity, "idle");
        assert.equal(row.sessionId, "sess-idle");
    });

    it("round-trips occupancy to disk so a canvas reload can hydrate", () => {
        const occupancy = new Map();
        applyOccupancyReport(occupancy, {
            target: "scheduler:pr-triage",
            busy: true,
            name: "PR triage scheduler",
            skill: "autopilot-pr-triage-scheduler",
            session_id: "sess-1",
        });
        applyOccupancyReport(occupancy, {
            target: "worker:pr-triage:12",
            busy: true,
            name: "PR triage #12",
            session_id: "sess-2",
        });
        const filePath = join(mkdtempSync(join(tmpdir(), "apm-occ-")), "occupancy.json");
        saveOccupancyFile(occupancy, filePath);
        const restored = new Map();
        assert.equal(loadOccupancyFile(restored, filePath), true);
        assert.deepEqual(serializeOccupancy(restored), serializeOccupancy(occupancy));
        const empty = new Map([["stale", { busy: true }]]);
        hydrateOccupancy(empty, serializeOccupancy(occupancy));
        assert.equal(empty.has("stale"), false);
        assert.equal(empty.get("scheduler:pr-triage").sessionId, "sess-1");
    });
});
