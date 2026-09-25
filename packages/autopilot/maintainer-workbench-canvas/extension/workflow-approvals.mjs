import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { WorkflowApprovalApi } from "./workflow-approval-api.mjs";
import { REPO, parseId, pool, sanitize } from "./config.mjs";

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const SHA = /^[a-f0-9]{40}$/;
const STATES = new Set(["preview", "approving", "completed", "partial", "blocked", "failed", "uncertain"]);
const RUNNING = new Set(["queued", "in_progress", "requested", "pending", "waiting"]);
const TERMINAL = new Set(["success", "failure", "cancelled", "neutral", "skipped", "timed_out", "stale", "startup_failure"]);
const OUTCOMES = new Set(["approved", "already-started", "already-completed", "failed", "uncertain"]);
const PREVIEW_TTL = 2 * 60 * 1000;
const OBSERVE_INTERVAL = 10000;
const MAX_RUNS = 20;
const now = () => new Date().toISOString();
const integer = value => Number.isSafeInteger(value) && value > 0;
const runUrl = id => `https://github.com/${REPO}/actions/runs/${id}`;
const isBlocked = run => run.status === "completed" && run.conclusion === "action_required";
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const shape = (value, keys) => {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(k => !keys.includes(k))) {
    throw new Error("Invalid workflow approval input.");
  }
};
const shortRun = run => ({
  id: run.id, name: typeof run.name === "string" && run.name ? sanitize(run.name).slice(0, 200) : `Workflow ${run.workflow_id}`,
  url: runUrl(run.id), attempt: run.run_attempt,
});

function validateIdentity(context, number) {
  const { actor, repository: repo, pr } = context;
  if (!integer(actor?.id) || actor.type !== "User" || !/^[A-Za-z0-9-]+$/.test(actor.login || "")) {
    throw new Error("A signed-in human GitHub account could not be verified.");
  }
  if (repo?.full_name !== REPO || !integer(repo.id) || repo.permissions?.push !== true) {
    throw new Error("Repository write permission could not be verified. No workflows were approved.");
  }
  if (pr?.number !== number || pr.state !== "open" || pr.merged === true || pr.base?.repo?.id !== repo.id ||
      pr.base.repo.full_name !== REPO || !Object.hasOwn(pr, "auto_merge") || !SHA.test(pr.head?.sha || "") ||
      !integer(pr.head.repo?.id) || typeof pr.head.ref !== "string" || !pr.head.ref ||
      pr.head.repo.id === repo.id || pr.head.repo.fork !== true || pr.head.repo.private !== false) {
    throw new Error("A current open PR from a public fork could not be verified. Open the PR on GitHub.");
  }
  return {
    actor: actor.login, actorId: actor.id, repoId: repo.id, head: pr.head.sha,
    headRepoId: pr.head.repo.id, headRef: pr.head.ref, autoMerge: Boolean(pr.auto_merge),
  };
}

function validateRun(run, proof, context = null) {
  if (!integer(run?.id) || !integer(run.workflow_id) || !integer(run.run_attempt) ||
      run.repository?.id !== proof.repoId || run.repository.full_name !== REPO ||
      run.head_repository?.id !== proof.headRepoId || run.head_sha !== proof.head ||
      run.head_branch !== proof.headRef || run.event !== "pull_request") {
    throw new Error("Workflow repository, fork, branch or commit does not match the confirmed PR.");
  }
  if (!Array.isArray(run.pull_requests)) throw new Error("Workflow PR association is unavailable.");
  if (run.pull_requests.length) {
    if (!run.pull_requests.some(pr => pr.number === proof.prNumber && pr.head?.sha === proof.head && pr.base?.repo?.id === proof.repoId)) {
      throw new Error("Workflow belongs to another pull request.");
    }
  } else if (context) {
    // Fork runs commonly have an empty pull_requests array. Never guess between PRs sharing a head.
    const matches = context.matchingPulls?.filter(pr => pr.state === "open" && pr.head?.sha === proof.head &&
      pr.head.repo?.id === proof.headRepoId && pr.head.ref === proof.headRef && pr.base?.repo?.id === proof.repoId);
    if (matches?.length !== 1 || matches[0].number !== proof.prNumber) {
      throw new Error("The fork run cannot be uniquely associated with this PR. Open GitHub to approve it.");
    }
  } else if (!proof.uniqueHead) {
    throw new Error("Workflow PR association is not verified.");
  }
}

