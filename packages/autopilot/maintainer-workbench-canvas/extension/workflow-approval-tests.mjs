import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { WorkflowApprovals } from "./workflow-approvals.mjs";
import { WorkflowApprovalApi, ApprovalApiError } from "./workflow-approval-api.mjs";
import { STORE, REPO } from "./config.mjs";

const HEAD = "a".repeat(40);
const target = `${REPO}/pr/20`;
const issue = `${REPO}/issue/4`;
const stamp = () => new Date().toISOString();
const clone = value => structuredClone(value);
function fixture() {
  const repo = { id: 1, full_name: REPO, permissions: { push: true } };
  const fork = { id: 2, full_name: "contributor/apm", fork: true, private: false, owner: { login: "contributor" } };
  const pr = { number: 20, state: "open", merged: false, auto_merge: null,
    head: { sha: HEAD, ref: "fix", repo: fork }, base: { repo } };
  const runs = [101, 102].map((id, index) => ({
    id, workflow_id: index + 1, name: `Workflow ${index + 1}`, event: "pull_request",
    repository: repo, head_repository: fork, head_sha: HEAD, head_branch: "fix",
    pull_requests: [], status: "completed", conclusion: "action_required", run_attempt: 1,
    created_at: "2026-09-25T10:00:00Z",
  }));
  return { repository: repo, pr, actor: { id: 3, login: "maintainer", type: "User" }, runs, matchingPulls: [pr] };
}
async function setup(t) {
  const base = join(STORE, "test-runs"); await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, "workflow-approval-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const data = fixture(), calls = [], model = {
    view: { selectedId: issue },
    items: [{ id: issue, kind: "issue", number: 4, related: [{ pr: 20 }] }, { id: target, kind: "pr", number: 20, related: [] }],
  };
  const store = { directory: root, state: () => clone(model),
    save: (name, value) => writeFile(join(root, name), JSON.stringify({ schema: 1, data: value })) };
  const api = {
    context: async () => clone(data),
    run: async id => clone(data.runs.find(run => run.id === id)),
    approve: async id => {
      calls.push(id);
      Object.assign(data.runs.find(run => run.id === id), { status: "queued", conclusion: null });
      return { status: 201 };
    },
  };
  const approvals = new WorkflowApprovals(store, { api });
  await approvals.load();
  return { approvals, api, data, calls, store, root, model };
}
const preview = approvals => approvals.preview({ targetId: target });
const confirm = (approvals, p, extra = {}) => approvals.confirm({ id: p.id, revision: p.revision, confirmed: true, ...extra });
async function finish(approvals, id) { await approvals.tasks.get(id); return approvals.get(id); }

test("preview is read-only and binds actor, exact head, workflows, attempts and short expiry", async t => {
  const { approvals, calls } = await setup(t);
  const p = await preview(approvals);
  assert.equal(p.status, "preview"); assert.equal(p.actor, "maintainer");
  assert.equal(p.head, HEAD); assert.equal(p.autoMerge, false);
  assert.deepEqual(p.runs.map(r => [r.id, r.attempt]), [[101, 1], [102, 1]]);
  assert.equal(p.proof, undefined); assert.deepEqual(calls, []);
  assert.equal(Date.parse(p.expiresAt) - Date.parse(p.createdAt), 120000);
  assert.equal(approvals.state(issue, { items: [{ id: issue, kind: "issue", related: [{ pr: 20 }] }] }).requests.length, 1);
});

test("same preview coalesces concurrent reads and duplicate completed previews", async t => {
  const { approvals, api } = await setup(t);
  const context = api.context; let reads = 0;
  api.context = async n => { reads++; return context(n); };
  const [a, b] = await Promise.all([preview(approvals), preview(approvals)]);
  assert.equal(a.id, b.id); assert.equal(reads, 1);
  assert.equal((await preview(approvals)).id, a.id); assert.equal(approvals.requests.length, 1);
});

test("confirmation is explicit; unrelated target and extra input cannot select arbitrary operations", async t => {
  const { approvals, calls } = await setup(t);
  await assert.rejects(() => approvals.preview({ targetId: `${REPO}/pr/99` }), /not the selected/);
  await assert.rejects(() => approvals.preview({ targetId: issue }), /not the selected/);
  await assert.rejects(() => approvals.preview({ targetId: target, runs: [99] }), /Invalid/);
  const p = await preview(approvals);
  await assert.rejects(() => confirm(approvals, p, { confirmed: false }), /Explicit/);
  await assert.rejects(() => confirm(approvals, p, { runs: [99] }), /Invalid/);
  assert.deepEqual(calls, []);
});

