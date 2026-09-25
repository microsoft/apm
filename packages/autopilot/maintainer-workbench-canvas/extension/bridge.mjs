import { randomUUID, randomBytes, timingSafeEqual } from "node:crypto";
import { readFile, mkdir, writeFile, rename, unlink } from "node:fs/promises";
import { join } from "node:path";
import { ACTIONS, validateParams, executionContract, relayPrompt } from "./actions.mjs";
import { digest } from "./governance.mjs";
import { parseId, sanitize, safeUrl, TTL } from "./config.mjs";

const ACTIVE = new Set(["sending", "awaiting-host", "awaiting-confirmation", "queued", "running", "plan-required", "cancel-requested", "reconcile-required", "partial"]);
const FINAL = new Set(["completed", "failed", "blocked", "cancelled"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const exact = (input, fields) => {
  if (!input || typeof input !== "object" || Array.isArray(input) || Object.keys(input).some(key => !fields.includes(key))) throw new Error("Invalid request fields.");
};
const text = (value, max = 2000) => typeof value === "string" && value.length > 0 && value.length <= max;
export const REQUEST_VERSION = 1;

export class Bridge {
  constructor(store, { send = null, fixtureOnly = false } = {}) {
    this.store = store; this.send = send; this.fixtureOnly = fixtureOnly;
    this.requests = []; this.runs = []; this.feed = null; this.queue = Promise.resolve(); this.error = null;
  }
  get path() { return join(this.store.directory, "requests.json"); }
  async load() {
    try {
      const saved = JSON.parse(await readFile(this.path, "utf8"));
      if (saved.schema !== 1 || !Array.isArray(saved.requests) || !Array.isArray(saved.runs) || saved.requests.length > 200 ||
          new Set(saved.requests.map(r => r.id)).size !== saved.requests.length) throw new Error("Invalid request journal.");
      for (const r of saved.requests) {
        if (!UUID.test(r.id) || !ACTIONS[r.kind] || r.version !== REQUEST_VERSION || !Number.isSafeInteger(r.revision) || r.revision < 1 ||
            !["preview", ...ACTIVE, ...FINAL].includes(r.status) || !Array.isArray(r.targetIds) || !Array.isArray(r.lockedIds) ||
            !Array.isArray(r.receipts) || typeof r.watermark !== "string") throw new Error("Invalid saved request.");
        r.targetIds.forEach(parseId); r.lockedIds.forEach(parseId);
        if (digest({ kind: r.kind, ids: r.targetIds, params: validateParams(r.kind, r.params) }) !== r.identity) throw new Error("Saved request identity mismatch.");
      }
      for (const r of saved.runs) {
        if (!text(r.id, 100) || !Array.isArray(r.targetIds) || !Number.isFinite(Date.parse(r.observedAt))) throw new Error("Invalid saved run.");
        r.targetIds.forEach(parseId);
      }
      this.requests = saved.requests; this.runs = saved.runs; this.feed = saved.feed || null;
      for (const r of this.requests) if (ACTIVE.has(r.status)) {
        r.status = "reconcile-required"; r.message = "Provider restarted; parent must reconcile prior effects before continuing. No automatic resend.";
        r.revision++; delete r.claimToken;
      }
      await this.save();
    } catch (error) { if (error.code !== "ENOENT") this.error = "Request journal could not be loaded; actions blocked. Parent must inspect storage, not reset it blindly."; }
  }
  async save() {
    const temp = `${this.path}.${randomUUID()}.tmp`;
    try {
      await mkdir(this.store.directory, { recursive: true });
      await writeFile(temp, JSON.stringify({ schema: 1, requests: sanitize(this.requests), runs: sanitize(this.runs), feed: this.feed }), { mode: 0o600 });
      await rename(temp, this.path);
    } catch {
      this.error = "Request journal could not be persisted. Actions blocked; parent must reconcile durable receipts before restarting.";
      throw new Error(this.error);
    } finally {
      try { await unlink(temp); } catch (error) { if (error.code !== "ENOENT") this.error = "Request journal cleanup failed. Actions blocked; parent must inspect storage."; }
    }
  }
  serial(fn) { const next = this.queue.then(() => { if (this.error) throw new Error(this.error); return fn(); }); this.queue = next.catch(() => {}); return next; }
  async relay(prompt) {
    let timer;
    try {
      return await Promise.race([this.send({ prompt, mode: "immediate" }), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Parent acknowledgement timed out; delivery is uncertain.")), 15000);
      })]);
    } finally { clearTimeout(timer); }
  }
  public(r) { const { claimToken, ...record } = r; return sanitize(record); }
  state() { return { version: REQUEST_VERSION, available: Boolean(this.send) && !this.error, error: this.error,
    requests: this.requests.map(r => this.public(r)), runs: this.runs, feed: this.feed }; }
  get(id) {
    if (!UUID.test(id || "")) throw new Error("Invalid request ID.");
    const record = this.requests.find(r => r.id === id);
    if (!record) throw new Error("Invalid request: not found.");
    return record;
  }
  context(ids) {
    const model = this.store.state();
    const items = ids.map(id => { parseId(id); const item = model.items.find(i => i.id === id); if (!item) throw new Error("Invalid target: not loaded."); return item; });
    const actor = model.lifecycle?.actor;
    const associated = new Set(items.flatMap(i => [i.id, ...i.related.map(r => `microsoft/apm/${i.kind === "issue" ? "pr" : "issue"}/${r.pr || r.issue}`)]));
    return { items, actor, watermark: digest({ actor, trustedSha: model.lifecycle?.trustedSha,
      items: items.map(i => ({ id: i.id, updatedAt: i.updatedAt, head: i.head, placement: i.placement,
        labels: i.labels, assignees: i.assignees, related: i.related, record: i.lifecycle?.record?.watermark,
        acceptance: i.lifecycle?.record?.acceptance })),
      runs: this.runs.filter(r => r.targetIds.some(id => associated.has(id))) }) };
  }
  assertCurrent(request) {
    if (request.kind === "smoke") return;
    const ctx = this.context(request.targetIds);
    if (ctx.watermark !== request.watermark) throw new Error("Stale request: evidence or ownership changed. Create a new preview.");
    if (!ctx.actor || !Number.isFinite(Date.parse(ctx.actor.observedAt)) || Math.abs(Date.now() - Date.parse(ctx.actor.observedAt)) >= TTL) throw new Error("Stale actor evidence. Refresh before confirmation.");
    const consequential = !["scope-draft", "smoke"].includes(request.kind) && !(["triage", "admission"].includes(request.kind) && request.params.mode === "preview");
    if (consequential && !ctx.actor.canWrite) throw new Error("Current actor lacks observed write permission.");
    if (["defer", "design", "decline"].includes(request.kind) && !ctx.actor.responsible) throw new Error("Responsible maintainer confirmation is required.");
    if (request.scopeRecord && (request.scopeRecord.actor !== ctx.actor.login || request.scopeRecord.trustedSha !== this.store.snapshot.lifecycle?.trustedSha)) throw new Error("Stale scope author or governance revision. Refresh before confirming.");
    if (ctx.items.some(i => !i.metadataFresh || !i.fieldCoverage || (i.kind === "issue" && !i.lifecycle?.current))) throw new Error("Stale or incomplete issue evidence. Reconcile before confirmation.");
    if ((request.kind === "horizon" || request.params.horizon) && this.store.snapshot.sources?.roadmap?.status !== "current") throw new Error("Stale or incomplete Roadmap evidence. Refresh before a planning update.");
  }
  async preview(input) {
    return this.serial(async () => {
      exact(input, ["id", "kind", "targetIds", "params", "version"]);
      if (this.error) throw new Error(this.error);
      if (input.version !== REQUEST_VERSION || !UUID.test(input.id || "") || !ACTIONS[input.kind]) throw new Error("Invalid action version, ID or kind.");
      if (!Array.isArray(input.targetIds) || new Set(input.targetIds).size !== input.targetIds.length || input.targetIds.length > 10) throw new Error("Invalid bounded targets.");
      if (this.fixtureOnly && input.kind !== "smoke") throw new Error("Fixture bridge cannot target live work.");
      const definition = ACTIONS[input.kind];
      if (input.kind === "smoke" ? input.targetIds.length !== 0 : input.targetIds.length < 1 || (!definition.batch && input.targetIds.length !== 1)) throw new Error("Invalid action target count.");
      const params = validateParams(input.kind, input.params);
      if (JSON.stringify(sanitize(params)) !== JSON.stringify(params)) throw new Error("Invalid parameters: remove credentials or control characters before previewing.");
      const identity = digest({ kind: input.kind, ids: input.targetIds, params });
      const duplicate = this.requests.find(r => r.id === input.id);
      if (duplicate) { if (duplicate.identity !== identity) throw new Error("Conflict: request ID already has a different preview."); return this.public(duplicate); }
      const ctx = input.kind === "smoke" ? { items: [], actor: null, watermark: "fixture-v1" } : this.context(input.targetIds);
      if (ctx.items.some(i => !definition.kinds.includes(i.kind))) throw new Error("Invalid target kind.");
      if (ctx.items.some(i => i.state !== "OPEN") && !["scope-draft"].includes(input.kind)) throw new Error("Closed work is history, not a new execution target.");
      const lockedIds = [...new Set(ctx.items.flatMap(i => [i.id, ...i.related.map(r => `microsoft/apm/${i.kind === "issue" ? "pr" : "issue"}/${r.pr || r.issue}`)]))];
      if (this.requests.some(r => ACTIVE.has(r.status) && r.lockedIds.some(id => lockedIds.includes(id)))) throw new Error("Conflict: a related request is already pending. Reconcile it first.");
      if (this.requests.length >= 200) throw new Error("Request journal limit reached; parent must archive resolved receipts explicitly.");
      if (input.kind === "implement") {
        const item = ctx.items[0];
        if (!item.lifecycle.accepted || item.lifecycle.record.acceptance.approvalUrl !== params.approvalUrl) throw new Error("Implementation needs the nominated current human record, not a label.");
        if (item.related.some(ref => this.store.state().items.some(p => p.kind === "pr" && p.number === ref.pr && p.state === "OPEN")) ||
            this.runs.some(r => r.targetIds.includes(item.id) && !["completed", "cancelled"].includes(r.state))) throw new Error("Existing contribution or run: coordinate/review/resume, do not start duplicate delivery.");
      }
      if (["recover", "review"].includes(input.kind) && this.runs.some(r => r.targetIds.some(id => lockedIds.includes(id)) &&
          !["completed", "cancelled"].includes(r.state) && (input.kind === "recover" ? r.kind !== "triage" : r.kind === "review"))) throw new Error("Existing mapped driver: inspect its state and coordinate or resume that same session before another request.");
      let runContext;
      if (input.kind === "resume") {
        const run = this.runs.find(r => r.id === params.runId && r.targetIds.some(id => lockedIds.includes(id)));
        if (!run || run.current === false || !["waiting-human", "blocked"].includes(run.state) || Date.now() - Date.parse(run.observedAt) >= TTL) throw new Error("No fresh resumable mapped run. Ask parent to observe the existing session.");
        runContext = { id: run.id, sessionId: run.sessionId, targetIds: run.targetIds, actor: run.actor, mandate: run.mandate, blocker: run.blocker, observedAt: run.observedAt };
      }
      if ((input.kind === "horizon" || params.horizon) && (!ctx.items[0]?.placement || ctx.items[0].placement.archived !== false)) throw new Error("Existing nonarchived Roadmap item required for a Horizon update. This action does not add or unarchive items.");
      let scopeRecord;
      if (input.kind === "scope-accept") scopeRecord = await this.store.governance.validateScope(params);
      if (input.kind === "scope-withdraw") scopeRecord = await this.store.governance.validateWithdrawal(params);
      const record = { id: input.id, version: REQUEST_VERSION, identity, kind: input.kind, targetIds: input.targetIds,
        params, lockedIds, watermark: ctx.watermark, actor: ctx.actor?.login || null, revision: 1, status: "preview",
        createdAt: new Date().toISOString(), effects: definition.effects, scopeRecord, runContext,
        message: "Preview only. Nothing has been sent or executed.", receipts: [] };
      this.requests.push(record); await this.save(); return this.public(record);
    });
  }
  confirm(input, instanceId) {
    return this.serial(async () => {
      exact(input, ["id", "revision", "watermark"]);
      const r = this.get(input.id);
      if (r.status !== "preview") return this.public(r);
      if (r.revision !== input.revision || r.watermark !== input.watermark) throw new Error("Stale preview revision.");
      if (!this.send) throw new Error("Parent transport unavailable. No action was sent.");
      this.assertCurrent(r);
      if (this.requests.some(other => other.id !== r.id && ACTIVE.has(other.status) && other.lockedIds.some(id => r.lockedIds.includes(id)))) throw new Error("Conflict: another request acquired this work after the preview.");
      r.status = "sending"; r.revision++; r.confirmedAt = new Date().toISOString(); r.instanceId = instanceId;
      r.message = "Sending request to parent; this is not an implementation mandate or running worker.";
      await this.save();
      try {
        const messageId = await this.relay(relayPrompt(r, instanceId));
        r.transportMessageId = text(messageId, 200) && sanitize(messageId) === messageId ? messageId : null;
        r.status = "awaiting-host"; r.message = "Request received by transport. Waiting for parent observation and real host confirmation; no worker reported running.";
      } catch {
        r.status = "reconcile-required"; r.message = "Parent transport failed or acknowledgement is ambiguous. Inspect this request before retrying; it was not marked running.";
      }
      r.revision++; await this.save(); return this.public(r);
    });
  }
  claim(input) {
    return this.serial(async () => {
      exact(input, ["id", "revision"]);
      const r = this.get(input.id);
      if (r.revision !== input.revision || !["awaiting-host", "reconcile-required"].includes(r.status)) throw new Error("Conflict: request already claimed, cancelled or changed.");
      let staleReason = null;
      if (r.status !== "reconcile-required") {
        try { this.assertCurrent(r); } catch (error) { staleReason = error.message; }
      }
      const reconcileOnly = r.status === "reconcile-required" || Boolean(staleReason);
      r.claimToken = randomBytes(32).toString("hex"); r.status = "awaiting-confirmation"; r.revision++;
      r.reconcileOnly = reconcileOnly;
      r.message = reconcileOnly ? `Parent must reconcile prior effects or stale evidence. No new execution allowed. ${staleReason || ""}` : "Parent claimed request. Awaiting a fresh host gate; not running.";
      await this.save();
      return { ...this.public(r), claimToken: r.claimToken, executionContract: executionContract(r) };
    });
  }
  report(input) {
    return this.serial(async () => {
      exact(input, ["id", "revision", "claimToken", "status", "message", "gate", "receipts", "draft"]);
      const original = this.get(input.id);
      const r = structuredClone(original);
      const supplied = Buffer.from(input.claimToken || ""), expected = Buffer.from(r.claimToken || "");
      if (!expected.length || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw new Error("Invalid parent claim token.");
      if (r.revision !== input.revision || FINAL.has(r.status)) throw new Error("Conflict: report revision or terminal state.");
      if (!["awaiting-confirmation", "queued", "running", "plan-required", "partial", "completed", "failed", "blocked", "cancelled"].includes(input.status) || !text(input.message)) throw new Error("Invalid result status or message.");
      if (r.status === "cancel-requested" && !["cancelled", "partial", "completed", "failed", "blocked"].includes(input.status)) throw new Error("Cancellation pending; do not start more work.");
      if (r.reconcileOnly && ["queued", "running"].includes(input.status)) throw new Error("Ambiguous request may only be reconciled, not redispatched.");
      const consequential = !["scope-draft", "smoke"].includes(r.kind);
      if (input.gate) {
        exact(input.gate, ["tool", "reference", "actor", "observedAt", "confirmed", "planApproved"]);
        if (!text(input.gate.tool, 100) || !text(input.gate.reference, 500) || input.gate.actor !== r.actor ||
            input.gate.confirmed !== true || !Number.isFinite(Date.parse(input.gate.observedAt)) ||
            Math.abs(Date.now() - Date.parse(input.gate.observedAt)) > TTL || Date.parse(input.gate.observedAt) > Date.now() + 10000 ||
            Date.parse(input.gate.observedAt) < Date.parse(r.confirmedAt)) throw new Error("Invalid or stale host confirmation receipt.");
        r.gate = input.gate;
      }
      if (consequential && ["queued", "running", "plan-required", "completed", "partial"].includes(input.status) && !r.gate) throw new Error("Actual host confirmation receipt required.");
      if (r.kind === "recover" && ["running", "completed"].includes(input.status) && r.gate?.planApproved !== true) throw new Error("Real plan approval is required before recovery execution.");
      const receipts = input.receipts || [];
      if (!Array.isArray(receipts) || receipts.length > 30) throw new Error("Invalid receipts.");
      for (const receipt of receipts) {
        exact(receipt, ["step", "tool", "reference", "observedAt", "outcome", "url", "resolves"]);
        if (!text(receipt.step, 100) || !text(receipt.tool, 100) || !text(receipt.reference, 1000) ||
            !["verified", "failed", "uncertain", "noop"].includes(receipt.outcome) ||
            !Number.isFinite(Date.parse(receipt.observedAt)) || Date.parse(receipt.observedAt) > Date.now() + 10000 ||
            Date.parse(receipt.observedAt) < Date.parse(r.confirmedAt) || (receipt.url && !safeUrl(receipt.url))) throw new Error("Invalid tool/readback receipt.");
      }
      const all = [...r.receipts, ...receipts];
      const unresolved = new Set();
      for (const receipt of all) {
        if (["failed", "uncertain"].includes(receipt.outcome)) unresolved.add(receipt.reference);
        if (receipt.resolves !== undefined) {
          if (receipt.step !== "reconciliation" || receipt.outcome !== "verified" || !Array.isArray(receipt.resolves) ||
              !receipt.resolves.length || receipt.resolves.length > 30 || receipt.resolves.some(ref => !text(ref, 1000) || !unresolved.has(ref))) throw new Error("Reconciliation must explicitly reference earlier unresolved receipts and verify actual effects.");
          receipt.resolves.forEach(ref => unresolved.delete(ref));
        }
      }
      if (["triage", "admission", "implement", "review", "recover", "resume"].includes(r.kind) &&
          input.status === "running" && !all.some(s => s.step === "session-observed" && s.outcome === "verified")) throw new Error("Running requires a tool-observed session receipt, not a transport acknowledgement.");
      if (input.status === "queued" && !all.some(s => ["session-observed", "capacity-observed"].includes(s.step) && s.outcome === "verified")) throw new Error("Queued requires an actual session or capacity observation, not a transport acknowledgement.");
      if (input.status === "plan-required" && !all.some(s => s.step === "plan-observed" && s.outcome === "verified")) throw new Error("Plan-required needs an actual pending host plan observation.");
      const commentActions = ["scope-accept", "scope-withdraw", "defer", "design", "decline"];
      if (commentActions.includes(r.kind)) {
        let verifiedComment = false;
        for (const receipt of all) {
          if (receipt.step === "comment-readback" && receipt.outcome === "verified") verifiedComment = true;
          if (receipt.step === "metadata" && !verifiedComment) throw new Error("Comment readback must precede metadata; report partial effects honestly.");
        }
        if (input.status === "completed" && (!verifiedComment || !all.some(s => s.step === "metadata" && ["verified", "noop"].includes(s.outcome)))) throw new Error("Completion needs verified comment then metadata receipts.");
      }
      if (input.status === "completed" && (!all.length || unresolved.size)) throw new Error("Completion requires verified/noop receipts without unresolved failures.");
      if (input.draft) {
        if (r.kind !== "scope-draft") throw new Error("Invalid draft return.");
        r.draft = validateParams("scope-accept", input.draft);
      }
      if (r.kind === "scope-draft" && input.status === "completed" && !r.draft) throw new Error("Scope drafting completion requires the proposed fields.");
      r.receipts = all; r.status = input.status; r.message = input.message; r.updatedAt = new Date().toISOString(); r.revision++;
      const saved = sanitize(r);
      this.requests[this.requests.indexOf(original)] = saved;
      await this.save(); return this.public(saved);
    });
  }
  cancel(input, instanceId) {
    return this.serial(async () => {
      exact(input, ["id", "revision"]);
      const r = this.get(input.id);
      if (r.revision !== input.revision) throw new Error("Stale cancellation revision.");
      if (FINAL.has(r.status)) return this.public(r);
      const local = r.status === "preview";
      r.status = local ? "cancelled" : "cancel-requested"; r.revision++;
      r.message = local ? "Preview cancelled. No request sent." : "Cancellation requested. Parent must verify actual run state; existing effects are not rolled back.";
      await this.save();
      if (!local && this.send) {
        try {
          const messageId = await this.relay(`[Maintainer canvas cancellation] instance ${instanceId}, request ${r.id}. Read get_request; do not start new effects. Reconcile actual work and report cancelled/partial. This is not permission to stop an unrelated session.`);
          r.cancellationMessageId = text(messageId, 200) && sanitize(messageId) === messageId ? messageId : null;
        }
        catch { r.message += " Cancellation transport failed; parent reconciliation is required."; }
        await this.save();
      }
      return this.public(r);
    });
  }
  reportRuns(input) {
    return this.serial(async () => {
      exact(input, ["observedAt", "tool", "reference", "runs", "complete"]);
      if (!text(input.tool, 100) || !text(input.reference, 1000) || !Number.isFinite(Date.parse(input.observedAt)) ||
          Math.abs(Date.now() - Date.parse(input.observedAt)) > TTL || Date.parse(input.observedAt) > Date.now() + 10000 ||
          !Array.isArray(input.runs) || input.runs.length > 100 || typeof input.complete !== "boolean") throw new Error("Invalid observed run feed.");
      if (new Set(input.runs.map(r => r.id)).size !== input.runs.length || new Set(input.runs.map(r => r.sessionId)).size !== input.runs.length) throw new Error("Invalid duplicate run/session mapping.");
      if (this.feed && Date.parse(input.observedAt) < Date.parse(this.feed.observedAt)) throw new Error("Stale run feed cannot replace a newer observation.");
      for (const r of input.runs) {
        exact(r, ["id", "sessionId", "targetIds", "name", "kind", "state", "actor", "mandate", "blocker"]);
        if (!text(r.id, 100) || !text(r.sessionId, 100) || !text(r.name, 200) || !text(r.actor, 100) || !text(r.mandate, 2000) ||
            !["triage", "delivery", "review", "recovery"].includes(r.kind) ||
            !["queued", "running", "waiting-human", "plan-required", "blocked", "completed", "cancelled"].includes(r.state) ||
            !Array.isArray(r.targetIds) || !r.targetIds.length || r.targetIds.length > 10 ||
            new Set(r.targetIds).size !== r.targetIds.length || (r.blocker !== undefined && !text(r.blocker))) throw new Error("Invalid mapped run.");
        r.targetIds.forEach(parseId);
      }
      const returned = new Set(input.runs.map(r => r.id));
      const sessions = new Set(input.runs.map(r => r.sessionId));
      const prior = this.runs.filter(r => !returned.has(r.id) && !sessions.has(r.sessionId) && (!input.complete || !["completed", "cancelled"].includes(r.state)))
        .map(r => input.complete ? { ...r, current: false } : r);
      this.runs = sanitize([...prior, ...input.runs.map(r => ({ ...r, current: true, observedAt: input.observedAt, source: input.tool, reference: input.reference }))]);
      this.feed = sanitize({ observedAt: input.observedAt, tool: input.tool, reference: input.reference, complete: input.complete });
      await this.save(); return this.state();
    });
  }
}
