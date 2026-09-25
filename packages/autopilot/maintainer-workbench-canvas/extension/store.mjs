import { mkdir, readFile, writeFile, rename } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Github, pullRecord } from "./github.mjs";
import { STORE, MIN_REFRESH, AUTO_REFRESH, TTL, HORIZONS, QUEUES, parseId, itemId, sanitize, pool } from "./config.mjs";
import { buildModel, makeBrief } from "./model.mjs";
import { VIEW_SCHEMA, SCOPES, defaultView, projectView, reconcileSelection, restoreView, applyAttentionView } from "./scope.mjs";
import { Governance } from "./governance.mjs";
import { mergeObservations, CHECK_INTERVAL, isoNow } from "./observations.mjs";
import { approvalNeedsObservation } from "./async-view.mjs";

export class Store {
  constructor({ root = STORE, github = new Github(), governance, selectedTimeout = 90000, refreshTimeout = 180000, checksTimeout = 45000 } = {}) {
    this.root = root;
    this.github = github;
    this.governance = governance || new Governance(github);
    this.bridge = null;
    this.explanations = null;
    this.workflowApprovals = null;
    this.preparation = null;
    this.snapshot = {};
    this.view = defaultView();
    this.viewNotice = null;
    this.error = null;
    this.refreshing = false;
    this.lastAttempt = 0;
    this.pending = null;
    this.writeQueue = Promise.resolve();
    this.publishQueue = Promise.resolve();
    this.selectedReads = new Map();
    this.selectedTasks = new Map();
    this.selectedActive = 0;
    this.selectedQueue = [];
    this.checkReads = new Map();
    this.checkTasks = new Map();
    this.pullTasks = new Map();
    this.timeouts = { selected: selectedTimeout, portfolio: refreshTimeout, checks: checksTimeout };
    this.refreshProgress = null;
    this.refreshUnderlying = false;
    this.viewIntents = new Map();
    this.viewRevision = 0;
  }

  get directory() { return join(this.root, "repos", "microsoft", "apm"); }

  async load() {
    let savedView = null;
    for (const [file, field] of [["snapshot.json", "snapshot"], ["view.json", "view"]]) {
      try {
        const parsed = JSON.parse(await readFile(join(this.directory, file), "utf8"));
        if (![1, ...(field === "view" ? [2, VIEW_SCHEMA] : [])].includes(parsed.schema) || !parsed.data || typeof parsed.data !== "object") throw new Error("Invalid local cache schema.");
        if (field === "view") {
          if (parsed.schema === VIEW_SCHEMA) this.validateView(parsed.data);
          savedView = sanitize(parsed);
          if (parsed.schema === VIEW_SCHEMA) this.view = savedView.data;
        } else this.snapshot = sanitize(parsed.data);
      } catch (error) {
        if (error.code !== "ENOENT") this.error = `Local ${file} could not be loaded. Refresh to rebuild the read-only cache.`;
      }
    }
    const current = this.state();
    const restored = restoreView(savedView, current.items, current.followingApproval?.itemId);
    this.view = restored.view;
    this.viewNotice = restored.notice;
    await this.save("view.json", this.view);
    return this;
  }

  async save(file, data) {
    const serialized = JSON.stringify({ schema: file === "view.json" ? VIEW_SCHEMA : 1, data: sanitize(data) });
    const operation = this.writeQueue.catch(() => {}).then(async () => {
      await mkdir(this.directory, { recursive: true });
      const target = join(this.directory, file);
      const temporary = `${target}.${randomUUID()}.tmp`;
      await writeFile(temporary, serialized, { mode: 0o600 });
      await rename(temporary, target);
    });
    this.writeQueue = operation;
    return operation;
  }

