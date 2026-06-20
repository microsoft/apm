// Test suite for api.js service layer
// Run: node --test tests/api-service.test.mjs

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Fetch mock -- must be in place before the module is imported so the module's
// module-level `fetch` reference is already mocked.
// ---------------------------------------------------------------------------

let fetchCalls = [];

function mockFetch({ ok = true, status = 200, statusText = "OK", json = { ok: true } } = {}) {
  globalThis.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    return { ok, status, statusText, json: async () => json };
  };
}

// Install a default success mock before the dynamic import so the module loads fine.
mockFetch();

// Dynamic import -- happens once; the mock is already wired into globalThis.fetch.
const api = await import("../client/src/services/api.js");

// Helper: reset recorded calls and install a fresh success mock before each test.
function setup(overrides) {
  fetchCalls = [];
  mockFetch(overrides);
}

// ---------------------------------------------------------------------------
// GET endpoints
// ---------------------------------------------------------------------------

describe("getIssues", () => {
  it("calls GET /api/issues", async () => {
    setup();
    await api.getIssues();
    assert.equal(fetchCalls.length, 1);
    assert.equal(fetchCalls[0].url, "/api/issues");
  });

  it("does not set a method (native GET)", async () => {
    setup();
    await api.getIssues();
    assert.equal(fetchCalls[0].opts, undefined);
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: [{ number: 1 }] });
    const result = await api.getIssues();
    assert.deepEqual(result, [{ number: 1 }]);
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 500, statusText: "Internal Server Error" });
    await assert.rejects(() => api.getIssues(), /500/);
  });
});

describe("getPrs", () => {
  it("calls GET /api/prs", async () => {
    setup();
    await api.getPrs();
    assert.equal(fetchCalls[0].url, "/api/prs");
  });

  it("does not set a method (native GET)", async () => {
    setup();
    await api.getPrs();
    assert.equal(fetchCalls[0].opts, undefined);
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: [{ number: 42 }] });
    const result = await api.getPrs();
    assert.deepEqual(result, [{ number: 42 }]);
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 404, statusText: "Not Found" });
    await assert.rejects(() => api.getPrs(), /404/);
  });
});

describe("getIssueDetail", () => {
  it("calls GET /api/issue/{number}", async () => {
    setup();
    await api.getIssueDetail(7);
    assert.equal(fetchCalls[0].url, "/api/issue/7");
  });

  it("interpolates the number correctly", async () => {
    setup();
    await api.getIssueDetail(123);
    assert.equal(fetchCalls[0].url, "/api/issue/123");
  });

  it("does not set a method (native GET)", async () => {
    setup();
    await api.getIssueDetail(1);
    assert.equal(fetchCalls[0].opts, undefined);
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { number: 7, title: "Test" } });
    const result = await api.getIssueDetail(7);
    assert.deepEqual(result, { number: 7, title: "Test" });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 404, statusText: "Not Found" });
    await assert.rejects(() => api.getIssueDetail(99), /404/);
  });
});

describe("getPrDetail", () => {
  it("calls GET /api/pr/{number}", async () => {
    setup();
    await api.getPrDetail(5);
    assert.equal(fetchCalls[0].url, "/api/pr/5");
  });

  it("interpolates the number correctly", async () => {
    setup();
    await api.getPrDetail(456);
    assert.equal(fetchCalls[0].url, "/api/pr/456");
  });

  it("does not set a method (native GET)", async () => {
    setup();
    await api.getPrDetail(1);
    assert.equal(fetchCalls[0].opts, undefined);
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { number: 5, state: "open" } });
    const result = await api.getPrDetail(5);
    assert.deepEqual(result, { number: 5, state: "open" });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 403, statusText: "Forbidden" });
    await assert.rejects(() => api.getPrDetail(5), /403/);
  });
});

// ---------------------------------------------------------------------------
// POST endpoints -- shared helper to verify JSON POST shape
// ---------------------------------------------------------------------------