function newestBlocked(context, proof) {
  if (!Array.isArray(context.runs)) throw new Error("Workflow evidence is incomplete.");
  const latest = new Map();
  for (const run of context.runs) {
    if (run.event !== "pull_request" || run.head_sha !== proof.head || run.head_repository?.id !== proof.headRepoId ||
        run.head_branch !== proof.headRef) continue;
    validateRun(run, proof, context);
    if (!Number.isFinite(Date.parse(run.created_at))) throw new Error("Workflow creation time is unavailable.");
    const old = latest.get(run.workflow_id);
    const newer = old && (Date.parse(run.created_at) - Date.parse(old.created_at) || run.id - old.id || run.run_attempt - old.run_attempt);
    if (!old || newer > 0) {
      latest.set(run.workflow_id, run);
    }
  }
  return [...latest.values()].filter(isBlocked).sort((a, b) => a.id - b.id);
}

export class WorkflowApprovals {
  constructor(store, { api = new WorkflowApprovalApi() } = {}) {
    this.store = store;
    this.api = api;
    this.requests = [];
    this.error = null;
    this.queue = Promise.resolve();
    this.tasks = new Map();
    this.observations = new Map();
    this.lastObserved = new Map();
    this.previewing = new Map();
  }

  serial(fn) { const next = this.queue.then(fn); this.queue = next.catch(() => {}); return next; }
  async save() {
    try { await this.store.save("workflow-approvals.json", { version: 1, requests: this.requests }); }
    catch {
      this.error = "Workflow approval history could not be saved. Approval is disabled; inspect GitHub before retrying.";
      throw new Error(this.error);
    }
  }
  public(record) {
    const { proof, ...result } = record;
    return structuredClone(result);
  }
  record(id) {
    if (!UUID.test(id || "")) throw new Error("Invalid workflow approval ID.");
    const r = this.requests.find(r => r.id === id);
    if (!r) throw new Error("Unknown workflow approval request.");
    return r;
  }
  get(id) { return this.public(this.record(id)); }

  async load() {
    try {
      const saved = JSON.parse(await readFile(join(this.store.directory, "workflow-approvals.json"), "utf8"));
      if (saved.schema !== 1 || saved.data?.version !== 1 || !Array.isArray(saved.data.requests) || saved.data.requests.length > 100) {
        throw new Error("Invalid workflow approval history.");
      }
      for (const r of saved.data.requests) {
        if (!UUID.test(r.id || "") || !integer(r.revision) || !STATES.has(r.status) || !SHA.test(r.head || "") ||
            r.targetId !== `${REPO}/pr/${r.prNumber}` || !integer(r.prNumber) ||
            !Array.isArray(r.runs) || !r.runs.length || r.runs.length > MAX_RUNS ||
            new Set(r.runs.map(run => run.id)).size !== r.runs.length ||
            r.runs.some(run => !integer(run.id) || !integer(run.attempt) || run.url !== runUrl(run.id)) ||
            !Array.isArray(r.receipts) || r.receipts.some(receipt => !r.runs.some(run => run.id === receipt.runId) || !OUTCOMES.has(receipt.outcome)) ||
            r.currentRunId != null && !r.runs.some(run => run.id === r.currentRunId) ||
            !r.proof || r.proof.head !== r.head || r.proof.actor !== r.actor || r.proof.prNumber !== r.prNumber ||
            !integer(r.proof.actorId) || !integer(r.proof.repoId) || !integer(r.proof.headRepoId) ||
            typeof r.proof.headRef !== "string" || typeof r.proof.autoMerge !== "boolean") {
          throw new Error("Invalid workflow approval record.");
        }
        if (r.status === "approving") {
          r.status = "uncertain"; r.revision++; r.updatedAt = now();
          r.message = "The provider restarted during approval. Check GitHub status; nothing will be resent automatically.";
        } else if (r.status === "preview") {
          r.status = "blocked"; r.revision++; r.updatedAt = now();
          r.message = "This preview expired when the provider restarted. Prepare a fresh preview before approval.";
        }
      }
      this.requests = saved.data.requests;
      await this.save();
    } catch (error) {
      if (error.code !== "ENOENT") this.error = "Workflow approval history is unavailable or invalid. Direct approval is disabled.";
    }
  }

