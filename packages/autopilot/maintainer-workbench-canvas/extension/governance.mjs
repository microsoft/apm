import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { isAbsolute } from "node:path";
import { REPO, pool, AUTO_REFRESH, HISTORY_TTL, sanitize } from "./config.mjs";

// Parent-configured trusted checkout, never a request-supplied contribution worktree.
export const TRUSTED_ROOT = process.env.APM_MAINTAINER_TRUSTED_ROOT || null;
const require = createRequire(import.meta.url);
const blobHash = bytes => createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex");
export const digest = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");

export class Governance {
  constructor(github, { root = TRUSTED_ROOT } = {}) { this.github = github; this.root = root; }

  async get(route) {
    if (!/^\/(?:user$|repos\/microsoft\/apm(?:\/|$))/.test(route) || route.includes("..")) throw new Error("Unsupported governance read.");
    return sanitize(await this.github.run(["api", "--hostname", "github.com", "--method", "GET", route]));
  }

  async trust() {
    if (!this.root || !isAbsolute(this.root)) throw new Error("Set APM_MAINTAINER_TRUSTED_ROOT to an absolute trusted microsoft/apm checkout path.");
    const repo = await this.get(`/repos/${REPO}`);
    const commit = await this.get(`/repos/${REPO}/commits/${encodeURIComponent(repo.default_branch)}`);
    if (!/^[a-f0-9]{40}$/.test(commit.sha)) throw new Error("Trusted default-branch revision unavailable.");
    const files = ["scripts/governance/authority.cjs", "scripts/governance/eligibility.cjs"];
    for (const file of files) {
      const bytes = await readFile(`${this.root}/${file}`);
      const remote = await this.get(`/repos/${REPO}/contents/${file}?ref=${commit.sha}`);
      if (blobHash(bytes) !== remote.sha) throw new Error("Trusted governance helper differs from current default branch; parent must reconcile the trusted checkout.");
    }
    if (this.helperSha !== commit.sha) {
      for (const file of files) delete require.cache[require.resolve(`${this.root}/${file}`)];
      this.helperSha = commit.sha;
    }
    const authority = require(`${this.root}/${files[0]}`);
    const eligibility = require(`${this.root}/${files[1]}`);
    let requests = 0, bytes = 0;
    const client = { get: async route => {
      if (++requests > 1000 || bytes > 64 * 1024 * 1024) throw new Error("Decision-history read budget reached. Remaining issues are explicitly unverified; refresh or inspect a selected issue.");
      const result = await this.get(route);
      bytes += Buffer.byteLength(JSON.stringify(result));
      if (bytes > 64 * 1024 * 1024) throw new Error("Decision-history byte budget reached; source coverage is partial.");
      return result;
    } };
    const { policy } = await eligibility.trustedPolicy(client, REPO, commit.sha);
    const actor = await this.get("/user");
    if (!actor.login || actor.type !== "User") throw new Error("Authenticated human actor unavailable.");
    return { authority, eligibility, policy, client, sha: commit.sha,
      actor: { login: actor.login, type: actor.type, canWrite: Boolean(repo.permissions?.push || repo.permissions?.maintain || repo.permissions?.admin),
        responsible: authority.isResponsible(policy, actor, "project"), observedAt: new Date().toISOString() } };
  }

  async issue(number, trust) {
    const { eligibility, client } = trust;
    const [issue, comments, timeline] = await Promise.all([
      client.get(`/repos/${REPO}/issues/${number}`),
      eligibility.listAll(client, `/repos/${REPO}/issues/${number}/comments`),
      // Issue events have stable numeric IDs; timeline cross-references do not.
      eligibility.listAll(client, `/repos/${REPO}/issues/${number}/events`),
    ]);
    if (issue.number !== number || issue.pull_request) throw new Error("Issue context identity mismatch.");
    return reconcileIssue({ issue, comments, timeline, trust });
  }

  async prepare(snapshot, onProgress = () => {}, { selectedIssue = null } = {}) {
    const previous = snapshot.lifecycle || {};
    let trust;
    try { trust = await this.trust(); }
    catch (error) { return { ...previous, status: "error", error: error.message, attemptedAt: new Date().toISOString() }; }
    const records = { ...previous.issues };
    const targets = (snapshot.issues?.nodes || []).filter(i => i.state === "OPEN");
    let completed = 0;
    await pool(targets, async issue => {
      const old = records[issue.number];
      try {
        if (!old || !old.complete || old.status !== "current" || previous.trustedSha !== trust.sha || previous.actor?.login !== trust.actor.login ||
            old.updatedAt !== issue.updatedAt || !Number.isFinite(Date.parse(old.observedAt)) ||
            Date.now() - Date.parse(old.observedAt) >= (issue.number === selectedIssue ? AUTO_REFRESH : HISTORY_TTL)) records[issue.number] = await this.issue(issue.number, trust);
      } catch (error) { records[issue.number] = { ...old, status: "error", error: error.message }; }
      onProgress({ completed: ++completed, total: targets.length });
    });
    return { status: "current", issues: records, actor: trust.actor, trustedSha: trust.sha,
      observedAt: new Date().toISOString(), error: null, complete: targets.every(i => records[i.number]?.status === "current") };
  }

