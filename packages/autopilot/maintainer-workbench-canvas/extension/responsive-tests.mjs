import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, readFile } from "node:fs/promises";
import { join } from "node:path";
import { STORE, AUTO_REFRESH, itemId } from "./config.mjs";
import { deferred, clone, responsiveStore } from "./responsive-fixtures.mjs";
import { mergeObservations } from "./observations.mjs";
import { now } from "./fixtures.mjs";
import { Explanations, explanationPacket } from "./explanations.mjs";
import { renderDecisionHtml, escape } from "./decision-view.mjs";
import { explanationProgress, pollDelay, workflowSummary } from "./async-view.mjs";
import { startServer } from "./server.mjs";
import { evidenceFor } from "./model.mjs";
import { projectView } from "./scope.mjs";
import { WorkflowApprovals } from "./workflow-approvals.mjs";

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function setup(t) {
  await mkdir(join(STORE, "test-runs"), { recursive: true });
  const root = await mkdtemp(join(STORE, "test-runs", "responsive-"));
  const f = responsiveStore(root);
  t.after(async () => {
    for (const gate of f.held.values()) gate.resolve();
    f.approvalGate.resolve();
    await Promise.all([...f.store.selectedTasks.values(), ...f.store.checkTasks.values(), ...f.store.workflowApprovals.tasks.values()]);
    await f.store.pending; await f.store.publishQueue; await f.store.writeQueue;
    await rm(root, { recursive: true, force: true });
  });
  return { ...f, root };
}
const helpers = { link: (label, value) => `<a href="${escape(value)}">${escape(label)}</a>`,
  stateLabel: () => "Open", evidence: () => "", people: () => "", date: value => value || "Not observed" };
async function http(t, store) {
  const server = await startServer(store, { instanceId: "responsive-tests" });
  t.after(() => server.close());
  const html = await (await fetch(server.url)).text();
  const token = /name="canvas-token" content="([^"]+)"/.exec(html)[1];
  return { server, post: (path, data, override = token) => fetch(new URL(path, server.url), {
    method: "POST", headers: { "content-type": "application/json", "x-canvas-token": override }, body: JSON.stringify(data) }) };
}

test("displayed auto-update time exactly matches the refresh gate after a slow portfolio read", async t => {
  const clock = Date.parse("2026-09-25T13:40:00Z");
  t.mock.timers.enable({ apis: ["Date"], now: clock });
  const { store } = await setup(t);
  store.lastAttempt = clock - 100000;
  store.snapshot.sources.issues.observedAt = new Date(clock - 90000).toISOString();
  store.snapshot.sources.prs.observedAt = new Date(clock - 60000).toISOString();
  store.snapshot.sources.roadmap.observedAt = new Date(clock - 30000).toISOString();
  store.snapshot.lifecycle.observedAt = new Date(clock).toISOString();
  store.snapshot.fetchedAt = new Date(clock).toISOString();
  const due = clock - 90000 + AUTO_REFRESH;
  assert.equal(Date.parse(store.state().nextAutoRefreshAt), due);
  let refreshes = 0;
  store.refresh = async () => { refreshes++; };
  t.mock.timers.tick(due - clock - 1);
  store.ensureFresh(); assert.equal(refreshes, 0);
  t.mock.timers.tick(1);
  store.ensureFresh(); assert.equal(refreshes, 1);
  store.lastAttempt = Date.now();
  delete store.snapshot.sources.issues.observedAt;
  assert.equal(Date.parse(store.state().nextAutoRefreshAt), Date.now() + AUTO_REFRESH);
  store.ensureFresh(); assert.equal(refreshes, 1);
});

test("held portfolio does not block immediate selection ack, cached title or selected progress", async t => {
  const { store, held } = await setup(t), portfolio = deferred(), read = deferred();
  const old = clone(store.snapshot);
  store.github.portfolio = async () => { await portfolio.promise; return old; };
  const refreshing = store.refresh();
  held.set(1, read);
  const ack = await store.select(itemId("issue", 1));
  assert.equal(ack.refreshing, true);
  assert.equal(ack.view.selectedId, itemId("issue", 1));
  assert.equal(ack.selectedRead.status, "reading");
  assert.match(renderDecisionHtml(ack, helpers), /Preserve nested skill links/);
  assert.match(renderDecisionHtml(ack, helpers), /Updating this item from GitHub/);
  read.resolve(); await store.waitSelected(itemId("issue", 1));
  assert.equal(store.state().refreshing, true);
  portfolio.resolve(); await refreshing;
  assert.equal(store.snapshot.details[1].status, "current");
});

