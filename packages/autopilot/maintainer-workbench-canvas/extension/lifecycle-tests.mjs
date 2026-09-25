import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { fixtureStore, fixtureSnapshot, scopeFields, scopeBody, comment, trustFixture, now, authority } from "./fixtures.mjs";
import { Governance, reconcileIssue } from "./governance.mjs";
import { buildModel } from "./model.mjs";
import { defaultView, projectView, restoreView, VIEW_SCHEMA } from "./scope.mjs";
import { Bridge } from "./bridge.mjs";
import { startServer } from "./server.mjs";
import { STORE, AUTO_REFRESH, HISTORY_TTL, TTL } from "./config.mjs";
import { ACTIONS, executionContract } from "./actions.mjs";

async function setup(t, options) {
  const base = join(STORE, "test-runs"); await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, "lifecycle-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return fixtureStore(root, options);
}
const target = number => `microsoft/apm/issue/${number}`;
const preview = (store, kind = "defer", number = 1, params = { reason: "Park this controlled fixture." }) =>
  store.bridge.preview({ id: randomUUID(), kind, targetIds: kind === "smoke" ? [] : [typeof number === "string" ? number : target(number)], params, version: 1 });
async function claimed(store, kind, number, params) {
  const p = await preview(store, kind, number, params);
  const sent = await store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel");
  return store.bridge.claim({ id: p.id, revision: sent.revision });
}
const receipt = (step, outcome = "verified") => ({ step, outcome, tool: "fixture-tool", reference: "fixture-only-readback", observedAt: now() });
const gate = (planApproved = false) => ({ tool: "ask_user", reference: "fixture-gate-not-live", actor: "fixture-maintainer", observedAt: now(), confirmed: true, planApproved });
const result = (r, patch = {}) => ({ id: r.id, revision: r.revision, claimToken: r.claimToken, status: "completed", message: "Fixture result.", ...patch });

test("lifecycle views overlap by unique issue; PRs do not inflate roadmap and partial PR cannot complete issue", () => {
  const model = buildModel(fixtureSnapshot());
  const get = scope => projectView(model.items, { ...defaultView(), scope }).visible;
  assert.equal(defaultView().scope, "decisions");
  assert.deepEqual(get("roadmap").map(i => i.number).sort(), [2, 3, 4, 7]);
  assert.equal(get("roadmap").find(i => i.number === 3).lifecycle.plannedUnverified, true);
  assert.ok(get("delivery").some(i => i.number === 4));
  assert.ok(get("roadmap").some(i => i.number === 4));
  assert.equal(get("deferred")[0].number, 5);
  assert.equal(get("history")[0].number, 6);
  assert.equal(get("admission")[0].number, 21);
  assert.ok(get("roadmap").every(i => i.kind === "issue"));
  assert.equal(new Set(get("roadmap").map(i => i.id)).size, get("roadmap").length);
  assert.equal(model.items.find(i => i.number === 4).state, "OPEN");
});

test("old triage advice does not reopen accepted/deferred/design dispositions; context and contact consent remain", () => {
  const model = buildModel(fixtureSnapshot());
  for (const number of [2, 5, 7]) {
    const i = model.items.find(i => i.kind === "issue" && i.number === number);
    assert.equal(i.lifecycle.needsDecision, false);
    assert.equal(i.lifecycle.record.recommendation.status, "retained-context");
  }
  const accepted = model.items.find(i => i.number === 2);
  assert.equal(accepted.authority.authorizes_implementation, false);
  assert.equal(accepted.lifecycle.record.acceptance.contact_confirmation_needed, true);
  assert.equal(model.items.find(i => i.number === 7).lifecycle.design, true);
});

test("TTL expiry retains last-known decisions, roadmap, deferral and selection without authorizing actions", async t => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-09-25T12:00:00Z") });
  let sends = 0;
  const store = await setup(t, { send: async () => { sends++; } });
  const before = store.state();
  const ids = (model, scope) => projectView(model.items, { ...defaultView(), scope }).visible.map(i => i.id);
  t.mock.timers.tick(TTL + 1);
  const after = store.state();
  for (const scope of ["decisions", "roadmap", "deferred"]) assert.deepEqual(ids(after, scope), ids(before, scope));
  assert.equal(after.view.selectedId, before.view.selectedId);
  const accepted = after.items.find(i => i.number === 2);
  assert.equal(accepted.lifecycle.accepted, true);
  assert.equal(accepted.lifecycle.lastKnown, true);
  assert.equal(accepted.lifecycle.current, false);
  assert.equal(accepted.authority.verified, false);
  assert.equal(accepted.authority.authorizes_implementation, false);
  assert.match(accepted.lifecycle.explanation, /Last-known/);
  store.snapshot.lifecycle.actor.observedAt = now();
  for (const source of Object.values(store.snapshot.sources)) source.observedAt = now();
  const p = await preview(store);
  await assert.rejects(() => store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel"), /Stale or incomplete issue evidence/);
  assert.equal(sends, 0);
});

