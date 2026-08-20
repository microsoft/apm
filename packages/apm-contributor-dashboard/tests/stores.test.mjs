/**
 * Unit tests for Solid.js stores: issues.js and prs.js
 *
 * Run:
 *   cd ~/.copilot/extensions/issue-monitor
 *   node --loader ./tests/solid-loader.mjs --test tests/stores.test.mjs
 *
 * Strategy
 * --------
 * The stores use module-level code (createSignal, createResource, setInterval)
 * that executes on first import.  We therefore:
 *   1. Install a fetch spy and a setInterval spy BEFORE importing either store.
 *   2. Dynamically import both stores inside a before() hook.
 *   3. Verify exports, initial fetch behaviour, and polling mechanics.
 *
 * solid-js notes (server / dev build in Node.js)
 * -----------------------------------------------
 * - createResource(source, fetcher) calls fetcher immediately when the source
 *   signal already has a value (signal(0) -> initial fetch on import).
 * - Advancing the source signal (setPollTick(t => t+1)) triggers a new fetch.
 * - refetch() marks the resource stale but does not synchronously re-invoke
 *   the fetcher in the dev build outside a reactive owner; interval-driven
 *   polling is the intended production path and is what we test here.
 */

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Spies -- installed at module load time so they are in place when the stores
// execute their top-level code during import.
// ---------------------------------------------------------------------------

/** Accumulated URL strings for every fetch() call made by either store. */
const fetchCalls = [];

global.fetch = async (url) => {
  fetchCalls.push(String(url));
  return { ok: true, json: async () => [] };
};

/**
 * Captured setInterval registrations: { fn, ms }[]
 * The stores call setInterval once each at module-load time.
 */