test("persisted acknowledgement precedes POST; immediate response is not running CI", async t => {
  const { approvals, api, calls, root } = await setup(t), p = await preview(approvals);
  const approve = api.approve;
  api.approve = async id => {
    const disk = JSON.parse(await readFile(join(root, "workflow-approvals.json"), "utf8")).data.requests[0];
    assert.equal(disk.status, "approving"); assert.equal(disk.currentRunId, id);
    assert.ok(disk.confirmedAt);
    return approve(id);
  };
  const accepted = await confirm(approvals, p);
  assert.equal(accepted.status, "approving"); assert.match(accepted.message, /Rechecking/);
  const result = await finish(approvals, p.id);
  assert.deepEqual(calls, [101, 102]); assert.equal(result.status, "completed");
  assert.deepEqual(result.receipts.map(r => r.outcome), ["approved", "approved"]);
  assert.equal(result.observedRuns[0].status, "queued");
  assert.match(result.message, /not a passing-CI claim/);
});

test("duplicate confirmations never repeat an approval", async t => {
  const { approvals, calls } = await setup(t), p = await preview(approvals);
  const results = await Promise.allSettled([confirm(approvals, p), confirm(approvals, p)]);
  assert.equal(results.filter(r => r.status === "fulfilled").length, 1);
  await finish(approvals, p.id);
  await assert.rejects(() => confirm(approvals, p), /Stale/);
  assert.deepEqual(calls, [101, 102]);
});

for (const [name, change] of [
  ["permission loss", x => { x.repository.permissions.push = false; }],
  ["actor change", x => { x.actor = { id: 4, login: "someone-else", type: "User" }; }],
  ["head change", x => { x.pr.head.sha = "b".repeat(40); }],
  ["auto-merge change", x => { x.pr.auto_merge = { merge_method: "squash" }; }],
  ["PR closure", x => { x.pr.state = "closed"; }],
  ["new workflow", x => { x.runs.push({ ...x.runs[0], id: 103, workflow_id: 3 }); }],
  ["new run attempt", x => { x.runs[0].run_attempt++; }],
]) {
  test(`confirmation fails closed after ${name}`, async t => {
    const { approvals, data, calls } = await setup(t), p = await preview(approvals);
    change(data); await confirm(approvals, p);
    assert.equal((await finish(approvals, p.id)).status, "blocked");
    assert.deepEqual(calls, []);
  });
}

test("expiry and changed selection reject before execution", async t => {
  const { approvals, model, calls } = await setup(t), p = await preview(approvals);
  model.view.selectedId = null;
  await assert.rejects(() => confirm(approvals, p), /not the selected/);
  model.view.selectedId = issue;
  approvals.record(p.id).expiresAt = "2000-01-01T00:00:00Z";
  await assert.rejects(() => confirm(approvals, p), /expired/);
  assert.deepEqual(calls, []);
});

for (const [name, change] of [
  ["unknown account", x => { x.actor.type = "Bot"; }],
  ["no write permission", x => { x.repository.permissions.push = false; }],
  ["private fork", x => { x.pr.head.repo.private = true; }],
  ["same repository", x => { x.pr.head.repo.id = x.repository.id; }],
  ["missing auto-merge state", x => { delete x.pr.auto_merge; }],
  ["ambiguous shared head", x => { x.matchingPulls.push({ ...clone(x.pr), number: 21 }); }],
  ["wrong run repository", x => { x.runs[0].repository = { id: 99, full_name: "foreign/repo" }; }],
  ["different explicit PR association", x => { x.runs[0].pull_requests = [{ number: 21, head: { sha: HEAD }, base: { repo: { id: 1 } } }]; }],
]) {
  test(`preview rejects ${name}`, async t => {
    const { approvals, data, calls } = await setup(t);
    change(data); await assert.rejects(() => preview(approvals)); assert.deepEqual(calls, []);
  });
}

test("empty fork association needs unique matching PR; explicit verified association works", async t => {
  const { approvals, data } = await setup(t);
  for (const run of data.runs) run.pull_requests = [{ number: 20, head: { sha: HEAD }, base: { repo: { id: 1 } } }];
  data.matchingPulls.push({ ...clone(data.pr), number: 21 });
  assert.equal((await preview(approvals)).runs.length, 2);
});