test("live HTTP polling refreshes GitHub once, keeps rows during preparation, and publishes real changes atomically", async t => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-09-25T12:00:00Z") });
  const store = await setup(t);
  const initial = store.snapshot, selected = store.view.selectedId;
  const initialIds = store.state().projection.visibleIds;
  let reads = 0, release, preparing;
  const began = new Promise(resolve => { preparing = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  store.github.portfolio = async () => {
    reads++;
    const next = fixtureSnapshot();
    next.issues.nodes[0].state = "CLOSED";
    next.roadmap.nodes[0].content.state = "CLOSED";
    return next;
  };
  store.governance.prepare = async snapshot => { preparing(); await held; return snapshot.lifecycle; };
  const server = await startServer(store);
  t.after(() => server.close());
  await fetch(`${server.url}api/state`);
  assert.equal(reads, 0);
  t.mock.timers.tick(TTL + 1);
  const first = await (await fetch(`${server.url}api/state`)).json();
  await began;
  const pending = store.pending;
  assert.equal(first.refreshing, true);
  assert.deepEqual(first.projection.visibleIds, initialIds);
  assert.equal(store.snapshot, initial);
  assert.equal(store.state().view.selectedId, selected);
  await fetch(`${server.url}api/state`);
  assert.equal(reads, 1);
  release(); await pending;
  const final = await (await fetch(`${server.url}api/state`)).json();
  assert.equal(final.refreshing, false);
  assert.equal(reads, 1);
  assert.ok(!final.projection.visibleIds.includes(selected));
  assert.equal(final.items.find(i => i.id === selected).state, "CLOSED");
  assert.equal(final.error, null);
  assert.equal(final.autoRefreshSeconds, AUTO_REFRESH / 1000);
});

test("failed automatic refresh preserves data and selection and bounds retries", async t => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-09-25T12:00:00Z") });
  const store = await setup(t);
  const before = store.state();
  let reads = 0;
  store.github.portfolio = async () => { reads++; throw new Error("GitHub temporarily unavailable"); };
  t.mock.timers.tick(TTL + 1);
  store.ensureFresh(); await store.pending;
  const after = store.state();
  assert.match(after.error, /GitHub temporarily unavailable/);
  assert.deepEqual(after.projection.visibleIds, before.projection.visibleIds);
  assert.equal(after.view.selectedId, before.view.selectedId);
  assert.equal(after.refreshing, false);
  assert.equal(after.preparation, null);
  store.ensureFresh(); store.ensureFresh();
  assert.equal(reads, 1);
  t.mock.timers.tick(AUTO_REFRESH);
  store.ensureFresh(); await store.pending;
  assert.equal(reads, 2);
});

test("unchanged histories are reused without extending action freshness; changed or old histories are reread", async t => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-09-25T12:00:00Z") });
  const snapshot = fixtureSnapshot(), governance = new Governance({});
  governance.trust = async () => trustFixture();
  const reads = [];
  governance.issue = async number => {
    reads.push(number);
    return { ...snapshot.lifecycle.issues[number], observedAt: now(), updatedAt: snapshot.issues.nodes.find(i => i.number === number).updatedAt };
  };
  const originalObservation = snapshot.lifecycle.issues[1].observedAt;
  t.mock.timers.tick(TTL + 1);
  snapshot.lifecycle = await governance.prepare(snapshot);
  assert.deepEqual(reads, []);
  assert.equal(snapshot.lifecycle.issues[1].observedAt, originalObservation);
  assert.equal(buildModel(snapshot).items.find(i => i.number === 1).lifecycle.current, false);
  snapshot.issues.nodes[1].updatedAt = now();
  snapshot.lifecycle = await governance.prepare(snapshot);
  assert.deepEqual(reads, [2]);
  reads.length = 0;
  t.mock.timers.tick(HISTORY_TTL);
  await governance.prepare(snapshot);
  assert.deepEqual(reads.sort(), [1, 2, 3, 4, 5, 7]);
});

test("automatic refresh rechecks the selected discussion without rereading every unchanged history", async t => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-09-25T12:00:00Z") });
  const snapshot = fixtureSnapshot(), governance = new Governance({});
  governance.trust = async () => trustFixture();
  const reads = [];
  governance.issue = async number => {
    reads.push(number);
    return { ...snapshot.lifecycle.issues[number], observedAt: now() };
  };
  t.mock.timers.tick(TTL + 1);
  snapshot.lifecycle = await governance.prepare(snapshot, () => {}, { selectedIssue: 1 });
  assert.deepEqual(reads, [1]);
  const items = buildModel(snapshot).items;
  assert.equal(items.find(i => i.number === 1).lifecycle.current, true);
  assert.equal(items.find(i => i.number === 2).lifecycle.lastKnown, true);
});