const capturedIntervals = [];
const _origSetInterval = globalThis.setInterval;
globalThis.setInterval = (fn, ms, ...rest) => {
  capturedIntervals.push({ fn, ms });
  // Register the real interval but unref it so it does not keep the Node.js
  // event loop alive after all tests finish.
  const handle = _origSetInterval(fn, ms, ...rest);
  if (typeof handle?.unref === "function") handle.unref();
  return handle;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Wait for microtasks / async resource fetchers to settle. */
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

/** Count how many logged fetch calls contain the given substring. */
const countFetchCalls = (substr) =>
  fetchCalls.filter((u) => u.includes(substr)).length;

// ---------------------------------------------------------------------------
// Shared state populated by before()
// ---------------------------------------------------------------------------

let issuesStore;
let prsStore;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("Solid.js stores", () => {
  let triageStore;

  before(async () => {
    // Dynamic imports execute module-level store code (createSignal,
    // createResource, setInterval) exactly once; ESM caches the modules.
    issuesStore = await import("../client/src/stores/issues.js");
    prsStore = await import("../client/src/stores/prs.js");
    triageStore = await import("../client/src/stores/triage.js");

    // Allow the async resource fetchers (triggered by createResource's initial
    // signal value) to resolve before the first test runs.
    await tick();
  });

  // -------------------------------------------------------------------------
  // issues.js -- exports
  // -------------------------------------------------------------------------

  describe("issues.js -- exports", () => {
    it("exports issueResource", () => {
      assert.ok(
        issuesStore.issueResource !== undefined,
        "issueResource should be exported",
      );
    });

    it("issueResource is callable (Solid.js accessor / signal)", () => {
      assert.strictEqual(typeof issuesStore.issueResource, "function");
    });

    it("exports refetchIssues as a function", () => {
      assert.strictEqual(typeof issuesStore.refetchIssues, "function");
    });
  });

  // -------------------------------------------------------------------------
  // prs.js -- exports
  // -------------------------------------------------------------------------

  describe("prs.js -- exports", () => {
    it("exports prResource", () => {
      assert.ok(
        prsStore.prResource !== undefined,
        "prResource should be exported",
      );
    });

    it("prResource is callable (Solid.js accessor / signal)", () => {
      assert.strictEqual(typeof prsStore.prResource, "function");
    });

    it("exports refetchPrs as a function", () => {
      assert.strictEqual(typeof prsStore.refetchPrs, "function");
    });
  });

  // -------------------------------------------------------------------------
  // Initial data fetch (createResource fires fetcher on first signal value)
  // -------------------------------------------------------------------------

  describe("initial data fetch on import", () => {
    it("issues store fetches /api/issues on load", () => {
      assert.ok(
        countFetchCalls("/api/issues") >= 1,
        `Expected at least one /api/issues fetch on load; calls: ${fetchCalls}`,
      );
    });

    it("prs store fetches /api/prs on load", () => {
      assert.ok(
        countFetchCalls("/api/prs") >= 1,
        `Expected at least one /api/prs fetch on load; calls: ${fetchCalls}`,
      );
    });
  });

  // -------------------------------------------------------------------------
  // Polling interval registration
  // -------------------------------------------------------------------------

  describe("30-second polling interval", () => {
    it("registers exactly two intervals with a 30 000 ms period (one per store)", () => {
      const thirtySecIntervals = capturedIntervals.filter(
        (i) => i.ms === 30_000,
      );
      assert.strictEqual(
        thirtySecIntervals.length,
        2,
        `Expected 2 x 30 s intervals, got ${thirtySecIntervals.length}`,
      );
    });

    it("issues store interval advances pollTick and triggers a new /api/issues fetch", async () => {
      // The issues store is imported first, so capturedIntervals[0] is its interval.
      const issuesInterval = capturedIntervals.filter(
        (i) => i.ms === 30_000,
      )[0];
      assert.ok(issuesInterval, "issues store interval should be registered");

      const before = countFetchCalls("/api/issues");
      issuesInterval.fn(); // fire the interval callback (setPollTick(t => t+1))
      await tick();
      const after = countFetchCalls("/api/issues");

      assert.ok(
        after > before,
        `Expected a new /api/issues fetch after interval fires (before: ${before}, after: ${after})`,
      );
    });

    it("prs store interval advances pollTick and triggers a new /api/prs fetch", async () => {
      // The prs store is imported second, so capturedIntervals[1] is its interval.
      const prsInterval = capturedIntervals.filter(
        (i) => i.ms === 30_000,
      )[1];
      assert.ok(prsInterval, "prs store interval should be registered");

      const before = countFetchCalls("/api/prs");
      prsInterval.fn(); // fire the interval callback
      await tick();
      const after = countFetchCalls("/api/prs");

      assert.ok(
        after > before,
        `Expected a new /api/prs fetch after interval fires (before: ${before}, after: ${after})`,
      );
    });
  });

  // -------------------------------------------------------------------------
  // Interval isolation -- each store's interval only affects its own endpoint
  // -------------------------------------------------------------------------

  describe("interval isolation", () => {
    it("firing the issues interval does NOT add a /api/prs fetch", async () => {
      const issuesInterval = capturedIntervals.filter(
        (i) => i.ms === 30_000,
      )[0];
      const prsBefore = countFetchCalls("/api/prs");
      issuesInterval.fn();
      await tick();
      const prsAfter = countFetchCalls("/api/prs");

      assert.strictEqual(
        prsAfter,
        prsBefore,
        "Issues interval should not trigger /api/prs",
      );
    });

    it("firing the prs interval does NOT add a /api/issues fetch", async () => {
      const prsInterval = capturedIntervals.filter(
        (i) => i.ms === 30_000,
      )[1];
      const issuesBefore = countFetchCalls("/api/issues");
      prsInterval.fn();
      await tick();
      const issuesAfter = countFetchCalls("/api/issues");

      assert.strictEqual(
        issuesAfter,
        issuesBefore,
        "Prs interval should not trigger /api/issues",
      );
    });
  });

  // -------------------------------------------------------------------------
  // triage.js -- lazy store
  // -------------------------------------------------------------------------

  describe("triage.js -- exports", () => {
    it("exports triageResource as a function", () => {
      assert.strictEqual(typeof triageStore.triageResource, "function");
    });

    it("exports refetchTriage as a function", () => {
      assert.strictEqual(typeof triageStore.refetchTriage, "function");
    });

    it("exports activateTriage as a function", () => {
      assert.strictEqual(typeof triageStore.activateTriage, "function");
    });
  });

  describe("triage.js -- lazy-load contract", () => {
    it("does NOT fetch /api/triage on import (lazy until activateTriage())", () => {
      // fetchCalls is populated before the import; triage store uses fetchTick=null
      // so createResource fires the fetcher with null and returns early -- no HTTP call.
      assert.strictEqual(
        countFetchCalls("/api/triage"),
        0,
        "triage store must not fetch on import; fetchTick starts as null",
      );
    });

    it("fetches /api/triage after activateTriage() is called", async () => {
      const before = countFetchCalls("/api/triage");
      triageStore.activateTriage();
      await tick(50);
      const after = countFetchCalls("/api/triage");
      assert.ok(
        after > before,
        `Expected a /api/triage fetch after activateTriage() (before: ${before}, after: ${after})`,
      );
    });

    it("calling activateTriage() a second time does NOT trigger another fetch (idempotent)", async () => {
      // activateTriage() sets fetchTick from null to 0 only on first call.
      const before = countFetchCalls("/api/triage");
      triageStore.activateTriage();
      await tick(50);
      const after = countFetchCalls("/api/triage");
      assert.strictEqual(
        after,
        before,
        "activateTriage() should be idempotent -- no extra fetch on repeated calls",
      );
    });
  });
});
