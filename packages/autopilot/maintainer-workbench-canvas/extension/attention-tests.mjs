import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { fixtureSnapshot, fixtureStore, now } from "./fixtures.mjs";
import { buildModel, makeBrief } from "./model.mjs";
import { itemId, STORE } from "./config.mjs";
import { attentionCounts, projectView, defaultView, applyAttentionView, restoreView, VIEW_SCHEMA } from "./scope.mjs";
import { renderDecisionHtml, escape } from "./decision-view.mjs";

const helpers = { link: (label, value) => `<a href="${escape(value)}">${escape(label)}</a>`, stateLabel: () => "Open",
  evidence: () => "", people: () => "", date: value => value || "Not observed" };
const view = scope => ({ ...defaultView(), scope });
const get = (model, kind, number) => model.items.find(item => item.id === itemId(kind, number));
function gated(snapshot, number = 20, conclusion = "ACTION_REQUIRED") {
  const raw = snapshot.prs.nodes.find(pr => pr.number === number);
  snapshot.pulls[number] = { status: "current", observedAt: now(), data: { ...structuredClone(raw), evidence: {
    head: raw.headRefOid, observedAt: now(), checks: { complete: true, nodes: [{ name: "Checks", status: "COMPLETED", conclusion }] },
    runs: { complete: true, observedAt: now(), nodes: [] },
  } } };
}
function renderState(snapshot, scope, kind, number) {
  const model = buildModel(snapshot), selectedId = itemId(kind, number);
  applyAttentionView(model, { scope });
  return { ...model, view: { ...view(scope), selectedId }, workflowApprovals: { available: true, requests: [] }, explanation: { available: false },
    brief: makeBrief(get(model, kind, number), model, snapshot) };
}

test("attention counts scope issues and unique PRs, including unlinked and many-to-many relationships", () => {
  const snapshot = fixtureSnapshot();
  snapshot.prs.nodes[0].body = "Partial work for #4 and #1.";
  snapshot.prs.nodes.push({ ...structuredClone(snapshot.prs.nodes[0]), number: 22, url: "https://github.com/microsoft/apm/pull/22", body: "Refs #4." });
  for (const number of [20, 21, 22]) gated(snapshot, number);
  const model = buildModel(snapshot), counts = attentionCounts(model.items);
  assert.equal(counts.decisions, 2);
  assert.equal(counts.permissions, 3);
  assert.equal(counts.reviews, 3);
  assert.equal(get(model, "pr", 21).related.length, 0);
  assert.equal(get(model, "pr", 20).related.length, 2);
  assert.equal(get(model, "issue", 4).related.length, 2);
  assert.match(get(model, "pr", 20).linkedState, /Issue #4.*partial/);
  assert.equal(projectView(model.items, view("permissions")).kind, "pull requests");
  assert.match(projectView(model.items, view("reviews")).facetBasis, /do not add/);
  assert.equal(new Set([...projectView(model.items, view("permissions")).visible, ...projectView(model.items, view("reviews")).visible].map(i => i.id)).size, 3);
});

test("scope recommendations exclude linked CI permissions and mapped human run gates", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  snapshot.runFeed = { runs: [{ id: "fixture-run", name: "Existing session", kind: "implement", state: "plan-required",
    actor: "fixture-maintainer", current: true, observedAt: now(), targetIds: [itemId("issue", 2)], blocker: "Approve the recorded plan in its conversation." }] };
  const model = buildModel(snapshot);
  const decisions = projectView(model.items, view("decisions")).visible.map(i => i.number);
  assert.deepEqual(decisions.sort(), [1, 3]);
  const run = get(model, "issue", 2);
  assert.equal(run.lifecycle.humanAttention, true);
  assert.equal(run.lifecycle.scopeDecision, false);
  assert.match(run.lifecycle.humanReason, /recorded plan/);
  assert.ok(projectView(model.items, view("delivery")).visible.includes(run));
});

test("scope contextual primary does not become permission for a linked PR", () => {
  const snapshot = fixtureSnapshot();
  snapshot.prs.nodes[0].body = "Refs #1";
  gated(snapshot);
  const model = buildModel(snapshot), issue = get(model, "issue", 1);
  assert.equal(issue.primaryAction.type, "approve-workflows");
  applyAttentionView(model, view("decisions"));
  assert.equal(issue.primaryAction.kind, "scope-draft");
  snapshot.lifecycle.issues[1].observedAt = "2000-01-01T00:00:00Z";
  const stale = buildModel(snapshot);
  applyAttentionView(stale, view("decisions"));
  assert.equal(get(stale, "issue", 1).primaryAction.type, "refresh");
});