test("labels alone and unavailable/partial/stale context never prove acceptance, no assessment or zero runs", () => {
  const s = fixtureSnapshot();
  delete s.lifecycle;
  s.issues.nodes[0].labels.nodes.push({ name: "status/accepted" });
  let item = buildModel(s).items.find(i => i.number === 1);
  assert.equal(item.lifecycle.accepted, false);
  assert.equal(item.lifecycle.needsDecision, false);
  assert.equal(item.lifecycle.stage, "Checking decision history");
  assert.equal(item.lifecycle.runStatus, "Run status unavailable");
  s.lifecycle = fixtureSnapshot().lifecycle;
  s.lifecycle.issues[1].status = "error";
  s.lifecycle.issues[1].error = "Incomplete pagination";
  item = buildModel(s).items.find(i => i.number === 1);
  assert.match(item.lifecycle.explanation, /Incomplete/);
  s.lifecycle.issues[1].status = "current";
  s.lifecycle.issues[1].observedAt = "2000-01-01T00:00:00Z";
  assert.equal(buildModel(s).items.find(i => i.number === 1).lifecycle.current, false);
});

test("canonical authority rejects edited, withdrawn and bot records, not a second scope parser", () => {
  const issue = { number: 1, state: "open", updated_at: now(), labels: [] };
  const approve = comment(1, scopeBody(), "fixture-maintainer", 10);
  const trust = trustFixture();
  assert.equal(reconcileIssue({ issue, comments: [approve], timeline: [], trust }).acceptance.state, "record-present");
  const edited = { ...approve, updated_at: now() };
  assert.equal(reconcileIssue({ issue, comments: [edited], timeline: [], trust }).acceptance.state, "needs-evidence");
  const withdrawal = comment(2, scopeBody("withdraw"));
  assert.equal(reconcileIssue({ issue, comments: [approve, withdrawal], timeline: [], trust }).acceptance.state, "withdrawn");
  const bot = { ...approve, user: { login: "fixture-maintainer", type: "Bot" } };
  assert.notEqual(reconcileIssue({ issue, comments: [bot], timeline: [], trust }).acceptance.state, "record-present");
  assert.equal(authority.parseRecord(`${scopeBody()}\nAI generated footer`), null);
});

test("later responsible human discussion suppresses a repeated recommendation rather than inventing a decision", () => {
  const issue = { number: 1, state: "open", updated_at: now(), labels: [] };
  const advisory = comment(1, "<!-- apm-triage-advisory:v2 target=issue#1 watermark=1 -->", "fixture-adviser", 10);
  const r = reconcileIssue({ issue, comments: [advisory, comment(2, "Discuss this existing proposal before repeating advice.")], timeline: [], trust: trustFixture() });
  assert.equal(r.recommendation.status, "retained-context");
  assert.match(r.followup, /reconciliation/);
  assert.notEqual(r.acceptance.state, "record-present");
});

test("schema 1/2 migrate safely to decisions; schema 3 restores explicit overlapping view and selection", () => {
  const model = buildModel(fixtureSnapshot());
  for (const schema of [1, 2]) {
    const r = restoreView({ schema, data: { kind: "work", scope: "roadmap-open", selectedId: target(2), search: "old" } }, model.items);
    assert.equal(r.view.scope, "decisions"); assert.equal(r.view.search, ""); assert.notEqual(r.view.selectedId, target(2));
    assert.match(r.notice, /View updated/);
  }
  const r = restoreView({ schema: VIEW_SCHEMA, data: { ...defaultView(), scope: "roadmap", selectedId: target(2) } }, model.items);
  assert.equal(r.view.selectedId, target(2)); assert.equal(r.migrated, false);
});

test("duplicate clicks and concurrent confirmations send once; acknowledgement is not running", async t => {
  let sends = 0;
  const store = await setup(t, { send: async () => { sends++; await new Promise(resolve => setTimeout(resolve, 5)); } });
  const p = await preview(store);
  const input = { id: p.id, revision: p.revision, watermark: p.watermark };
  const results = await Promise.all([store.bridge.confirm(input, "fixture-panel"), store.bridge.confirm(input, "fixture-panel")]);
  assert.equal(sends, 1); assert.equal(results[0].status, "awaiting-host");
  assert.equal(store.bridge.state().requests[0].claimToken, undefined);
  await assert.rejects(() => preview(store), /Conflict/);
});