function assertJsonPost(call, url, expectedBody) {
  assert.equal(call.url, url);
  assert.equal(call.opts.method, "POST");
  assert.equal(call.opts.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(call.opts.body), expectedBody);
}

describe("startSession", () => {
  it("calls POST /start-session with number and title", async () => {
    setup();
    await api.startSession(10, "Fix the bug");
    assertJsonPost(fetchCalls[0], "/start-session", { number: 10, title: "Fix the bug" });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { sessionId: "abc" } });
    const result = await api.startSession(1, "t");
    assert.deepEqual(result, { sessionId: "abc" });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 400, statusText: "Bad Request" });
    await assert.rejects(() => api.startSession(1, "t"), /400/);
  });
});

describe("openSession", () => {
  it("calls POST /open-session with number and title", async () => {
    setup();
    await api.openSession(20, "Open PR review");
    assertJsonPost(fetchCalls[0], "/open-session", { number: 20, title: "Open PR review" });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { ok: true } });
    const result = await api.openSession(2, "t");
    assert.deepEqual(result, { ok: true });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 500, statusText: "Internal Server Error" });
    await assert.rejects(() => api.openSession(2, "t"), /500/);
  });
});

describe("runPanel", () => {
  it("calls POST /run-panel with number", async () => {
    setup();
    await api.runPanel(30);
    assertJsonPost(fetchCalls[0], "/run-panel", { number: 30 });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { status: "running" } });
    const result = await api.runPanel(3);
    assert.deepEqual(result, { status: "running" });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 502, statusText: "Bad Gateway" });
    await assert.rejects(() => api.runPanel(3), /502/);
  });
});

describe("rerunCi", () => {
  it("calls POST /approve-pipeline with number", async () => {
    setup();
    await api.rerunCi(40);
    assertJsonPost(fetchCalls[0], "/approve-pipeline", { number: 40 });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { queued: true } });
    const result = await api.rerunCi(4);
    assert.deepEqual(result, { queued: true });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 422, statusText: "Unprocessable Entity" });
    await assert.rejects(() => api.rerunCi(4), /422/);
  });
});

describe("approvePr", () => {
  it("calls POST /approve-pr with number", async () => {
    setup();
    await api.approvePr(50);
    assertJsonPost(fetchCalls[0], "/approve-pr", { number: 50 });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { approved: true } });
    const result = await api.approvePr(5);
    assert.deepEqual(result, { approved: true });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 403, statusText: "Forbidden" });
    await assert.rejects(() => api.approvePr(5), /403/);
  });
});

describe("approveWorkflowRuns", () => {
  it("calls POST /approve-workflow-runs with branch", async () => {
    setup();
    await api.approveWorkflowRuns("main");
    assertJsonPost(fetchCalls[0], "/approve-workflow-runs", { branch: "main" });
  });

  it("sends the branch name as-is", async () => {
    setup();
    await api.approveWorkflowRuns("feature/my-branch");
    assert.deepEqual(JSON.parse(fetchCalls[0].opts.body), { branch: "feature/my-branch" });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { count: 3 } });
    const result = await api.approveWorkflowRuns("main");
    assert.deepEqual(result, { count: 3 });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 404, statusText: "Not Found" });
    await assert.rejects(() => api.approveWorkflowRuns("ghost-branch"), /404/);
  });
});

describe("mergeWhenReady", () => {
  it("calls POST /merge-when-ready with number", async () => {
    setup();
    await api.mergeWhenReady(60);
    assertJsonPost(fetchCalls[0], "/merge-when-ready", { number: 60 });
  });

  it("returns parsed JSON on success", async () => {
    setup({ json: { scheduled: true } });
    const result = await api.mergeWhenReady(6);
    assert.deepEqual(result, { scheduled: true });
  });

  it("throws with status code on non-ok response", async () => {
    setup({ ok: false, status: 409, statusText: "Conflict" });
    await assert.rejects(() => api.mergeWhenReady(6), /409/);
  });
});