  state() {
    const model = buildModel({ ...this.snapshot, runFeed: this.bridge ? { runs: this.bridge.runs, feed: this.bridge.feed } : undefined,
      bridgeRequests: this.bridge?.state().requests || [], workflowApprovals: this.workflowApprovals?.state().requests || [] });
    const selectedCandidate = model.items.find(i => i.id === this.view.selectedId);
    const followed = this.workflowApprovals?.state().requests.find(r => r.id === this.view.followingApprovalId && r.status !== "preview" &&
      (r.targetId === selectedCandidate?.id || selectedCandidate?.related.some(ref => r.targetId === itemId("pr", ref.pr))));
    const reconciliation = reconcileSelection(model.items, this.view, followed ? selectedCandidate.id : null);
    const view = reconciliation.view;
    applyAttentionView(model, view);
    const projection = projectView(model.items, view);
    const selected = model.items.find(i => i.id === view.selectedId);
    const detail = selected?.kind === "issue" ? this.snapshot.details?.[selected.number] : this.snapshot.pulls?.[selected?.number];
    const selectedRead = this.selectedReads.get(selected?.id) || null;
    const checksRead = this.checkReads.get(selected?.id) || null;
    const workflowApprovals = this.workflowApprovals?.state(selected?.id, model) || { available: false, error: null, requests: [], pollSeconds: 10 };
    return {
      ...model, view, projection: { ...projection, universe: undefined, matches: undefined, visible: undefined,
        visibleIds: projection.visible.map(i => i.id), universeIds: projection.universe.map(i => i.id) },
      workIds: projection.visible.map(i => i.id),
      counts: Object.fromEntries(QUEUES.map(([id]) => [id, projection.queueCounts[id] || 0])),
      viewNotice: reconciliation.notice || this.viewNotice,
      bridge: this.bridge?.state() || { available: false, requests: [], runs: [], feed: null },
      preparation: this.preparation,
      refreshing: this.refreshing, refreshProgress: this.refreshProgress, selectedRead, checksRead,
      detailLoading: ["queued", "reading"].includes(selectedRead?.status),
      workflowApprovals,
      followingApproval: followed ? { itemId: selectedCandidate.id, requestId: followed.id,
        outsideQueue: !projection.visible.some(i => i.id === selectedCandidate.id) } : null,
      error: this.error, detailError: detail?.error || null,
      detailStatus: detail?.status === "current" && Date.now() - Date.parse(detail.observedAt) >= TTL ? "stale" : detail?.status || "not-loaded",
      brief: selected ? makeBrief(selected, model, this.snapshot) : null,
      explanation: selected && this.explanations ? this.explanations.state(selected.id, model) : null,
      autoRefreshSeconds: AUTO_REFRESH / 1000,
      nextAutoRefreshAt: new Date(this.autoRefreshDueAt()).toISOString(),
      selectedChecksSeconds: CHECK_INTERVAL / 1000,
      minRefreshSeconds: Math.max(0, Math.ceil((MIN_REFRESH - (Date.now() - this.lastAttempt)) / 1000)),
    };
  }

  autoRefreshDueAt() {
    const dates = ["issues", "prs", "roadmap"].map(key => this.snapshot.sources?.[key]?.observedAt);
    dates.push(this.snapshot.lifecycle?.observedAt);
    const oldest = Math.min(...dates.map(date => Number.isFinite(Date.parse(date)) ? Date.parse(date) : 0));
    return Math.max(this.lastAttempt, oldest) + AUTO_REFRESH;
  }

  ensureFresh() {
    if (this.pending || this.refreshUnderlying || Date.now() < this.autoRefreshDueAt()) return;
    this.refresh().catch(error => { this.error = `Automatic GitHub refresh failed: ${error.message}`; });
  }

  async refresh() {
    if (this.pending) return this.pending;
    if (this.refreshUnderlying || Date.now() - this.lastAttempt < MIN_REFRESH) return this.state();
    this.refreshing = true;
    this.lastAttempt = Date.now();
    this.refreshUnderlying = true;
    this.refreshProgress = { status: "reading", kind: "portfolio", startedAt: isoNow(), message: "Checking all issues, pull requests and Roadmap." };
    const guard = { valid: true };
    const work = (async () => {
      try {
        const snapshot = await this.github.portfolio(structuredClone(this.snapshot));
        if (!guard.valid) return;
        this.preparation = { completed: 0, total: snapshot.issues?.nodes?.length || 0 };
        const selected = this.view.selectedId ? parseId(this.view.selectedId) : null;
        snapshot.lifecycle = await this.governance.prepare(snapshot, progress => { this.preparation = progress; },
          { selectedIssue: selected?.kind === "issue" ? selected.number : null });
        if (!guard.valid) return;
        await this.publish(snapshot, true, guard);
        this.error = null;
        this.refreshProgress = { ...this.refreshProgress, status: "completed", finishedAt: isoNow(), message: "Portfolio update finished. Individual source coverage is shown below." };
      } catch (error) {
        if (!guard.valid) return;
        this.error = `Refresh could not be saved or completed: ${error.message}. Last-known evidence remains available; it is not a successful refresh.`;
        this.refreshProgress = { ...this.refreshProgress, status: "error", error: this.error };
      } finally {
        this.refreshUnderlying = false;
      }
    })();
    this.pending = this.withTimeout(work, this.timeouts.portfolio, () => {
      guard.valid = false;
      this.error = "GitHub portfolio update timed out. Previous data is retained. Retry when the outstanding read has finished.";
      this.refreshProgress = { ...this.refreshProgress, status: "error", error: this.error };
    }).finally(() => { this.preparation = null; this.refreshing = false; this.pending = null; }).then(() => this.state());
    return this.pending;
  }