test("immediate transport awaits admission, records SDK IDs and preserves gates and duplicate protection", async t => {
  const sends = [];
  let started, acknowledge;
  const sending = new Promise(resolve => { started = resolve; });
  const store = await setup(t, { send: options => {
    sends.push(options); started();
    return new Promise(resolve => { acknowledge = resolve; });
  } });
  const p = await preview(store);
  const input = { id: p.id, revision: p.revision, watermark: p.watermark };
  const pending = store.bridge.confirm(input, "fixture-panel");
  await sending;
  assert.deepEqual(Object.keys(sends[0]).sort(), ["mode", "prompt"]);
  assert.equal(sends[0].mode, "immediate");
  assert.ok(sends[0].prompt.includes(p.id));
  assert.equal(store.bridge.get(p.id).status, "sending");
  acknowledge("fixture-sdk-admitted-message");
  const sent = await pending;
  assert.equal(sent.transportMessageId, "fixture-sdk-admitted-message");
  assert.equal(sent.status, "awaiting-host");
  assert.equal(sent.gate, undefined);
  await store.bridge.confirm(input, "fixture-panel");
  assert.equal(sends.length, 1);
  const claim = await store.bridge.claim({ id: sent.id, revision: sent.revision });
  await assert.rejects(() => store.bridge.report(result(claim, { status: "running" })), /host confirmation/);
  store.bridge.send = async options => { sends.push(options); return "fixture-sdk-cancellation-message"; };
  const cancelled = await store.bridge.cancel({ id: claim.id, revision: claim.revision }, "fixture-panel");
  assert.equal(sends[1].mode, "immediate");
  assert.deepEqual(Object.keys(sends[1]).sort(), ["mode", "prompt"]);
  assert.equal(cancelled.status, "cancel-requested");
  assert.equal(cancelled.cancellationMessageId, "fixture-sdk-cancellation-message");
  const reloaded = new Bridge(store, { send: async () => { throw new Error("Reload must not send"); } });
  await reloaded.load();
  assert.equal(reloaded.get(p.id).transportMessageId, sent.transportMessageId);
  assert.equal(reloaded.get(p.id).cancellationMessageId, cancelled.cancellationMessageId);
  assert.equal(reloaded.get(p.id).status, "reconcile-required");
  assert.equal(sends.length, 2);
});

test("preview races lock at confirmation and same IDs cannot carry changed effects", async t => {
  const store = await setup(t);
  const p = await preview(store), other = await preview(store, "design", 1, { reason: "Design boundary." });
  await store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel");
  await assert.rejects(() => store.bridge.confirm({ id: other.id, revision: other.revision, watermark: other.watermark }, "fixture-panel"), /Conflict/);
  await assert.rejects(() => store.bridge.preview({ id: p.id, version: 1, kind: "defer", targetIds: [target(1)], params: { reason: "Changed" } }), /different preview/);
});