for (const outcome of ["ACTION_REQUIRED", "FAILURE", "SUCCESS", "PENDING"]) {
  test(`PR review membership and diff primary are independent of ${outcome}`, () => {
    const snapshot = fixtureSnapshot();
    gated(snapshot, 20, outcome);
    if (outcome === "PENDING") snapshot.pulls[20].data.evidence.checks.nodes[0].status = "IN_PROGRESS";
    const state = renderState(snapshot, "reviews", "pr", 20);
    assert.equal(get(state, "pr", 20).attention.review, true);
    assert.equal(get(state, "pr", 20).primaryAction.type, "navigate");
    assert.equal(new URL(get(state, "pr", 20).primaryAction.url).pathname, "/microsoft/apm/pull/20/files");
    const html = renderDecisionHtml(state, helpers);
    assert.equal((html.match(/data-primary="true"/g) || []).length, 1);
    assert.match(html, /Related issues/);
    assert.doesNotMatch(html, /for your review/);
  });
}

test("mixed failed checks do not obscure the contextual workflow permission preview", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  snapshot.pulls[20].data.evidence.checks.nodes.push({ name: "Tests", status: "COMPLETED", conclusion: "FAILURE" });
  const state = renderState(snapshot, "permissions", "pr", 20);
  assert.equal(get(state, "pr", 20).primaryAction.type, "approve-workflows");
  assert.match(get(state, "pr", 20).primaryAction.status, /Failed checks.*not diagnosed/);
  assert.match(get(state, "pr", 20).attention.permissionReason, /checks report/);
});

test("accepted receipts remove only exact covered permission runs, not a new head or new attempt", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  snapshot.pulls[20].data.evidence.runs.nodes = [1, 2].map(id => ({ id, name: `Workflow ${id}`, head_sha: "b".repeat(40),
    status: "completed", conclusion: "action_required", run_attempt: 1, workflow_id: id, event: "pull_request" }));
  const approval = { targetId: itemId("pr", 20), head: "b".repeat(40), status: "completed",
    runs: [{ id: 1, attempt: 1 }, { id: 2, attempt: 1 }], receipts: [{ runId: 1, outcome: "approved" }] };
  snapshot.workflowApprovals = [approval];
  let pr = get(buildModel(snapshot), "pr", 20);
  assert.equal(pr.attention.permission, true);
  assert.match(pr.attention.permissionReason, /1 observed workflow/);
  approval.receipts.push({ runId: 2, outcome: "approved" });
  assert.equal(get(buildModel(snapshot), "pr", 20).attention.permission, false);
  snapshot.pulls[20].data.evidence.runs.nodes[1].run_attempt = 2;
  assert.equal(get(buildModel(snapshot), "pr", 20).attention.permission, true);
  approval.status = "approving";
  assert.equal(get(buildModel(snapshot), "pr", 20).attention.permission, false);
  approval.head = "c".repeat(40);
  assert.equal(get(buildModel(snapshot), "pr", 20).attention.permission, true);
});

for (const [field, value, expected] of [["isDraft", true, "draft"], ["reviewDecision", "CHANGES_REQUESTED", "changes-requested"],
  ["reviewDecision", "APPROVED", "review-recorded"], ["author", { login: "fixture-maintainer" }, "own-contribution"]]) {
  test(`${expected} stays in follow-up without inflating PR reviews`, () => {
    const snapshot = fixtureSnapshot();
    snapshot.prs.nodes[0][field] = value;
    gated(snapshot);
    const model = buildModel(snapshot), pr = get(model, "pr", 20);
    assert.equal(pr.attention.review, false);
    assert.equal(pr.attention.reviewState, expected);
    assert.equal(pr.attention.permission, true);
    assert.ok(projectView(model.items, view("review-followup")).visible.includes(pr));
    assert.equal(attentionCounts(model.items).reviews, 1);
  });
}