  state(targetId, model) {
    const selected = model?.items?.find(i => i.id === targetId);
    const targets = selected?.kind === "issue" ? selected.related.map(r => `${REPO}/pr/${r.pr}`) : [targetId];
    return { available: !this.error, error: this.error, pollSeconds: 10,
      requests: this.requests.filter(r => !targetId || targets.includes(r.targetId)).map(r => this.public(r)) };
  }
  selected(targetId) {
    const { kind, number } = parseId(targetId);
    const model = this.store.state(), selected = model.items.find(i => i.id === model.view.selectedId);
    if (kind !== "pr" || !selected || !(selected.id === targetId || selected.kind === "issue" && selected.related.some(r => r.pr === number))) {
      throw new Error("Approval target is not the selected PR or one of its linked PRs.");
    }
    return number;
  }
  alreadyApproved(run) {
    return this.requests.some(r => r.runs.some(saved => saved.id === run.id && saved.attempt === run.run_attempt) &&
      r.receipts.some(receipt => receipt.runId === run.id && receipt.outcome === "approved"));
  }
  pending(context, proof) { return newestBlocked(context, proof).filter(run => !this.alreadyApproved(run)); }

  async preview(input) {
    shape(input, ["targetId"]);
    if (this.error) throw new Error(this.error);
    const number = this.selected(input.targetId);
    if (this.previewing.has(input.targetId)) return this.previewing.get(input.targetId);
    const task = (async () => {
      const context = await this.api.context(number);
      const proof = { ...validateIdentity(context, number), prNumber: number, uniqueHead: true };
      const runs = this.pending(context, proof).map(shortRun);
      if (!runs.length) throw new Error("No unapproved fork workflows remain on this PR head. Update its GitHub status; an accepted approval may take time to appear.");
      if (runs.length > MAX_RUNS) throw new Error("More than 20 workflows need approval. Inspect and approve them on GitHub.");
      return this.serial(async () => {
        if (this.error) throw new Error(this.error);
        this.selected(input.targetId);
        const active = this.requests.find(r => r.targetId === input.targetId && ["approving", "uncertain"].includes(r.status));
        if (active) throw new Error("An approval is running or its outcome is uncertain. Check its status before preparing another.");
        const prior = [...this.requests].reverse().find(r => r.targetId === input.targetId && r.status === "preview" &&
          Date.parse(r.expiresAt) > Date.now() && same(r.proof, proof) && same(r.runs, runs));
        if (prior) return this.public(prior);
        const createdAt = now();
        const record = {
          id: randomUUID(), revision: 1, targetId: input.targetId, prNumber: number,
          head: proof.head, actor: proof.actor, autoMerge: proof.autoMerge, proof, runs,
          status: "preview", createdAt, updatedAt: createdAt, expiresAt: new Date(Date.parse(createdAt) + PREVIEW_TTL).toISOString(),
          receipts: [], message: `Review ${runs.length} workflows on this exact commit. Nothing has been approved.`,
        };
        // Keep every unsettled outcome rather than evicting an ambiguous request.
        if (this.requests.length >= 100) {
          const removable = this.requests.findIndex(r => !["approving", "uncertain"].includes(r.status) &&
            r.receipts.every(receipt => receipt.outcome !== "approved" || r.observedRuns?.some(run =>
              run.id === receipt.runId && run.conclusion !== "action_required")));
          if (removable < 0) throw new Error("Approval history is full of unresolved requests. Reconcile those before continuing.");
          this.requests.splice(removable, 1);
        }
        this.requests.push(record); await this.save(); return this.public(record);
      });
    })();
    this.previewing.set(input.targetId, task);
    try { return await task; } finally { this.previewing.delete(input.targetId); }
  }