test("stale sources/head/actor invalidate confirmation and host claim becomes reconciliation only", async t => {
  const store = await setup(t);
  const p = await preview(store);
  store.snapshot.issues.nodes[0].updatedAt = "2000-01-01T00:00:00Z";
  await assert.rejects(() => store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel"), /Stale/);
  store.snapshot = fixtureSnapshot();
  const fresh = await preview(store);
  const sent = await store.bridge.confirm({ id: fresh.id, revision: fresh.revision, watermark: fresh.watermark }, "fixture-panel");
  store.snapshot.lifecycle.actor.canWrite = false;
  const claim = await store.bridge.claim({ id: fresh.id, revision: sent.revision });
  assert.equal(claim.reconcileOnly, true);
  await assert.rejects(() => store.bridge.report(result(claim, { status: "running", gate: gate() })), /reconciled/);
});

test("transport ambiguity and reload never auto-resend; corrupt journal blocks actions", async t => {
  let sends = 0;
  const store = await setup(t, { send: async () => { sends++; throw new Error("Ambiguous transport"); } });
  const p = await preview(store);
  const sent = await store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel");
  assert.equal(sent.status, "reconcile-required");
  const reloaded = new Bridge(store, { send: async () => { sends++; } });
  await reloaded.load();
  assert.equal(reloaded.get(p.id).status, "reconcile-required"); assert.equal(sends, 1);
  await writeFile(reloaded.path, "{bad");
  const broken = new Bridge(store); await broken.load();
  assert.match(broken.error, /journal/);
  await assert.rejects(() => broken.preview({}), /journal/);
});

test("scope composer validates canonical record and requires actual host gate plus ordered comment/readback metadata receipts", async t => {
  const store = await setup(t);
  const r = await claimed(store, "scope-accept", 1, scopeFields);
  assert.equal(authority.parseRecord(r.scopeRecord.body).Decision, "approve");
  assert.equal(r.scopeRecord.reviewContactConfirmationNeeded, true);
  await assert.rejects(() => store.bridge.report(result(r, { receipts: [receipt("comment-readback"), receipt("metadata")] })), /host confirmation/);
  await assert.rejects(() => store.bridge.report(result(r, { gate: gate(), receipts: [receipt("metadata"), receipt("comment-readback")] })), /precede/);
  assert.equal(store.bridge.get(r.id).gate, undefined);
  await assert.rejects(() => store.bridge.report(result(r, { claimToken: "wrong" })), /claim token/);
  const partial = await store.bridge.report(result(r, { status: "partial", gate: gate(), receipts: [receipt("comment-readback"), receipt("metadata", "failed")] }));
  assert.equal(partial.status, "partial");
  await assert.rejects(() => store.bridge.report(result(r, { revision: partial.revision, receipts: [receipt("metadata")] })), /unresolved failures/);
});

test("scope acceptance completion is a receipt, not delivery permission; drafting is a separate request", async t => {
  const store = await setup(t);
  const draft = await claimed(store, "scope-draft", 1, {});
  const done = await store.bridge.report(result(draft, { draft: scopeFields, receipts: [receipt("draft")] }));
  assert.deepEqual(done.draft, scopeFields);
  assert.equal(store.snapshot.lifecycle.issues[1].acceptance.state, "needs-evidence");
  const accept = await claimed(store, "scope-accept", 1, done.draft);
  const accepted = await store.bridge.report(result(accept, { gate: gate(), receipts: [receipt("comment-readback"), receipt("metadata")] }));
  assert.equal(accepted.status, "completed");
  assert.equal(store.bridge.runs.length, 0);
  await assert.rejects(() => store.bridge.report(result(accept)), /terminal|revision/);
});

test("implementation rejects existing PRs and demands nominated scope; recover enforces a real plan and observed session", async t => {
  const store = await setup(t);
  await assert.rejects(() => preview(store, "implement", 1, { reason: "Fix", approvalUrl: "https://github.com/microsoft/apm/issues/1#issuecomment-1" }), /nominated/);
  const approval = store.snapshot.lifecycle.issues[4].acceptance.approvalUrl;
  await assert.rejects(() => preview(store, "implement", 4, { reason: "Fix", approvalUrl: approval }), /Existing contribution/);
  const recover = await claimed(store, "recover", "microsoft/apm/pr/20", { reason: "Address fixture review only." });
  await assert.rejects(() => store.bridge.report(result(recover, { status: "running", gate: gate(false), receipts: [receipt("session-observed")] })), /plan approval/);
  await assert.rejects(() => store.bridge.report(result(recover, { status: "running", gate: gate(true) })), /session receipt/);
  const running = await store.bridge.report(result(recover, { status: "running", gate: gate(true), receipts: [receipt("session-observed")] }));
  assert.equal(running.status, "running");
});

test("cancel before send is local; active cancellation requires parent readback and prevents more execution", async t => {
  const store = await setup(t);
  const p = await preview(store);
  const cancelled = await store.bridge.cancel({ id: p.id, revision: p.revision }, "fixture-panel");
  assert.equal(cancelled.status, "cancelled");
  const r = await claimed(store, "defer", 1, { reason: "Park" });
  const pending = await store.bridge.cancel({ id: r.id, revision: r.revision }, "fixture-panel");
  assert.equal(pending.status, "cancel-requested");
  await assert.rejects(() => store.bridge.report(result(r, { revision: pending.revision, status: "running", gate: gate() })), /Cancellation/);
  const done = await store.bridge.report(result(r, { revision: pending.revision, status: "cancelled", receipts: [receipt("no-effects")] }));
  assert.equal(done.status, "cancelled");
});

test("run feed requires mapped target/mandate/tool evidence; completed runs are not resumable", async t => {
  const store = await setup(t);
  const report = { tool: "get_sessions_status", reference: "fixture-status", observedAt: now(), complete: false,
    runs: [{ id: "mapped-run", sessionId: "fixture-session", name: "Fixture run", targetIds: [target(2)], kind: "delivery",
      state: "waiting-human", actor: "fixture-maintainer", mandate: "Fixture bounded mandate", blocker: "Fixture blocker" }] };
  await store.bridge.reportRuns(report);
  const item = store.state().items.find(i => i.number === 2);
  assert.equal(item.lifecycle.needsDecision, true);
  assert.match(item.execution.name, /waiting-human/);
  const resume = await preview(store, "resume", 2, { runId: "mapped-run", reason: "Inspect resolved fixture blocker." });
  assert.equal(resume.kind, "resume");
  report.runs[0].state = "completed"; await store.bridge.reportRuns(report);
  await assert.rejects(() => preview(store, "resume", 2, { runId: "mapped-run", reason: "Resume" }), /No fresh/);
  await assert.rejects(() => store.bridge.reportRuns({ ...report, observedAt: "2000-01-01T00:00:00Z" }), /Invalid/);
});

test("catalog preserves exact disposition/publication/reviewer/plan boundaries", () => {
  assert.match(ACTIONS.defer.effects, /Do not close.*withdraw scope/);
  assert.match(ACTIONS.design.effects, /Preserve acceptance/);
  assert.match(ACTIONS.decline.effects, /not planned/);
  assert.match(ACTIONS.admission.effects, /MAY add status\/deferred/);
  assert.match(ACTIONS.review.effects, /Scheduler never requests/);
  assert.match(ACTIONS.recover.effects, /real plan/);
  const contract = executionContract({ kind: "implement", targetIds: [target(2)], params: {} });
  assert.match(contract.safety.join("\n"), /contact_confirmation_needed/);
  assert.match(contract.safety.join("\n"), /Do not fabricate a HUMAN_SCOPE_RECEIPT/);
});

test("fixture-only HTTP -> awaited transport -> parent claim/report -> state roundtrip has no mutation route", async t => {
  const prompts = [];
  const store = await setup(t, { send: async value => { prompts.push(value.prompt); }, fixtureOnly: true });
  const server = await startServer(store, { instanceId: "safe-smoke-panel" }); t.after(() => server.close());
  const html = await (await fetch(server.url)).text(), token = /name="canvas-token" content="([^"]+)"/.exec(html)[1];
  const post = (path, body, secret = token) => fetch(new URL(path, server.url), { method: "POST", headers: { "Content-Type": "application/json", "X-Canvas-Token": secret }, body: JSON.stringify(body) });
  const p = await (await post("/api/requests/preview", { version: 1, id: randomUUID(), kind: "smoke", targetIds: [], params: {} })).json();
  const sent = await (await post("/api/requests/confirm", { id: p.id, revision: p.revision, watermark: p.watermark })).json();
  assert.equal(sent.status, "awaiting-host"); assert.equal(prompts.length, 1); assert.match(prompts[0], /FIXTURE ONLY/);
  const r = await store.bridge.claim({ id: sent.id, revision: sent.revision });
  await store.bridge.report(result(r, { receipts: [receipt("fixture")] }));
  const state = await (await fetch(new URL("/api/state", server.url))).json();
  assert.equal(state.bridge.requests[0].status, "completed");
  assert.equal(state.bridge.requests[0].claimToken, undefined);
  assert.equal((await post("/api/requests/report", result(r))).status, 404);
  assert.equal((await post("/api/requests/confirm", {}, "bad")).status, 403);
  assert.equal((await post("/api/merge", {})).status, 404);
  assert.notEqual((await post("/api/requests/preview", { version: 1, id: randomUUID(), kind: "defer", targetIds: [target(1)], params: { reason: "no" } })).status, 200);
});