test("non-PR workflow and obsolete gated run cannot be approved", async t => {
  const { approvals, data } = await setup(t);
  data.runs[0].event = "pull_request_target";
  data.runs.push({ ...data.runs[1], id: 104, status: "queued", conclusion: null, created_at: "2026-09-25T11:00:00Z" });
  await assert.rejects(() => preview(approvals), /No unapproved/);
});

test("current attempt takes precedence when the same run ID is returned twice", async t => {
  const { approvals, data } = await setup(t);
  data.runs.push({ ...data.runs[0], run_attempt: 2, conclusion: "success" });
  assert.deepEqual((await preview(approvals)).runs.map(r => r.id), [102]);
});

test("human approval elsewhere skips POST and truthfully records running or completed", async t => {
  const { approvals, data, calls } = await setup(t), p = await preview(approvals);
  data.runs[0].status = "in_progress"; data.runs[0].conclusion = null;
  data.runs[1].conclusion = "failure";
  await confirm(approvals, p);
  const r = await finish(approvals, p.id);
  assert.deepEqual(calls, []);
  assert.deepEqual(r.receipts.map(r => r.outcome), ["already-started", "already-completed"]);
  assert.equal(r.observedRuns[1].conclusion, "failure");
});

test("API access denial stops after exact partial outcome; no retry of remaining runs", async t => {
  const { approvals, api, calls } = await setup(t), p = await preview(approvals);
  const approve = api.approve;
  api.approve = async id => {
    if (id === 102) throw new ApprovalApiError("GitHub approval failed (HTTP 403).", { status: 403, ambiguous: false });
    return approve(id);
  };
  await confirm(approvals, p);
  const r = await finish(approvals, p.id);
  assert.equal(r.status, "partial");
  assert.deepEqual(r.receipts.map(r => r.outcome), ["approved", "failed"]);
  assert.deepEqual(calls, [101]);
});

test("head changes between runs leave remaining approvals untouched", async t => {
  const { approvals, api, data, calls } = await setup(t), p = await preview(approvals);
  const approve = api.approve;
  api.approve = async id => { const result = await approve(id); data.pr.head.sha = "b".repeat(40); return result; };
  await confirm(approvals, p);
  assert.equal((await finish(approvals, p.id)).status, "partial");
  assert.deepEqual(calls, [101]);
});

test("new unconfirmed workflow between runs stops approval of remaining workflows", async t => {
  const { approvals, api, data, calls } = await setup(t), p = await preview(approvals);
  const approve = api.approve;
  api.approve = async id => { const result = await approve(id); data.runs.push({ ...data.runs[1], id: 105, workflow_id: 5 }); return result; };
  await confirm(approvals, p);
  assert.equal((await finish(approvals, p.id)).status, "partial");
  assert.deepEqual(calls, [101]);
});

test("unknown POST outcome blocks retry until independent observation resolves the gate", async t => {
  const { approvals, api, data, calls } = await setup(t), p = await preview(approvals);
  api.approve = async id => { calls.push(id); throw new ApprovalApiError("Timed out", { ambiguous: true }); };
  await confirm(approvals, p);
  const r = await finish(approvals, p.id);
  assert.equal(r.status, "uncertain"); assert.equal(r.receipts[0].outcome, "uncertain");
  await assert.rejects(() => preview(approvals), /uncertain/);
  for (const run of data.runs) { run.status = "queued"; run.conclusion = null; }
  const observed = await approvals.observe({ id: p.id });
  assert.equal(observed.status, "completed"); assert.match(observed.message, /not attributed as confirmed/);
  assert.deepEqual(calls, [101]);
});

test("accepted API response plus stale GitHub gate cannot cause duplicate approval", async t => {
  const { approvals, api, calls } = await setup(t), p = await preview(approvals);
  api.approve = async id => { calls.push(id); return { status: 201 }; };
  await confirm(approvals, p);
  assert.equal((await finish(approvals, p.id)).status, "completed");
  await assert.rejects(() => preview(approvals), /accepted approval may take time/);
  assert.deepEqual(calls, [101, 102]);
});

test("resolved ambiguous first run permits a new confirmation for only unattempted remaining runs", async t => {
  const { approvals, api, data, calls } = await setup(t), p = await preview(approvals);
  api.approve = async id => {
    calls.push(id);
    Object.assign(data.runs.find(run => run.id === id), { status: "queued", conclusion: null });
    throw new ApprovalApiError("Reply lost", { ambiguous: true });
  };
  await confirm(approvals, p); await finish(approvals, p.id);
  const observed = await approvals.observe({ id: p.id });
  assert.equal(observed.status, "partial"); assert.match(observed.message, /fresh preview and confirmation/);
  const remaining = await preview(approvals);
  assert.deepEqual(remaining.runs.map(run => run.id), [102]); assert.deepEqual(calls, [101]);
});