  async confirm(input) {
    shape(input, ["id", "revision", "confirmed"]);
    if (input.confirmed !== true) throw new Error("Explicit acknowledgement is required to run contributor workflows.");
    let launch = false;
    const record = await this.serial(async () => {
      if (this.error) throw new Error(this.error);
      const r = this.record(input.id);
      if (r.status !== "preview" || r.revision !== input.revision) throw new Error("Stale or already submitted approval. Check its recorded status.");
      this.selected(r.targetId);
      if (Date.parse(r.expiresAt) <= Date.now()) throw new Error("Approval preview expired. Review a fresh workflow list.");
      if (this.requests.some(other => other !== r && (other.status === "approving" ||
          other.status === "uncertain" && other.targetId === r.targetId))) {
        throw new Error("Another approval is running, or this PR has an uncertain outcome. Check its status first.");
      }
      r.status = "approving"; r.confirmedAt = now(); r.updatedAt = now(); r.revision++;
      r.message = "Rechecking your account, PR commit and exact workflow list before approval.";
      await this.save(); launch = true; return r;
    });
    if (launch) {
      const task = this.execute(record).catch(async () => {
        record.status = "uncertain"; record.updatedAt = now();
        record.message = this.error || "Approval stopped unexpectedly. Inspect GitHub status; nothing will be resent.";
        if (!this.error) {
          try { await this.save(); }
          catch { /* save records the persistent failure and disables further approvals. */ }
        }
      }).finally(() => this.tasks.delete(record.id));
      this.tasks.set(record.id, task);
    }
    return this.public(record);
  }

  async update(r, patch) {
    return this.serial(async () => {
      Object.assign(r, patch, { revision: r.revision + 1, updatedAt: now() });
      await this.save();
    });
  }

  async execute(r) {
    let attempted = false;
    try {
      const context = await this.api.context(r.prNumber);
      const identity = { ...validateIdentity(context, r.prNumber), prNumber: r.prNumber, uniqueHead: true };
      if (!same(identity, r.proof)) throw new Error("Account, permission, PR commit or auto-merge state changed. Review a fresh preview.");
      const current = this.pending(context, r.proof);
      if (current.some(run => !r.runs.some(saved => saved.id === run.id && saved.attempt === run.run_attempt))) {
        throw new Error("The pending workflow list changed. Review a fresh preview; no new run is implicitly approved.");
      }
      for (const saved of r.runs) {
        if (this.error) throw new Error(this.error);
        const [fresh, run] = await Promise.all([this.api.context(r.prNumber), this.api.run(saved.id)]);
        const freshIdentity = { ...validateIdentity(fresh, r.prNumber), prNumber: r.prNumber, uniqueHead: true };
        if (!same(freshIdentity, r.proof)) throw new Error("PR, account or auto-merge changed during approval. Remaining runs were not approved.");
        validateRun(run, r.proof, fresh);
        if (run.id !== saved.id || run.run_attempt !== saved.attempt) throw new Error("Workflow run or attempt changed. Remaining runs were not approved.");
        if (this.pending(fresh, r.proof).some(run => !r.runs.some(saved => saved.id === run.id && saved.attempt === run.run_attempt))) {
          throw new Error("The workflow list changed during approval. Review the remaining runs again.");
        }
        let outcome, message;
        if (!isBlocked(run)) {
          if (RUNNING.has(run.status)) { outcome = "already-started"; message = `GitHub already reports ${run.status}; no approval was sent.`; }
          else if (run.status === "completed" && TERMINAL.has(run.conclusion)) {
            outcome = "already-completed"; message = `GitHub already reports ${run.conclusion}; no approval was sent.`;
          } else throw new Error("Workflow approval state is unclear. Remaining runs were not approved.");
        } else {
          await this.update(r, { currentRunId: saved.id, message: `Approving ${saved.name} (${r.receipts.length + 1} of ${r.runs.length}).` });
          attempted = true;
          try {
            await this.api.approve(saved.id);
            outcome = "approved"; message = "GitHub accepted workflow permission. Execution and results are observed separately.";
          } catch (error) {
            outcome = error.ambiguous !== false ? "uncertain" : "failed";
            message = error.message;
            r.receipts.push({ runId: saved.id, name: saved.name, url: saved.url, outcome, message, observedAt: now() });
            await this.update(r, { status: outcome === "uncertain" ? "uncertain" : r.receipts.some(x => x.outcome === "approved") ? "partial" : "failed",
              message: `${message} Unattempted workflows were left unchanged.`, currentRunId: null });
            return;
          }
        }
        r.receipts.push({ runId: saved.id, name: saved.name, url: saved.url, outcome, message, observedAt: now() });
        await this.update(r, { currentRunId: null });
      }
      await this.update(r, { status: "completed", currentRunId: null,
        message: "Workflow permission requests are complete. Waiting for GitHub's observed run status; this is not a passing-CI claim." });
      await this.observe({ id: r.id });
    } catch (error) {
      if (this.error) throw error;
      await this.update(r, { status: attempted ? "partial" : "blocked", currentRunId: null,
        message: `${error.message} No remaining approvals will be sent automatically.` });
    }
  }