test("canonical metadata pagination uses issue events, not timeline objects lacking numeric IDs", async () => {
  const routes = [], trust = trustFixture(), governance = new Governance({});
  trust.client = { get: async route => {
    routes.push(route);
    if (/\/issues\/1$/.test(route)) return { number: 1, state: "open", labels: [], updated_at: now() };
    if (route.includes("/comments?")) return [comment(1, scopeBody())];
    if (route.includes("/events?")) return [{ id: 2, event: "labeled", created_at: now(), label: { name: "triage/recommended" }, actor: { login: "fixture-maintainer", type: "User" } }];
    throw new Error("Unexpected route");
  } };
  const result = await governance.issue(1, trust);
  assert.equal(result.acceptance.state, "record-present");
  assert.ok(routes.every(route => !route.includes("timeline")));
  assert.ok(routes.some(route => route.includes("/events?per_page=100&page=1")));
});

test("removed and bot-reapplied disposition does not inherit old human label authority", () => {
  const stamp = now(), older = new Date(Date.now() - 10000).toISOString();
  const issue = { number: 1, state: "open", updated_at: stamp, labels: [{ name: "status/deferred" }] };
  const events = [
    { id: 1, event: "labeled", label: { name: "status/deferred" }, actor: { login: "fixture-maintainer", type: "User" }, created_at: older },
    { id: 2, event: "unlabeled", label: { name: "status/deferred" }, actor: { login: "fixture-maintainer", type: "User" }, created_at: stamp },
    { id: 3, event: "labeled", label: { name: "status/deferred" }, actor: { login: "fixture-adviser", type: "Bot" }, created_at: stamp },
  ];
  const r = reconcileIssue({ issue, comments: [], timeline: events, trust: trustFixture() });
  assert.equal(r.disposition, null); assert.equal(r.labelMismatch, true);
});