  async withTimeout(work, milliseconds, onTimeout) {
    let timer;
    try { await Promise.race([work, new Promise(resolve => { timer = setTimeout(() => { onTimeout(); resolve(); }, milliseconds); })]); }
    finally { clearTimeout(timer); }
  }

  publish(incoming, portfolio = false, guard = { valid: true }) {
    const task = this.publishQueue.then(async () => {
      if (!guard.valid) return;
      const merged = mergeObservations(this.snapshot, incoming, portfolio);
      await this.save("snapshot.json", merged);
      this.snapshot = merged;
      this.reconcileView();
      await this.save("view.json", this.view);
    });
    this.publishQueue = task.catch(() => {});
    return task;
  }

  async readContextDiscussions(snapshot, id) {
    const model = buildModel(snapshot), item = model.items.find(i => i.id === id);
    if (!item) return;
    const prs = item.kind === "pr" ? [item] : item.related.map(r => model.items.find(i => i.id === itemId("pr", r.pr))).filter(Boolean);
    snapshot.discussions = { ...snapshot.discussions };
    await pool(prs.slice(0, 6), async pr => {
      const prior = snapshot.discussions[pr.number];
      if (prior?.status === "current" && prior.updatedAt === pr.updatedAt && Date.now() - Date.parse(prior.observedAt) < TTL) return;
      try {
        const data = await this.github.selectedDiscussion(pr.number);
        snapshot.discussions[pr.number] = { ...data, status: data.complete ? "current" : "partial", updatedAt: pr.updatedAt };
      } catch (error) {
        snapshot.discussions[pr.number] = { ...prior, status: "error", attemptedAt: isoNow(), error: `PR discussion unavailable: ${error.message}` };
      }
    }, 2);
  }

  validateView(input) {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("View input must be an object.");
    const allowed = ["selectedId", "horizon", "scope", "queue", "search", "person", "area", "followingApprovalId"];
    for (const [key, value] of Object.entries(input)) {
      if (!allowed.includes(key) || (typeof value !== "string" && !(["selectedId", "followingApprovalId"].includes(key) && value === null))) throw new Error("Unsupported view input.");
      if (key === "selectedId" && value !== null) parseId(value);
      if (key === "followingApprovalId" && value !== null && !/^[a-f0-9-]{36}$/.test(value)) throw new Error("Invalid workflow follow-up ID.");
      if (key === "horizon" && !HORIZONS.includes(value)) throw new Error("Invalid Horizon.");
      if (key === "scope" && !SCOPES.some(([scope]) => value === scope)) throw new Error("Invalid scope.");
      if (key === "queue" && !["all", ...QUEUES.map(([id]) => id)].includes(value)) throw new Error("Invalid queue.");
      if (["search", "person", "area"].includes(key) && value.length > 200) throw new Error("Filter text is too long.");
    }
  }

  reconcileView() {
    const current = this.state();
    const result = reconcileSelection(current.items, this.view, current.followingApproval?.itemId);
    this.view = result.view;
    if (result.notice) this.viewNotice = result.notice;
  }

  async setView(input) {
    this.validateView(input);
    this.view = { ...this.view, ...input, followingApprovalId: null };
    this.viewRevision++;
    this.viewNotice = null;
    this.reconcileView();
    await this.save("view.json", this.view);
    return this.state();
  }

  async persistBrief(kind, number) {
    const current = this.state();
    const item = current.items.find(i => i.id === itemId(kind, number));
    if (item) {
      await mkdir(join(this.directory, "briefs"), { recursive: true });
      await this.save(`briefs/${kind}-${number}.json`, makeBrief(item, current, this.snapshot));
    }
    return current;
  }

  async select(id, { wait = false } = {}) {
    parseId(id);
    const model = this.state();
    if (!model.items.some(i => i.id === id)) throw new Error("Work item is not in the loaded repository snapshot.");
    if (!projectView(model.items, this.view).visible.some(i => i.id === id)) throw new Error("Work item is outside the active scope or filters. Choose its scope before selecting it.");
    this.view.selectedId = id;
    this.view.followingApprovalId = null;
    this.viewRevision++;
    this.viewNotice = null;
    const saving = this.save("view.json", this.view);
    this.startSelected(id);
    await saving;
    if (wait) await this.waitSelected(id);
    return this.state();
  }