test("rapid A/B selections stay responsive; late A never steals B or current filter selection", async t => {
  const { store, held } = await setup(t);
  held.set(1, deferred()); held.set(3, deferred());
  await store.select(itemId("issue", 1));
  await store.select(itemId("issue", 3));
  assert.equal(store.selectedActive, 2);
  assert.equal(store.state().view.selectedId, itemId("issue", 3));
  held.get(1).resolve(); await store.waitSelected(itemId("issue", 1));
  assert.equal(store.state().view.selectedId, itemId("issue", 3));
  await store.setView({ scope: "roadmap", selectedId: itemId("issue", 2) });
  held.get(3).resolve(); await store.waitSelected(itemId("issue", 3));
  assert.equal(store.state().view.selectedId, itemId("issue", 2));
  assert.equal(store.state().view.scope, "roadmap");
});

test("selected queue keeps at most two running reads and latest queued intent", async t => {
  const { store, held } = await setup(t);
  await store.setView({ scope: "roadmap-open" });
  held.set(1, deferred()); held.set(2, deferred());
  await store.select(itemId("issue", 1)); await store.select(itemId("issue", 2));
  await store.select(itemId("issue", 3)); await store.select(itemId("issue", 4));
  assert.equal(store.selectedActive, 2);
  assert.deepEqual(store.selectedQueue.map(x => x.id), [itemId("issue", 4)]);
  held.get(1).resolve(); held.get(2).resolve();
  await Promise.all([...store.selectedTasks.values()]);
  assert.equal(store.state().view.selectedId, itemId("issue", 4));
  assert.equal(store.snapshot.details[3], undefined);
});

test("serialized publication keeps newest source revision, independent timestamps and canonical record", async t => {
  const { store } = await setup(t), old = clone(store.snapshot), data = clone(store.snapshot.issues.nodes[0]);
  const newer = { ...data, updatedAt: "2030-01-01T00:00:00Z", title: "Newer source title" };
  const record = { ...old.lifecycle.issues[1], updatedAt: newer.updatedAt, observedAt: "2030-01-01T00:00:01Z", watermark: "newer-human-source" };
  await Promise.all([
    store.publish({ details: { 1: { data: newer, status: "current", observedAt: record.observedAt } },
      lifecycle: { ...old.lifecycle, issues: { 1: record } } }),
    store.publish(old, true),
  ]);
  assert.equal(store.snapshot.details[1].data.title, newer.title);
  assert.equal(store.snapshot.lifecycle.issues[1].watermark, "newer-human-source");
  assert.equal(store.snapshot.fetchedAt, old.fetchedAt);
  const onDisk = JSON.parse(await readFile(join(store.directory, "snapshot.json"), "utf8")).data;
  assert.deepEqual(onDisk, store.snapshot);
});

test("newer source wins over later observation of an older revision; ties choose later read", () => {
  const r = (changed, observed, title) => ({ data: { updatedAt: changed, title }, observedAt: observed, status: "current" });
  const a = r("2026-09-25T10:00:00Z", "2026-09-25T10:01:00Z", "new source");
  const b = r("2026-09-25T09:00:00Z", "2026-09-25T10:02:00Z", "old source");
  assert.equal(mergeObservations({ details: { 1: a } }, { details: { 1: b } }).details[1], a);
  const c = r(a.data.updatedAt, b.observedAt, "fresh tied source");
  assert.equal(mergeObservations({ details: { 1: a } }, { details: { 1: c } }).details[1], c);
});

test("independent check and run observation times survive a later metadata response", async t => {
  const { store } = await setup(t), a = clone(store.snapshot.pulls[20]), b = clone(a);
  a.data.evidence.observedAt = "2026-09-25T12:00:00Z";
  a.data.evidence.runs.observedAt = "2026-09-25T12:01:00Z";
  a.data.evidence.runs.nodes[0].status = "in_progress"; a.data.evidence.runs.nodes[0].conclusion = null;
  b.observedAt = "2026-09-25T12:03:00Z";
  b.data.evidence.observedAt = "2026-09-25T12:02:00Z";
  b.data.evidence.runs.observedAt = "2026-09-25T11:59:00Z";
  const merged = mergeObservations({ pulls: { 20: a } }, { pulls: { 20: b } }).pulls[20];
  assert.equal(merged.data.evidence.observedAt, b.data.evidence.observedAt);
  assert.equal(merged.data.evidence.runs.observedAt, a.data.evidence.runs.observedAt);
  assert.equal(merged.data.evidence.runs.nodes[0].status, "in_progress");
  b.data.headRefOid = b.data.evidence.head = "c".repeat(40);
  b.data.updatedAt = "2030-01-01T00:00:00Z";
  const changed = mergeObservations({ pulls: { 20: a } }, { pulls: { 20: b } }).pulls[20];
  assert.equal(changed.data.evidence.runs.nodes[0].status, "completed", "old-head evidence is never borrowed");
});