test("history preparation retries errors and incomplete histories even at unchanged item timestamp", async () => {
  const snapshot = fixtureSnapshot(), governance = new Governance({});
  governance.trust = async () => trustFixture();
  const reads = [];
  governance.issue = async number => { reads.push(number); return { ...snapshot.lifecycle.issues[number], status: "current", complete: true }; };
  snapshot.lifecycle.issues[1].status = "error";
  snapshot.lifecycle.issues[2].complete = false;
  const refreshed = await governance.prepare(snapshot);
  assert.deepEqual(reads, [1, 2]); assert.equal(refreshed.issues[1].status, "current");
  assert.equal(refreshed.issues[2].complete, true);
});

test("archived accepted items leave Roadmap but remain explicit history, and obsolete open metadata is reconciled", async t => {
  const store = await setup(t);
  store.snapshot.roadmap.nodes.find(n => n.content.number === 2).isArchived = true;
  const model = store.state();
  assert.ok(!projectView(model.items, { ...defaultView(), scope: "roadmap" }).visible.some(i => i.number === 2));
  assert.ok(projectView(model.items, { ...defaultView(), scope: "history" }).visible.some(i => i.number === 2));
  store.snapshot.issues.nodes[0].state = "CLOSED"; store.snapshot.issues.nodes[0].stateReason = "COMPLETED";
  store.snapshot.roadmap.nodes[0].content.state = "CLOSED";
  const result = await store.setView({ selectedId: target(1) });
  assert.notEqual(result.view.selectedId, target(1)); assert.match(result.viewNotice, /outside the active/);
  await assert.rejects(() => preview(store, "horizon", 2, { horizon: "Now" }), /nonarchived/);
});