test("reload never replays in-flight actions or allows old previews to be confirmed", async t => {
  const { approvals, store, api, calls, root } = await setup(t), p = await preview(approvals);
  const replacement = new WorkflowApprovals(store, { api }); await replacement.load();
  assert.equal(replacement.get(p.id).status, "blocked");
  await assert.rejects(() => confirm(replacement, p), /Stale/);
  const disk = JSON.parse(await readFile(join(root, "workflow-approvals.json"), "utf8"));
  disk.data.requests[0].status = "approving";
  await writeFile(join(root, "workflow-approvals.json"), JSON.stringify(disk));
  const restarted = new WorkflowApprovals(store, { api }); await restarted.load();
  assert.equal(restarted.get(p.id).status, "uncertain"); assert.deepEqual(calls, []);
});

test("read-only observations coalesce, throttle and show queued then actual completion", async t => {
  const { approvals, api, data } = await setup(t), p = await preview(approvals);
  let reads = 0; const run = api.run;
  api.run = async id => { reads++; return run(id); };
  await Promise.all([approvals.observe({ id: p.id }), approvals.observe({ id: p.id })]);
  assert.equal(reads, 2);
  data.runs[0].status = "in_progress"; data.runs[0].conclusion = null;
  await approvals.observe({ id: p.id }); assert.equal(reads, 2);
  approvals.lastObserved.set(p.id, 0);
  assert.equal((await approvals.observe({ id: p.id })).observedRuns[0].status, "in_progress");
  data.runs[0].status = "completed"; data.runs[0].conclusion = "success";
  approvals.lastObserved.set(p.id, 0);
  assert.equal((await approvals.observe({ id: p.id })).observedRuns[0].conclusion, "success");
});

test("observation failure preserves explicit error, never a completed or passing fallback", async t => {
  const { approvals, api } = await setup(t), p = await preview(approvals);
  api.run = async () => { throw new Error("No network"); };
  const observed = await approvals.observe({ id: p.id });
  assert.equal(observed.status, "preview"); assert.match(observed.observationError, /No network/);
  assert.equal(observed.observedRuns, undefined);
});

test("persistent-write failure prevents POST and disables later approval", async t => {
  const { approvals, store, calls } = await setup(t), p = await preview(approvals);
  store.save = async () => { throw new Error("disk full"); };
  await assert.rejects(() => confirm(approvals, p), /could not be saved/);
  assert.match(approvals.error, /disabled/); assert.deepEqual(calls, []);
});

test("malformed history fails closed without execution", async t => {
  const { store, root, api, calls } = await setup(t);
  await writeFile(join(root, "workflow-approvals.json"), '{"schema":99,"data":{"requests":[]}}');
  const approvals = new WorkflowApprovals(store, { api }); await approvals.load();
  assert.equal(approvals.state().available, false);
  await assert.rejects(() => preview(approvals), /disabled/); assert.deepEqual(calls, []);
});

test("API wrapper has one fixed mutation endpoint, correct 201 empty receipt and strict numeric IDs", async () => {
  const calls = [];
  const api = new WorkflowApprovalApi(async (method, path) => { calls.push({ method, path }); return { status: 201, data: null }; });
  assert.deepEqual(await api.approve(101), { status: 201 });
  assert.deepEqual(calls, [{ method: "POST", path: `repos/${REPO}/actions/runs/101/approve` }]);
  await assert.rejects(() => api.approve("101/../../permissions"), /Invalid/);
  assert.equal(calls.length, 1);
  api.request = async () => ({ status: 202 });
  await assert.rejects(() => api.approve(102), error => error.ambiguous === true);
});

test("API pagination refuses incomplete or over-limit approval sources", async () => {
  const api = new WorkflowApprovalApi(async () => ({ status: 200, data: { workflow_runs: [], total_count: 1 } }));
  await assert.rejects(() => api.pages(`repos/${REPO}/actions/runs?head_sha=${HEAD}`, "workflow_runs"), /incomplete/);
  let reads = 0;
  api.request = async () => { reads++; return { status: 200, data: Array(100).fill({ number: 1 }) }; };
  await assert.rejects(() => api.pages(`repos/${REPO}/pulls`), /bounded/);
  assert.equal(reads, 10);
});