test("fresh checks do not make an independently stale workflow permission observation current", async t => {
  const { store } = await setup(t);
  store.snapshot.pulls[20].data.evidence.runs.observedAt = "2000-01-01T00:00:00Z";
  const item = store.state().items.find(i => i.id === itemId("issue", 4));
  assert.equal(item.primaryAction.type, "refresh");
});

test("timed out selected read exposes retry and cannot publish a late result", async t => {
  const { store, held } = await setup(t), id = itemId("issue", 1);
  store.timeouts.selected = 10; held.set(1, deferred());
  await store.select(id); await sleep(25);
  assert.equal(store.state().selectedRead.status, "error");
  assert.match(renderDecisionHtml(store.state(), helpers), /Retry this item/);
  held.get(1).resolve(); await store.waitSelected(id);
  assert.equal(store.snapshot.details[1], undefined);
  assert.equal(store.state().selectedRead.status, "error");
});

test("portfolio timeout retains coherent data, stops spinner and rejects late publication", async t => {
  const { store } = await setup(t), gate = deferred(), initial = clone(store.snapshot);
  store.timeouts.portfolio = 10;
  store.github.portfolio = async () => { await gate.promise; return { ...initial, fetchedAt: "2030-01-01T00:00:00Z" }; };
  await store.refresh();
  assert.equal(store.state().refreshing, false); assert.match(store.state().error, /timed out/);
  gate.resolve(); await sleep(10); await store.publishQueue;
  assert.deepEqual(store.snapshot, initial);
});

test("HTTP selection ACK precedes held discussion and ordered client intent rejects older selection/filter", async t => {
  const { store, held } = await setup(t), { post } = await http(t, store);
  held.set(1, deferred());
  const first = await post("/api/select", { id: itemId("issue", 1), intent: { client: "test-client", sequence: 1 } });
  assert.equal(first.status, 202); assert.equal((await first.json()).detailLoading, true);
  await post("/api/select", { id: itemId("issue", 3), intent: { client: "test-client", sequence: 3 } });
  await post("/api/select", { id: itemId("issue", 1), intent: { client: "test-client", sequence: 2 } });
  await post("/api/view", { scope: "history", intent: { client: "test-client", sequence: 2 } });
  assert.equal(store.state().view.selectedId, itemId("issue", 3));
  assert.equal((await post("/api/view", { intent: { client: "test-client", sequence: -1 } })).status, 400);
});

test("selected checks coalesce, obey ten-second budget and do not relabel portfolio time", async t => {
  const { store, calls } = await setup(t);
  await store.setView({ scope: "delivery", selectedId: itemId("issue", 4) });
  const before = store.snapshot.fetchedAt;
  store.ensureSelectedChecks(); store.ensureSelectedChecks();
  await Promise.all([...store.checkTasks.values()]);
  store.ensureSelectedChecks({ force: true });
  assert.equal(calls.pulls, 1);
  assert.equal(store.snapshot.fetchedAt, before);
  assert.equal(store.state().checksRead.status, "completed");
  assert.equal(pollDelay(store.state()), 2000);
});

test("completed checks stop fast GitHub reads and failures remain visible", async t => {
  const { store, calls, setRunState } = await setup(t);
  await store.setView({ scope: "delivery", selectedId: itemId("issue", 4) });
  setRunState("completed", "success");
  store.ensureSelectedChecks({ force: true }); await Promise.all([...store.checkTasks.values()]);
  store.checkReads.clear(); store.ensureSelectedChecks();
  assert.equal(calls.pulls, 1);
  assert.equal(pollDelay(store.state()), 10000);
  store.github.pull = async () => { throw new Error("Fixture network unavailable"); };
  store.ensureSelectedChecks({ force: true }); await Promise.all([...store.checkTasks.values()]);
  assert.match(store.state().checksRead.error, /Fixture network unavailable/);
  assert.match(renderDecisionHtml(store.state(), helpers), /Retry selected checks/);
});