  waitSelected(id) { return this.selectedTasks.get(id) || Promise.resolve(); }

  startSelected(id) {
    if (this.selectedTasks.has(id)) return;
    for (const entry of this.selectedQueue.splice(0)) {
      this.selectedTasks.delete(entry.id); entry.done();
      this.selectedReads.set(entry.id, { ...this.selectedReads.get(entry.id), status: "superseded" });
    }
    if (this.selectedReads.size >= 100) for (const key of this.selectedReads.keys()) {
      if (!this.selectedTasks.has(key)) { this.selectedReads.delete(key); break; }
    }
    const guard = { valid: true };
    this.selectedReads.set(id, { id, status: "queued", startedAt: isoNow(), message: "Waiting for a selected-item read slot." });
    let done;
    const finished = new Promise(resolve => { done = resolve; });
    this.selectedTasks.set(id, finished);
    this.selectedQueue.push({ id, guard, done });
    this.drainSelected();
  }

  drainSelected() {
    while (this.selectedActive < 2 && this.selectedQueue.length) {
      const { id, guard, done } = this.selectedQueue.shift();
      if (id !== this.view.selectedId) {
        this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "superseded", message: "A newer selection took priority." });
        this.selectedTasks.delete(id); done(); continue;
      }
      this.selectedActive++;
      const work = this.readSelected(id, guard).finally(() => {
        this.selectedActive--; this.selectedTasks.delete(id); done(); this.drainSelected();
      });
      this.withTimeout(work, this.timeouts.selected, () => {
        guard.valid = false;
        this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "error", error: "This item took too long to read. Cached information remains available; retry after the outstanding read finishes." });
      }).catch(error => { this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "error", error: error.message }); });
    }
  }

  readPull(number, previous) {
    if (!this.pullTasks.has(number)) {
      const task = this.github.pull(number).then(data => pullRecord(data, previous)).finally(() => this.pullTasks.delete(number));
      this.pullTasks.set(number, task);
    }
    return this.pullTasks.get(number);
  }

  async readSelected(id, guard) {
    const { kind, number } = parseId(id);
    const model = this.state(), selected = model.items.find(i => i.id === id);
    if (!selected) return;
    const snapshot = structuredClone(this.snapshot);
    const previous = kind === "issue" ? snapshot.details?.[number] : snapshot.pulls?.[number];
    const progress = message => { if (guard.valid) this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "reading", message }); };
    progress(`Checking ${kind === "pr" ? "PR" : "issue"} #${number} and its discussion. You can choose another item.`);
    try {
    if (previous?.status === "current" && Date.now() - Date.parse(previous.observedAt) < TTL &&
        selected.metadataFresh && (kind !== "issue" || selected.lifecycle.current &&
          selected.related.every(ref => model.items.find(i => i.id === itemId("pr", ref.pr))?.evidence?.fresh))) {
      if (this.explanations) await this.readContextDiscussions(snapshot, id);
    } else {
      if (kind === "issue") {
        const [data, lifecycle] = await Promise.all([this.github.selectedIssue(number), (async () => {
          try {
            const trust = await this.governance.trust();
            const record = await this.governance.issue(number, trust);
            return { issues: { [number]: record }, actor: trust.actor, trustedSha: trust.sha, status: "current" };
          } catch (error) {
            return { issues: { [number]: { ...snapshot.lifecycle?.issues?.[number], status: "error", attemptedAt: isoNow(), error: error.message } } };
          }
        })()]);
        if (!guard.valid) return;
        snapshot.details ||= {};
        const partial = data.detailCoverage && (!data.detailCoverage.comments || !data.detailCoverage.timeline);
        snapshot.details[number] = { data, status: partial ? "partial" : "current", observedAt: isoNow() };
        snapshot.lifecycle = { ...snapshot.lifecycle, ...lifecycle, issues: { ...snapshot.lifecycle?.issues, ...lifecycle.issues } };
        snapshot.pulls ||= {};
        progress(`Checking linked pull requests for issue #${number}.`);
        const referenced = [...new Map([...data.timelineReferences.filter(r => r.url?.startsWith("https://github.com/microsoft/apm/pull/")),
          ...selected.related.map(r => ({ number: r.pr }))].map(r => [r.number, r])).values()];
        await pool(referenced, async ref => {
          if (!guard.valid) return;
          const prior = snapshot.pulls[ref.number];
          if (prior?.status === "current" && Date.now() - Date.parse(prior.observedAt) < TTL &&
              model.items.find(i => i.id === itemId("pr", ref.number))?.evidence?.fresh) return;
          try {
            snapshot.pulls[ref.number] = await this.readPull(ref.number, prior);
          } catch (error) {
            snapshot.pulls[ref.number] = { ...prior, status: prior ? "stale" : "error", error: error.message, attemptedAt: isoNow() };
          }
        }, 2);
      } else {
        snapshot.pulls ||= {};
        snapshot.pulls[number] = await this.readPull(number, previous);
      }
      if (guard.valid && this.explanations) await this.readContextDiscussions(snapshot, id);
    }
      if (!guard.valid) return;
      await this.publish(snapshot, false, guard);
      await this.persistBrief(kind, number);
      this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "completed", finishedAt: isoNow(), message: `${kind === "pr" ? "PR" : "Issue"} #${number} updated from GitHub.` });
    } catch (error) {
      if (!guard.valid) return;
      const key = kind === "issue" ? "details" : "pulls";
      try { await this.publish({ [key]: { [number]: { ...previous, status: previous ? "stale" : "error", error: error.message, attemptedAt: isoNow() } } }); }
      catch { this.error = "Selected-item evidence could not be persisted."; }
      this.selectedReads.set(id, { ...this.selectedReads.get(id), status: "error", error: error.message, finishedAt: isoNow() });
    }
  }

  ensureSelectedChecks({ force = false } = {}) {
    const model = this.state(), id = model.view.selectedId, item = model.items.find(i => i.id === id);
    if (!item || this.checkTasks.size || Date.now() - (Date.parse(this.checkReads.get(id)?.startedAt || "") || 0) < CHECK_INTERVAL) return;
    const prs = item.kind === "pr" ? [item] : item.related.map(ref => model.items.find(i => i.id === itemId("pr", ref.pr))).filter(Boolean);
    const pendingApproval = model.workflowApprovals.requests.some(approvalNeedsObservation);
    if (!force && !pendingApproval && !prs.some(p => p.state === "OPEN" && (p.evidence?.pending || p.evidence?.actionRequired))) return;
    if (!prs.length) return;
    const guard = { valid: true };
    if (this.checkReads.size >= 100) this.checkReads.delete(this.checkReads.keys().next().value);
    this.checkReads.set(id, { id, status: "reading", startedAt: isoNow(), message: "Checking selected PR workflows." });
    const work = (async () => {
      const pulls = {};
      const errors = [];
      await pool(prs.filter(p => p.state === "OPEN"), async pr => {
        try { pulls[pr.number] = await this.readPull(pr.number, this.snapshot.pulls?.[pr.number]); }
        catch (error) { errors.push(error.message); }
      }, 2);
      if (!guard.valid) return;
      await this.publish({ pulls }, false, guard);
      for (const approval of model.workflowApprovals.requests.filter(approvalNeedsObservation)) {
        if (!guard.valid) return;
        try { await this.workflowApprovals.observe({ id: approval.id }); } catch (error) { errors.push(error.message); }
      }
      if (guard.valid) this.checkReads.set(id, { ...this.checkReads.get(id), status: errors.length ? "error" : "completed",
        error: errors.join("; ") || null, finishedAt: isoNow() });
    })().catch(error => { if (guard.valid) this.checkReads.set(id, { ...this.checkReads.get(id), status: "error", error: error.message }); })
      .finally(() => this.checkTasks.delete(id));
    this.checkTasks.set(id, work);
    this.withTimeout(work, this.timeouts.checks, () => {
      guard.valid = false;
      this.checkReads.set(id, { ...this.checkReads.get(id), status: "error", error: "Selected checks timed out. Last-known results remain; retry when the outstanding read finishes." });
    });
  }

  acceptViewIntent(instanceId, intent) {
    if (intent === undefined) return true;
    if (!intent || Object.keys(intent).some(key => !["client", "sequence"].includes(key)) ||
        !/^[a-zA-Z0-9-]{8,64}$/.test(intent.client || "") || !Number.isSafeInteger(intent.sequence) || intent.sequence < 1) throw new Error("Invalid view intent.");
    const key = `${instanceId}:${intent.client}`;
    if ((this.viewIntents.get(key) || 0) >= intent.sequence) return false;
    if (this.viewIntents.size >= 128 && !this.viewIntents.has(key)) this.viewIntents.delete(this.viewIntents.keys().next().value);
    this.viewIntents.set(key, intent.sequence);
    return true;
  }

  async followApproval(record, selectedId, viewRevision = this.viewRevision) {
    if (this.view.selectedId !== selectedId || this.viewRevision !== viewRevision) return;
    this.view.followingApprovalId = record.id;
    await this.save("view.json", this.view);
  }
}