test("withdrawal uses canonical exact strict record; changed permissions and private credentials fail closed", async t => {
  const store = await setup(t);
  const p = await preview(store, "scope-withdraw", 2, { reason: "Scope has changed.", area: "project" });
  assert.equal(authority.parseRecord(p.scopeRecord.body).Decision, "withdraw");
  assert.doesNotMatch(p.scopeRecord.body, /AI|footer/);
  store.snapshot.lifecycle.actor.canWrite = false;
  await assert.rejects(() => store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel"), /Stale|permission/);
  await assert.rejects(() => preview(store, "defer", 1, { reason: `Never retain ghp_${"x".repeat(30)}` }), /credentials/);
});

test("failed journal persistence blocks future actions and never sends", async t => {
  let sends = 0;
  const store = await setup(t, { send: async () => { sends++; } });
  const file = join(store.root, "not-a-directory"); await writeFile(file, "fixture"); store.root = file;
  await assert.rejects(() => preview(store), /could not be persisted/);
  assert.equal(store.bridge.state().available, false);
  await assert.rejects(() => preview(store, "smoke", null, {}), /blocked/i);
  assert.equal(sends, 0);
});

test("verified reconciliation resolves partial effects without erasing failure receipts or reposting", async t => {
  const store = await setup(t);
  const r = await claimed(store, "defer", 1, { reason: "Park fixture." });
  const failed = { ...receipt("metadata", "failed"), reference: "metadata-failure" };
  const partial = await store.bridge.report(result(r, { status: "partial", gate: gate(), receipts: [receipt("comment-readback"), failed] }));
  const done = await store.bridge.report(result(r, { revision: partial.revision, receipts: [receipt("metadata"),
    { ...receipt("reconciliation"), resolves: ["metadata-failure"] }] }));
  assert.equal(done.status, "completed");
  assert.equal(done.receipts.filter(r => r.outcome === "failed").length, 1);
});

test("PR-mapped run appears on linked issue but resume retains only original PR mandate", async t => {
  const store = await setup(t);
  const feed = { observedAt: now(), tool: "get_session", reference: "fixture-session", complete: false,
    runs: [{ id: "pr-driver", sessionId: "same-driver", targetIds: ["microsoft/apm/pr/20"], kind: "recovery", state: "waiting-human",
      name: "PR-scoped fixture driver", actor: "fixture-maintainer", mandate: "Only PR 20.", blocker: "Fixture review gate" }] };
  await store.bridge.reportRuns(feed);
  assert.equal(store.state().items.find(i => i.id === target(4)).lifecycle.runs[0].id, "pr-driver");
  const p = await preview(store, "resume", 4, { runId: "pr-driver", reason: "Continue original PR scope." });
  assert.deepEqual(p.runContext.targetIds, ["microsoft/apm/pr/20"]);
  assert.equal(p.runContext.sessionId, "same-driver");
  await assert.rejects(() => preview(store, "recover", "microsoft/apm/pr/20", { reason: "Duplicate" }), /Existing mapped/);
  await store.bridge.reportRuns({ ...feed, observedAt: now(), complete: true, runs: [] });
  assert.equal(store.bridge.runs[0].current, false);
  assert.equal(store.state().items.find(i => i.id === target(4)).lifecycle.runs.length, 0);
  await assert.rejects(() => store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel"), /Stale/);
});

test("run feed rejects duplicate sessions, future and backwards observations; partial feeds retain last-known mappings", async t => {
  const store = await setup(t);
  const run = { id: "run", sessionId: "session", targetIds: [target(2)], kind: "delivery", state: "running", name: "Fixture", actor: "fixture-maintainer", mandate: "Fixture only" };
  const feed = { observedAt: now(), tool: "get_sessions_status", reference: "fixture", complete: false, runs: [run] };
  await store.bridge.reportRuns(feed);
  await store.bridge.reportRuns({ ...feed, observedAt: now(), runs: [] });
  assert.equal(store.bridge.runs.length, 1);
  await assert.rejects(() => store.bridge.reportRuns({ ...feed, runs: [run, { ...run, id: "another" }] }), /duplicate/);
  await assert.rejects(() => store.bridge.reportRuns({ ...feed, observedAt: new Date(Date.now() + 60000).toISOString() }), /Invalid/);
  await assert.rejects(() => store.bridge.reportRuns({ ...feed, observedAt: new Date(Date.now() - 2000).toISOString() }), /Stale/);
});

test("capacity-queued request belongs in Delivery without fabricating an active agent or asking twice", async t => {
  const store = await setup(t);
  const r = await claimed(store, "implement", 2, { approvalUrl: store.snapshot.lifecycle.issues[2].acceptance.approvalUrl, reason: "Fixture bounded repair." });
  await assert.rejects(() => store.bridge.report(result(r, { status: "queued", gate: gate() })), /capacity observation/);
  const queued = await store.bridge.report(result(r, { status: "queued", gate: gate(), receipts: [receipt("capacity-observed")] }));
  const model = store.state(), item = model.items.find(i => i.id === target(2));
  assert.equal(queued.status, "queued"); assert.equal(item.lifecycle.stage, "Queued to start");
  assert.equal(item.lifecycle.delivery, true); assert.equal(item.queue, "authorized");
  assert.equal(item.execution.verified, false); assert.equal(item.lifecycle.runs.length, 0);
  assert.equal(item.nextAction, "View pending request");
  assert.match(item.primaryAction.status, /Wait for/);
  assert.ok(projectView(model.items, { ...defaultView(), scope: "delivery" }).visible.some(i => i.id === target(2)));
});

test("preview-only worker dispatch still requires actual host gate even without write permission", async t => {
  const store = await setup(t);
  store.snapshot.lifecycle.actor.canWrite = false;
  const r = await claimed(store, "triage", 1, { mode: "preview" });
  await assert.rejects(() => store.bridge.report(result(r, { status: "running", receipts: [receipt("session-observed")] })), /host confirmation/);
  const running = await store.bridge.report(result(r, { status: "running", gate: gate(), receipts: [receipt("session-observed")] }));
  assert.equal(running.status, "running");
  const contract = executionContract(r);
  assert.equal(contract.workflow.schedulerWrite, "off"); assert.equal(contract.workflow.workerWrite, "off");
});

test("missing transport acknowledgement times out as uncertain without permitting resend", async t => {
  let entered;
  const sending = new Promise(resolve => { entered = resolve; });
  const store = await setup(t, { send: () => { entered(); return new Promise(() => {}); } });
  const p = await preview(store);
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const confirmed = store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel");
  await sending; t.mock.timers.tick(15001);
  const r = await confirmed;
  assert.equal(r.status, "reconcile-required");
  assert.match(r.message, /ambiguous/);
  assert.equal((await store.bridge.confirm({ id: p.id, revision: p.revision, watermark: p.watermark }, "fixture-panel")).status, "reconcile-required");
});

test("plan-required is a real observed host boundary, not a label on an unobserved worker", async t => {
  const store = await setup(t);
  const r = await claimed(store, "recover", "microsoft/apm/pr/20", { reason: "Fixture plan first." });
  await assert.rejects(() => store.bridge.report(result(r, { status: "plan-required", gate: gate() })), /pending host plan/);
  const plan = await store.bridge.report(result(r, { status: "plan-required", gate: gate(), receipts: [receipt("plan-observed")] }));
  assert.equal(plan.status, "plan-required");
  await assert.rejects(() => store.bridge.report(result(r, { revision: plan.revision, status: "running", receipts: [receipt("session-observed")] })), /plan approval/);
});