test("run dedupe chooses latest attempt, then distinct newer run, without double counting", async t => {
  const { store } = await setup(t), record = store.snapshot.pulls[20], old = clone(record.data.evidence.runs.nodes[0]);
  record.data.evidence.runs.nodes = [old, { ...old, run_attempt: 2, status: "in_progress", conclusion: null }, old];
  let e = evidenceFor(record.data, record);
  assert.equal(e.runs.length, 1); assert.equal(e.runs[0].attempt, 2); assert.equal(e.runs[0].status, "in_progress");
  record.data.evidence.runs.nodes.push({ ...old, id: 1001, created_at: "2026-09-25T11:00:00Z", run_attempt: 1, conclusion: "success" });
  e = evidenceFor(record.data, record);
  assert.equal(e.runs[0].id, 1001); assert.equal(e.runs[0].attempt, 1);
});

test("every async explanation phase is visible, truthful and escaped without a technical disclosure", () => {
  for (const status of ["sending", "awaiting-agent", "generating", "failed", "uncertain", "cancelled"]) {
    const html = explanationProgress({ status, available: true, createdAt: now() }, status === "failed" ? "<failure>" : null);
    assert.doesNotMatch(html, /<details|percent|ETA/);
    assert.ok(html.includes("<strong>"));
    if (["sending", "awaiting-agent", "generating"].includes(status)) assert.match(html, /data-elapsed|data-slow/);
    if (status === "failed") assert.match(html, /&lt;failure&gt;/);
    if (status === "awaiting-agent") assert.match(html, /Waiting for background agent/);
  }
});

test("CI transitions preserve in-flight narrative claim and completed prose while live CTA changes", async t => {
  const { store, setRunState } = await setup(t);
  await store.setView({ scope: "delivery", selectedId: itemId("issue", 4) });
  store.explanations = new Explanations(store, { send: async () => "fixture-message" });
  const original = store.state().explanation, p = await store.explanations.request({ targetId: original.targetId, contentKey: original.contentKey }, "fixture");
  const claim = await store.explanations.claim({ id: p.id, revision: p.revision });
  for (const [status, conclusion] of [["queued", null], ["in_progress", null], ["completed", "success"]]) {
    setRunState(status, conclusion);
    store.checkReads.clear(); store.ensureSelectedChecks({ force: true }); await Promise.all([...store.checkTasks.values()]);
    assert.equal(explanationPacket(store, original.targetId).contentKey, original.contentKey);
    const pr = store.state().items.find(item => item.id === itemId("pr", 20));
    assert.match(workflowSummary(pr, helpers.link, helpers.date), new RegExp(status === "queued" ? "Queued" : status === "in_progress" ? "Running" : "Completed: success"));
  }
  await store.explanations.report({ id: claim.id, revision: claim.revision, claimToken: claim.claimToken, contentKey: claim.contentKey, status: "completed",
    result: { title: "Fixture narrative", problem: "Fixture problem.", example: "Illustrative fixture.", exampleKind: "illustrative",
      impact: "Fixture impact.", citations: [claim.packet.selected.url] } });
  assert.equal(store.state().explanation.status, "completed");
  assert.equal(store.state().items.find(i => i.id === original.targetId).primaryAction.label, "Open PR #20 for your review");
  assert.equal(store.explanations.requests.length, 1);
  store.snapshot.pulls[20].data.headRefOid = "c".repeat(40);
  assert.notEqual(explanationPacket(store, original.targetId).contentKey, original.contentKey);
});