  async observe(input) {
    shape(input, ["id"]);
    const r = this.record(input.id);
    if (this.error) throw new Error(this.error);
    if (this.observations.has(r.id)) return this.observations.get(r.id);
    if (Date.now() - (this.lastObserved.get(r.id) || 0) < OBSERVE_INTERVAL) return this.public(r);
    this.lastObserved.set(r.id, Date.now());
    const task = (async () => {
      try {
        const observedRuns = await pool(r.runs, async saved => {
          const run = await this.api.run(saved.id);
          validateRun(run, r.proof);
          if (run.id !== saved.id || run.run_attempt < saved.attempt) throw new Error("GitHub returned mismatched run evidence.");
          return { ...shortRun(run), status: run.status, conclusion: run.conclusion, observedAt: now() };
        });
        const patch = { observedRuns, observationError: null, observedAt: now() };
        const uncertainIds = new Set([
          ...r.receipts.filter(receipt => receipt.outcome === "uncertain").map(receipt => receipt.runId),
          ...(integer(r.currentRunId) ? [r.currentRunId] : []),
        ]);
        const resolved = run => run.conclusion !== "action_required" &&
          (RUNNING.has(run.status) || run.status === "completed" && TERMINAL.has(run.conclusion));
        const uncertainRuns = uncertainIds.size ? observedRuns.filter(run => uncertainIds.has(run.id)) : observedRuns;
        if (r.status === "uncertain" && uncertainRuns.length > 0 &&
            (!uncertainIds.size || uncertainRuns.length === uncertainIds.size) && uncertainRuns.every(resolved)) {
          const remaining = observedRuns.filter(run => !resolved(run)).length;
          patch.status = remaining ? "partial" : "completed";
          patch.currentRunId = null;
          patch.message = `GitHub now shows the previously uncertain runs past the permission gate. No approval was resent; prior ambiguous approval is not attributed as confirmed.${
            remaining ? ` ${remaining} remaining workflows need a fresh preview and confirmation.` : ""}`;
        }
        await this.update(r, patch);
      } catch (error) {
        if (this.error) throw error;
        await this.update(r, { observationError: `Could not update workflow status: ${error.message}` });
      }
      return this.public(r);
    })();
    this.observations.set(r.id, task);
    try { return await task; } finally { this.observations.delete(r.id); }
  }
}