test("personal review assignment needs a matching User, not team name or maintainer role", () => {
  const snapshot = fixtureSnapshot();
  snapshot.lifecycle.actor.responsible = false;
  snapshot.prs.nodes[0].reviewRequests.nodes = [{ requestedReviewer: { __typename: "Team", name: "fixture-maintainer" } }];
  let model = buildModel(snapshot);
  assert.equal(get(model, "pr", 20).attention.review, false);
  assert.equal(get(model, "pr", 20).attention.reviewState, "ownership-unknown");
  snapshot.prs.nodes[0].reviewRequests.nodes = [{ requestedReviewer: { __typename: "User", login: "FIXTURE-MAINTAINER" } }];
  model = buildModel(snapshot);
  assert.equal(get(model, "pr", 20).attention.review, true);
  assert.equal(get(model, "pr", 20).attention.requestedFromActor, true);
  snapshot.lifecycle.actor.responsible = true;
  snapshot.prs.nodes[0].reviewRequests.nodes = [];
  model = buildModel(snapshot);
  assert.equal(get(model, "pr", 20).attention.requestedFromActor, false);
  assert.match(get(model, "pr", 20).attention.reviewReason, /no personal reviewer assignment/);
});

test("unknown ownership never silently counts permissions or reviews; stale known reads are labelled", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  delete snapshot.lifecycle.actor;
  let model = buildModel(snapshot);
  assert.equal(attentionCounts(model.items).permissions, 0);
  assert.equal(attentionCounts(model.items).reviews, 0);
  assert.equal(projectView(model.items, view("review-followup")).visible.length, 2);
  snapshot.lifecycle.actor = { login: "fixture-maintainer", type: "User", responsible: true, canWrite: true, observedAt: "2000-01-01T00:00:00Z" };
  model = buildModel(snapshot);
  assert.equal(get(model, "pr", 20).attention.ownershipCurrent, false);
  assert.match(get(model, "pr", 20).attention.reviewReason, /Last-known/);
  snapshot.lifecycle.actor.canWrite = false;
  assert.equal(attentionCounts(buildModel(snapshot).items).permissions, 0);
});

test("PRs never borrow same-number issue acceptance or recommendations", () => {
  const snapshot = fixtureSnapshot();
  snapshot.prs.nodes[0].number = 1;
  const pr = get(buildModel(snapshot), "pr", 1);
  assert.equal(pr.lifecycle.record, null);
  assert.equal(pr.lifecycle.scopeDecision, false);
  assert.equal(pr.authority.verified, false);
});

test("old mixed decision selection is preserved in Delivery with an explicit migration notice", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  const restored = restoreView({ schema: VIEW_SCHEMA, data: { ...defaultView(), selectedId: itemId("issue", 4) } }, buildModel(snapshot).items);
  assert.equal(restored.view.selectedId, itemId("issue", 4));
  assert.equal(restored.view.scope, "delivery");
  assert.match(restored.notice, /Attention is now split/);
});

test("saved permission preview cannot replace the review primary merely by changing category", () => {
  const snapshot = fixtureSnapshot();
  gated(snapshot);
  const state = renderState(snapshot, "reviews", "pr", 20);
  state.workflowApprovals.requests = [{ id: "fixture-preview", status: "preview", targetId: itemId("pr", 20), head: "b".repeat(40),
    actor: "fixture-maintainer", prNumber: 20, runs: [], receipts: [], createdAt: now(), expiresAt: now() }];
  const html = renderDecisionHtml(state, helpers);
  assert.doesNotMatch(html, /data-confirm-workflows/);
  assert.match(html, /data-primary="true"[^>]*href="https:\/\/github.com\/microsoft\/apm\/pull\/20\/files"/);
});

test("Store brief and item primary agree on the selected review context without changing timers", async t => {
  await mkdir(join(STORE, "test-runs"), { recursive: true });
  const root = await mkdtemp(join(STORE, "test-runs", "attention-"));
  const store = fixtureStore(root);
  t.after(async () => { await store.writeQueue; await rm(root, { recursive: true, force: true }); });
  gated(store.snapshot);
  store.view = { ...view("reviews"), selectedId: itemId("pr", 20) };
  const before = store.autoRefreshDueAt(), state = store.state();
  assert.equal(get(state, "pr", 20).primaryAction.type, "navigate");
  assert.equal(state.brief.recommendation.whyNow, get(state, "pr", 20).primaryAction.status);
  assert.equal(state.brief.recommendation.title, get(state, "pr", 20).primaryAction.label);
  assert.equal(store.autoRefreshDueAt(), before);
});