  async validateScope(fields) {
    const trust = await this.trust();
    const line = value => typeof value === "string" && value.trim() && !/[\r\n]/.test(value) && value.length <= 2000;
    for (const key of ["scope", "doneWhen", "outOfScope", "reviewContact"]) if (!line(fields[key])) throw new Error(`Invalid scope field: ${key}.`);
    const area = fields.area || "project";
    const body = `${trust.authority.MARKER}\nDecision: approve\nArea: ${area}\nScope: ${fields.scope}\nDone when: ${fields.doneWhen}\nOut of scope: ${fields.outOfScope}\nReview contact: ${fields.reviewContact}`;
    const record = trust.authority.parseRecord(body);
    if (!record || !trust.authority.isResponsible(trust.policy, { login: trust.actor.login, type: "User" }, area) ||
        !trust.authority.isResponsible(trust.policy, { login: record["Review contact"].slice(1), type: "User" }, area)) throw new Error("Scope author or review contact is outside the canonical governance remit.");
    return { body, trustedSha: trust.sha, actor: trust.actor.login, reviewContactConfirmationNeeded: fields.reviewContact.toLowerCase() !== `@${trust.actor.login.toLowerCase()}` };
  }

  async validateWithdrawal(fields) {
    const trust = await this.trust(), area = fields.area || "project";
    if (typeof fields.reason !== "string" || !fields.reason.trim() || /[\r\n]/.test(fields.reason) || fields.reason.length > 2000) throw new Error("Invalid withdrawal reason: use one paragraph, up to 2000 characters.");
    const body = `${trust.authority.MARKER}\nDecision: withdraw\nArea: ${area}\nReason: ${fields.reason}`;
    if (!trust.authority.parseRecord(body) || !trust.authority.isResponsible(trust.policy, { login: trust.actor.login, type: "User" }, area)) throw new Error("Scope withdrawal author is outside the canonical governance remit.");
    return { body, trustedSha: trust.sha, actor: trust.actor.login };
  }
}

export function reconcileIssue({ issue, comments, timeline, trust }) {
  const { authority, policy, actor } = trust;
  const ordered = [...comments].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const approvals = ordered.filter(c => authority.parseRecord(c.body)?.Decision === "approve" &&
    authority.isResponsible(policy, c.user, authority.parseRecord(c.body).Area));
  const nominated = approvals.at(-1);
  const approvalUrl = nominated ? `https://github.com/${REPO}/issues/${issue.number}#issuecomment-${nominated.id}` : null;
  const acceptance = authority.evaluateIssue({ policy, repository: REPO, issue, comments, approvalUrl });
  const advisory = ordered.filter(c => new RegExp(`<!-- apm-triage-advisory:v[12] target=issue#${issue.number} watermark=([^\\s]+) -->`).test(c.body)).at(-1);
  const advisoryEdited = advisory && advisory.created_at !== advisory.updated_at;
  const humanReplies = advisory ? ordered.filter(c => Date.parse(c.created_at) > Date.parse(advisory.created_at) && authority.isResponsible(policy, c.user, "project")) : [];
  const currentLabels = new Set((issue.labels || []).map(l => typeof l === "string" ? l : l.name));
  const latestLabels = new Map();
  for (const event of [...timeline].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))) {
    if (["labeled", "unlabeled"].includes(event.event) && ["status/deferred", "status/needs-design"].includes(event.label?.name)) latestLabels.set(event.label.name, event);
  }
  const dispositionEvents = [...latestLabels.values()].filter(e => e.event === "labeled" && currentLabels.has(e.label.name) &&
    authority.isResponsible(policy, e.actor, "project"));
  const deferred = dispositionEvents.filter(e => e.label.name === "status/deferred").at(-1);
  const design = dispositionEvents.filter(e => e.label.name === "status/needs-design").at(-1);
  const unresolved = Boolean(advisory && !advisoryEdited && acceptance.state !== "record-present" && !deferred && !design &&
    !humanReplies.length && !currentLabels.has("status/accepted") && !currentLabels.has("status/deferred"));
  return {
    status: "current", observedAt: new Date().toISOString(), updatedAt: issue.updated_at,
    watermark: digest({ issue: [issue.updated_at, issue.state, [...currentLabels].sort()], comments: comments.map(c => [c.id, c.updated_at, c.body]),
      timeline: timeline.map(e => [e.id, e.event, e.created_at, e.label?.name, e.actor?.login]) }),
    acceptance: { ...acceptance, approvalUrl, record: nominated ? authority.parseRecord(nominated.body) : null },
    recommendation: advisory ? { url: advisory.html_url, body: advisory.body, author: advisory.user?.login,
      updatedAt: advisory.updated_at, id: advisory.id, status: unresolved ? "unresolved" : "retained-context" } : null,
    disposition: deferred ? { kind: "deferred", actor: deferred.actor.login, at: deferred.created_at } : null,
    design: design ? { actor: design.actor.login, at: design.created_at } : null,
    followup: advisoryEdited ? "Edited advisory needs reconciliation; its marker alone is not a verified unresolved decision." :
      humanReplies.length > 0 && acceptance.state !== "record-present" && !deferred && !design ? "Later human discussion needs reconciliation; prior advice is not a new approval request." : null,
    labelMismatch: (currentLabels.has("status/accepted") && acceptance.state !== "record-present") ||
      (currentLabels.has("status/deferred") && !deferred),
    responsibleActor: actor.responsible ? actor.login : null,
    complete: true, commentCount: comments.length,
    latestHumanDiscussion: ordered.filter(c => c.user?.type === "User").slice(-12).map(c => ({
      author: c.user.login, body: c.body.slice(0, 2400), url: c.html_url, updatedAt: c.updated_at,
      truncated: c.body.length > 2400,
    })),
  };
}