test("strict guarded HTTP approval roundtrip previews, acknowledges and observes only mocked effects", async t => {
  const { store, calls, approvalGate, setRunState } = await setup(t), { post } = await http(t, store);
  await store.setView({ scope: "delivery", selectedId: itemId("issue", 4) });
  assert.equal((await post("/api/workflow-approvals/preview", { targetId: itemId("pr", 20) }, "wrong")).status, 403);
  assert.equal((await post("/api/workflow-approvals/preview", { targetId: itemId("pr", 20), runs: [999] })).status, 400);
  const preview = await (await post("/api/workflow-approvals/preview", { targetId: itemId("pr", 20) })).json();
  assert.equal(calls.approvals.length, 0);
  let html = renderDecisionHtml({ ...store.state(), approvalUi: { targetId: preview.targetId, record: preview } }, helpers);
  assert.match(html, /fixture-maintainer/); assert.match(html, /runs contributor code/);
  assert.match(html, /Approve 2 workflows for PR #20/); assert.equal((html.match(/data-primary="true"/g) || []).length, 1);
  assert.match(html, /data-confirm-workflows="[^"]+" disabled/);
  const result = await post("/api/workflow-approvals/confirm", { id: preview.id, revision: preview.revision, confirmed: true });
  assert.equal(result.status, 202); assert.equal((await result.json()).status, "approving");
  approvalGate.resolve(); await store.workflowApprovals.tasks.get(preview.id);
  assert.deepEqual(calls.approvals, [101, 102]);
  assert.equal(store.workflowApprovals.get(preview.id).status, "completed");
  setRunState("in_progress");
  store.workflowApprovals.lastObserved.clear();
  const observed = await (await post("/api/workflow-approvals/observe", { id: preview.id })).json();
  assert.ok(observed.observedRuns.every(r => r.status === "in_progress"));
  html = renderDecisionHtml(store.state(), helpers);
  assert.match(html, /Permission approval is not a passing CI result/); assert.match(html, /in_progress/);
  assert.deepEqual(calls.approvals, [101, 102]);
});

test("client timer stops hidden work, keeps elapsed updates local and never blocks the whole detail panel", async () => {
  const code = await readFile(new URL("./app.js", import.meta.url), "utf8");
  assert.match(code, /if \(!document.hidden\) pollTimer = setTimeout/);
  assert.match(code, /if \(document.hidden\) pollController\?\.abort/);
  assert.match(code, /setInterval\(updateClocks, 1000\)/);
  const clocks = code.slice(code.indexOf("function updateClocks()"), code.indexOf("function schedulePoll"));
  assert.doesNotMatch(clocks, /render\(|request\(|innerHTML/);
  assert.doesNotMatch(code, /aria-busy|if \(selectedLoading\) return/);
});

test("approved PR leaving permissions keeps exact receipts outside permission counts; explicit navigation ends follow", async t => {
  const { store, approvalGate, setRunState, root, api } = await setup(t), { post } = await http(t, store);
  await store.setView({ scope: "permissions", selectedId: itemId("pr", 20) });
  const p = await (await post("/api/workflow-approvals/preview", { targetId: itemId("pr", 20) })).json();
  await post("/api/workflow-approvals/confirm", { id: p.id, revision: p.revision, confirmed: true });
  approvalGate.resolve(); await store.workflowApprovals.tasks.get(p.id);
  setRunState("queued");
  store.ensureSelectedChecks({ force: true }); await Promise.all([...store.checkTasks.values()]);
  let state = store.state();
  assert.equal(state.view.selectedId, itemId("pr", 20));
  assert.equal(state.followingApproval.requestId, p.id); assert.equal(state.followingApproval.outsideQueue, true);
  assert.ok(!state.workIds.includes(itemId("pr", 20)));
  assert.equal(state.projection.filteredTotal, projectView(state.items, state.view).visible.length);
  assert.match(renderDecisionHtml(state, helpers), /Permission approval is not a passing CI result/);
  assert.deepEqual(state.workflowApprovals.requests[0].receipts.map(r => r.runId), [101, 102]);
  const restored = responsiveStore(root).store;
  restored.workflowApprovals = new WorkflowApprovals(restored, { api }); await restored.workflowApprovals.load(); await restored.load();
  assert.equal(restored.state().view.selectedId, itemId("pr", 20));
  assert.equal(restored.state().followingApproval.requestId, p.id);
  store.snapshot.pulls[20].observedAt = "2000-01-01T00:00:00Z";
  store.snapshot.pulls[20].data.evidence.observedAt = "2000-01-01T00:00:00Z";
  assert.equal(store.state().items.find(i => i.id === itemId("pr", 20)).primaryAction.type, "refresh");
  await store.setView({ search: "" });
  state = store.state();
  assert.equal(state.followingApproval, null);
  assert.notEqual(state.view.selectedId, itemId("pr", 20));
});

test("a late confirmation acknowledgement cannot re-enable follow after explicit view intent", async t => {
  const { store } = await setup(t);
  const oldRevision = store.viewRevision, selected = store.view.selectedId;
  await store.setView({ search: "" });
  await store.followApproval({ id: "7b402c8d-32ed-4449-ae99-f5b071ba96f9" }, selected, oldRevision);
  assert.equal(store.view.followingApprovalId, null);
});
