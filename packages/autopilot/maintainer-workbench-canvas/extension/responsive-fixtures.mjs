import { fixtureStore, now } from "./fixtures.mjs";
import { WorkflowApprovals } from "./workflow-approvals.mjs";

export const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
export const clone = value => structuredClone(value);
export function responsiveStore(root) {
  const store = fixtureStore(root), calls = { pulls: 0, approvals: [], contexts: 0 }, held = new Map();
  const original = store.github.selectedIssue;
  store.github.selectedIssue = async number => { if (held.has(number)) await held.get(number).promise; return original(number); };
  const repository = { id: 1, full_name: "microsoft/apm", permissions: { push: true } };
  const fork = { id: 2, full_name: "fixture-contributor/apm", fork: true, private: false };
  const raw = store.snapshot.prs.nodes[0];
  const pr = { number: 20, state: "open", merged: false, auto_merge: null,
    head: { sha: raw.headRefOid, ref: "fixture-fix", repo: fork }, base: { repo: repository } };
  const runs = [101, 102].map((id, i) => ({ id, workflow_id: i + 1, name: `[Fixture] Workflow ${i + 1}`,
    event: "pull_request", repository, head_repository: fork, head_sha: raw.headRefOid, head_branch: "fixture-fix",
    pull_requests: [], status: "completed", conclusion: "action_required", run_attempt: 1,
    created_at: "2026-09-25T10:00:00Z", updated_at: now(), html_url: `https://github.com/microsoft/apm/actions/runs/${id}` }));
  const data = { repository, actor: { id: 3, login: "fixture-maintainer", type: "User" }, pr, runs, matchingPulls: [pr] };
  const pull = () => ({ ...clone(raw), evidence: { head: raw.headRefOid, observedAt: now(),
    checks: { complete: true, nodes: [{ name: "CLA", status: "COMPLETED", conclusion: "SUCCESS" }] },
    runs: { complete: true, observedAt: now(), nodes: clone(runs) } } });
  store.snapshot.pulls[20] = { status: "current", observedAt: now(), data: pull() };
  store.github.pull = async number => { calls.pulls++; return number === 20 ? pull() : clone(store.snapshot.prs.nodes.find(p => p.number === number)); };
  const approvalGate = deferred();
  const api = {
    context: async () => { calls.contexts++; return clone(data); },
    run: async id => clone(runs.find(run => run.id === id)),
    approve: async id => {
      calls.approvals.push(id);
      await approvalGate.promise;
      Object.assign(runs.find(run => run.id === id), { status: "queued", conclusion: null, updated_at: now() });
      return { status: 201 };
    },
  };
  store.workflowApprovals = new WorkflowApprovals(store, { api });
  return { store, calls, held, data, approvalGate, api,
    setRunState(status, conclusion = null) { for (const run of runs) Object.assign(run, { status, conclusion, updated_at: now() }); },
  };
}
