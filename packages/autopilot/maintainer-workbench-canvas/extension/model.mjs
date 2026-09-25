import { GH, REPO, TTL, QUEUES, itemId, safeUrl } from "./config.mjs";
import { defaultView, projectView } from "./scope.mjs";
import { lifecycleFor, attentionFor } from "./lifecycle.mjs";
import { primaryAction } from "./primary-action.mjs";

const FAILURES = new Set(["FAILURE", "TIMED_OUT", "STARTUP_FAILURE"]);
const TERMINAL_OTHER = new Set(["CANCELLED", "STALE", "SKIPPED", "NEUTRAL"]);
const nodes = value => value?.nodes || [];
export const fresh = (date, now = Date.now()) => Boolean(date && now - Date.parse(date) < TTL && Date.parse(date) <= now + 60000);

export function stateAppearance(item) {
  if (item.kind === "pr" && item.state === "MERGED") return { key: "merged", label: "Merged" };
  if (item.state === "CLOSED") {
    if (item.kind === "pr") return { key: "closed-pr", label: "Closed" };
    if (item.stateReason === "COMPLETED") return { key: "completed", label: "Completed" };
    if (item.stateReason === "NOT_PLANNED") return { key: "not-planned", label: "Not planned" };
    return { key: "closed", label: "Closed" };
  }
  if (item.state !== "OPEN") return { key: "unknown", label: "Unknown" };
  return item.kind === "pr" && item.isDraft ? { key: "draft", label: "Draft" } : { key: "open", label: "Open" };
}

function completedWithoutFailures(checks, runs) {
  return checks.length > 0 &&
    checks.every(c => c.status === "COMPLETED" && ["SUCCESS", "NEUTRAL", "SKIPPED"].includes(c.conclusion)) &&
    runs.every(r => r.status === "completed" && ["success", "neutral", "skipped"].includes(r.conclusion));
}

export function evidenceFor(raw, record, now = Date.now()) {
  const evidence = record?.data?.evidence;
  const unknown = { status: "unknown", summary: "Checks not observed for the current head.", checks: [], runs: [], head: raw.headRefOid, observedAt: null, complete: false };
  if (!evidence) return unknown;
  if (evidence.head !== raw.headRefOid) return { ...unknown, status: "outdated-head", summary: "Stored checks belong to a different PR head; current-head evidence is unknown.", previousHead: evidence.head };
  const checks = evidence.checks.nodes.map(check => ({
    name: check.name || check.context,
    status: check.status || (check.state === "PENDING" ? "IN_PROGRESS" : "COMPLETED"),
    conclusion: check.conclusion || (check.state === "ERROR" ? "FAILURE" : check.state),
    url: safeUrl(check.detailsUrl || check.targetUrl),
    completedAt: check.completedAt || check.createdAt || null,
    startedAt: check.startedAt || null,
    platform: /windows/i.test(check.name || "") ? "Windows (job name)" : /linux|ubuntu/i.test(check.name || "") ? "Linux (job name)" : /macos|mac os/i.test(check.name || "") ? "macOS (job name)" : "Not identified by job name",
  }));
  const latestRuns = new Map();
  for (const run of evidence.runs.nodes.filter(r => r.head_sha === raw.headRefOid)) {
    const key = `${run.workflow_id || run.name}:${run.event || ""}`;
    const prior = latestRuns.get(key);
    const rank = r => [Date.parse(r.created_at || "") || 0, Number(r.id) || 0, Number(r.run_attempt) || 0, Date.parse(r.updated_at || "") || 0];
    const values = rank(run), before = prior ? rank(prior) : [];
    const different = values.findIndex((value, index) => value !== before[index]);
    if (!prior || different >= 0 && values[different] > before[different]) latestRuns.set(key, run);
  }
  const runs = [...latestRuns.values()].map(run => ({
    id: run.id, name: run.name, status: run.status, conclusion: run.conclusion,
    url: safeUrl(run.html_url), head: run.head_sha, updatedAt: run.updated_at,
    attempt: run.run_attempt,
  }));
  const failed = checks.filter(c => FAILURES.has(c.conclusion));
  const failedRuns = runs.filter(r => FAILURES.has(r.conclusion?.toUpperCase()));
  const cancelled = checks.filter(c => c.conclusion === "CANCELLED");
  const cancelledRuns = runs.filter(r => r.conclusion === "cancelled");
  const outcomeCounts = new Map();
  for (const check of checks.filter(c => c.conclusion !== "SUCCESS")) {
    const label = check.conclusion?.toLowerCase().replaceAll("_", " ") || "unreported conclusion";
    outcomeCounts.set(label, (outcomeCounts.get(label) || 0) + 1);
  }
  const outcomeSummary = new Intl.ListFormat("en").format([...outcomeCounts].sort(([a], [b]) => a.localeCompare(b)).map(([label, count]) => `${count} ${label}`));
  const cancellationSummary = [
    ...(cancelled.length ? [`${cancelled.length} cancelled check${cancelled.length === 1 ? "" : "s"}`] : []),
    ...(cancelledRuns.length ? [`${cancelledRuns.length} cancelled workflow run${cancelledRuns.length === 1 ? "" : "s"}`] : []),
  ].join(" and ");
  const actionRequired = checks.some(c => c.conclusion === "ACTION_REQUIRED") || runs.some(r => r.conclusion === "action_required");
  const pending = checks.some(c => c.status !== "COMPLETED") || runs.some(r => r.status !== "completed");
  const complete = evidence.checks.complete && evidence.runs.complete;
  const isFresh = record.status === "current" && fresh(evidence.observedAt, now) &&
    (!evidence.runs.observedAt || fresh(evidence.runs.observedAt, now));
  let status = "unknown";
  let summary = "No checks were returned. This does not establish a workflow approval gate or readiness.";
  if (failed.length || failedRuns.length) {
    status = "failed";
    const names = [...failed.map(c => c.name), ...failedRuns.map(r => `Workflow ${r.name}`)];
    summary = `${names.join(", ")} reported failure.${cancellationSummary ? ` Also observed: ${cancellationSummary}.` : ""} The cause has not been diagnosed here.`;
  } else if (actionRequired) {
    status = "action-required";
    summary = "GitHub reports action required. Inspect the workflow run; this is not a test failure.";
  } else if (pending) {
    status = "running";
    summary = "GitHub checks or workflow runs are still in progress.";
  } else if (cancellationSummary) {
    status = "mixed";
    summary = `Observed ${cancellationSummary}. Cancellation is not a passing outcome. Required-check coverage and acceptance are not verified by this prototype.`;
  } else if (checks.length && checks.every(c => c.conclusion === "SUCCESS")) {
    status = "observed-success";
    summary = "All returned check contexts report success. Required-check and acceptance coverage are not verified.";
  } else if (completedWithoutFailures(checks, runs)) {
    status = "mixed";
    summary = `${complete ? "Checks" : "Returned checks"} completed with no reported failures; ${outcomeSummary}. Required-check coverage and acceptance are not verified by this prototype.`;
  } else if (checks.length) {
    status = "mixed";
    summary = `Returned check outcomes include ${outcomeSummary}. Required-check coverage and acceptance are not verified by this prototype.`;
  }
  return {
    status, summary, checks, runs, failed: failed.length, actionRequired, pending,
    other: checks.filter(c => TERMINAL_OTHER.has(c.conclusion)).length,
    head: evidence.head, observedAt: evidence.observedAt,
    workflowsObservedAt: evidence.runs.observedAt, complete, fresh: isFresh,
    error: record.error || evidence.runs.error || null,
  };
}

export function classify(item) {
  if (item.kind === "pr" && item.state === "MERGED") return { queue: "completed", disposition: "PR merged on GitHub; issue scope is separate." };
  if (item.state === "CLOSED") return { queue: "deferred", disposition: item.stateReason === "COMPLETED" ? "Closed as completed on GitHub; whole-scope delivery is not independently verified." : "Closed without verified delivery." };
  if (item.labels.includes("status/deferred")) return { queue: "deferred", disposition: "Deferred label observed; no execution authority implied." };
  if (item.execution?.verified && item.execution.state === "waiting-human") return { queue: "needs-human", disposition: "Verified execution is paused at a human boundary." };
  if (item.evidence?.fresh && item.evidence.actionRequired) return { queue: "needs-human", disposition: "GitHub explicitly reports action required." };
  if (item.execution?.verified && item.execution.state === "active" && item.authority?.verified) return { queue: "active", disposition: "Verified active execution within a confirmed mandate." };
  if (item.authority?.verified && item.execution?.verified && item.execution.state === "waiting-capacity") return { queue: "authorized", disposition: "Confirmed mandate; verified capacity wait." };
  if (item.evidence?.fresh && item.evidence.pending) return { queue: "waiting", disposition: "Waiting for GitHub checks or workflows." };
  if (item.kind === "pr" && item.reviewDecision === "CHANGES_REQUESTED") return { queue: "waiting", disposition: "GitHub review requests changes; contributor follow-up needed." };
  if (item.kind === "pr" && item.isDraft) return { queue: "waiting", disposition: "Contributor PR is still a draft." };
  if (item.authority?.verified && item.acceptance?.verified && item.evidence?.fresh && item.evidence.complete && item.acceptance.head === item.head) return { queue: "review", disposition: "Confirmed acceptance evidence matches the current head." };
  return { queue: "preparing", disposition: "Preparing evidence; execution and implementation authority are unknown." };
}

function textSummary(body, title) {
  const text = String(body || "").replace(/<!--[\s\S]*?-->/g, "").replace(/```[\s\S]*?```/g, "").replace(/!\[[^\]]*\]\([^)]*\)/g, "");
  const paragraphs = text.split(/\n\s*\n/).map(p => p.replace(/^#+\s*/gm, "").replace(/\*\*/g, "").trim());
  return paragraphs.find(p => p.length > 65)?.slice(0, 650) || title;
}

export function relationships(rawPrs, details = {}) {
  const links = new Map();
  const add = (issue, pr, kind, source) => {
    if (!Number.isSafeInteger(issue) || issue < 1 || issue === pr) return;
    const key = `${issue}:${pr}`;
    const ranks = { mentioned: 0, reference: 1, "cross-reference": 2, closing: 3 };
    if (!links.has(key) || ranks[kind] > ranks[links.get(key).kind]) links.set(key, { issue, pr, kind, source: safeUrl(source) });
  };
  for (const pr of rawPrs) {
    for (const issue of nodes(pr.closingIssuesReferences)) if (issue.repository?.nameWithOwner === REPO) add(issue.number, pr.number, "closing", pr.url);
    const body = String(pr.body || "").replace(/```[\s\S]*?```/g, "").replace(/<!--[\s\S]*?-->/g, "");
    for (const match of body.matchAll(/https:\/\/github\.com\/microsoft\/apm\/issues\/(\d+)/g)) add(Number(match[1]), pr.number, "reference", pr.url);
    const withoutForeign = body.replace(/(?:https:\/\/github\.com\/[^\s)]+|[\w.-]+\/[\w.-]+#\d+)/g, "");
    for (const match of withoutForeign.matchAll(/(?:^|[\s(])#(\d+)\b/g)) add(Number(match[1]), pr.number, "mentioned", pr.url);
    for (const match of withoutForeign.matchAll(/\b(?:refs?|references?|issue|partial(?:\s+(?:fix|work))?(?:\s+for)?|part\s+of|related\s+to|fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s*#(\d+)\b/gi)) {
      // Only GitHub's closingIssuesReferences can establish a closing relationship.
      add(Number(match[1]), pr.number, "reference", pr.url);
    }
  }
  for (const detail of Object.values(details)) {
    for (const ref of detail.data?.timelineReferences || []) {
      if (safeUrl(ref.url)?.startsWith(`${GH}/pull/`)) add(detail.data.number, ref.number, "cross-reference", ref.url);
    }
  }
  return [...links.values()];
}

export function buildModel(snapshot = {}, now = Date.now()) {
  const records = new Map();
  const placements = new Map();
  let inaccessible = 0;
  let otherRepos = 0;
  const add = (kind, raw, observedAt) => {
    if (!raw || raw.repository?.nameWithOwner !== REPO) return;
    records.set(itemId(kind, raw.number), { kind, raw, observedAt });
  };
  for (const node of snapshot.roadmap?.nodes || []) {
    const content = node.content;
    if (!content || !["Issue", "PullRequest"].includes(content.__typename)) { inaccessible++; continue; }
    if (content.repository?.nameWithOwner !== REPO) { otherRepos++; continue; }
    const kind = content.__typename === "Issue" ? "issue" : "pr";
    add(kind, content, snapshot.sources?.roadmap?.observedAt);
    const values = Object.fromEntries(nodes(node.fieldValues).filter(v => v.field?.name).map(v => [v.field.name, v.name]));
    placements.set(itemId(kind, content.number), {
      horizon: ["Now", "Next", "Later"].includes(values.Horizon) ? values.Horizon : "Unset",
      rawHorizon: values.Horizon || null, status: values.Status || null,
      itemUpdatedAt: node.updatedAt, archived: node.isArchived, id: node.id,
      fieldsComplete: !node.fieldValues?.pageInfo?.hasNextPage,
    });
  }
  for (const issue of snapshot.issues?.nodes || []) add("issue", issue, snapshot.sources?.issues?.observedAt);
  for (const pr of snapshot.prs?.nodes || []) add("pr", pr, snapshot.sources?.prs?.observedAt);
  for (const record of Object.values(snapshot.details || {})) {
    const data = record.data;
    if (!data) continue;
    const key = itemId("issue", data.number);
    const prior = records.get(key);
    const version = Date.parse(data.updatedAt);
    const priorVersion = Date.parse(prior?.raw.updatedAt);
    if (!prior || version > priorVersion || (version === priorVersion &&
        Date.parse(record.observedAt) >= Date.parse(prior.observedAt))) add("issue", data, record.observedAt);
  }
  for (const [number, record] of Object.entries(snapshot.pulls || {})) {
    if (!record.data) continue;
    const key = itemId("pr", number);
    const prior = records.get(key);
    if (!prior || (record.status === "current" &&
        Date.parse(record.observedAt) >= Date.parse(prior.observedAt) &&
        Date.parse(record.data.updatedAt) >= Date.parse(prior.raw.updatedAt))) {
      add("pr", record.data, record.observedAt);
    }
  }
  const rawPrs = [...records.values()].filter(r => r.kind === "pr").map(r => r.raw);
  const links = relationships(rawPrs, snapshot.details);
  const items = [...records.entries()].map(([id, { kind, raw, observedAt }]) => {
    const placement = placements.get(id);
    const evidence = kind === "pr" ? evidenceFor(raw, snapshot.pulls?.[raw.number], now) : null;
    const item = {
      id, kind, number: raw.number, title: raw.title, url: safeUrl(raw.url), state: raw.state,
      stateReason: raw.stateReason || null, summary: textSummary(raw.body, raw.title),
      author: raw.author?.login || "Unknown", assignees: nodes(raw.assignees).map(a => a.login),
      labels: nodes(raw.labels).map(l => l.name), updatedAt: raw.updatedAt, observedAt,
      metadataFresh: fresh(observedAt, now), horizon: placement?.horizon || "Unset",
      placement: placement || null, boardKnown: Boolean(snapshot.roadmap?.complete),
      requestedReviewers: nodes(raw.reviewRequests).map(r => r.requestedReviewer?.login || r.requestedReviewer?.name || "Unknown reviewer"),
      requestedReviewerLogins: nodes(raw.reviewRequests).filter(r => !r.requestedReviewer?.__typename || r.requestedReviewer.__typename === "User").map(r => r.requestedReviewer?.login).filter(Boolean),
      reviewDecision: raw.reviewDecision || "UNKNOWN", head: raw.headRefOid || null,
      mergedAt: raw.mergedAt || null, mergedBy: raw.mergedBy?.login || null,
      mergeable: raw.mergeable || "UNKNOWN", mergeState: raw.mergeStateStatus || "UNKNOWN",
      autoMerge: kind === "pr" ? (Object.hasOwn(raw, "autoMergeRequest") ? (raw.autoMergeRequest ? "ON" : "OFF") : "UNKNOWN") : null,
      isDraft: Boolean(raw.isDraft), evidence,
      history: (snapshot.pulls?.[raw.number]?.history || []).map(e => ({
        head: e.head, observedAt: e.observedAt,
        outcomes: e.checks.nodes.map(c => ({ name: c.name || c.context, conclusion: c.conclusion || c.state, url: safeUrl(c.detailsUrl || c.targetUrl) })),
      })),
      authority: { verified: false, status: "Unknown - no authority evaluator connected" },
      execution: { verified: false, state: "unknown", name: "Unknown - no verified execution feed" },
      fieldCoverage: !["labels", "assignees", "reviewRequests", "closingIssuesReferences"].some(k => raw[k]?.pageInfo?.hasNextPage),
      related: links.filter(l => kind === "issue" ? l.issue === raw.number : l.pr === raw.number),
    };
    return { ...item, ...classify(item) };
  });
  const byId = new Map(items.map(i => [i.id, i]));
  for (const item of items.filter(i => i.kind === "issue" && i.state === "OPEN")) {
    const linked = item.related.map(l => byId.get(itemId("pr", l.pr))).filter(Boolean);
    // Delivery remains issue-scoped, but a linked PR can explain a real wait/boundary.
    const boundary = linked.find(pr => pr.queue === "needs-human");
    const wait = linked.find(pr => pr.queue === "waiting");
    if (item.queue !== "deferred" && (boundary || wait)) {
      item.queue = boundary ? "needs-human" : "waiting";
      item.disposition = `Linked PR #${(boundary || wait).number}: ${(boundary || wait).disposition}`;
    }
    item.linkedState = linked.map(pr => `#${pr.number} ${pr.state}${pr.evidence?.status ? ` / ${pr.evidence.status}${pr.evidence.fresh ? "" : " (stale or unverified)"}` : ""}`).join("; ") || "No linked PR observed";
  }
  for (const item of items) {
    item.displayState = stateAppearance(item);
    const linked = item.kind === "issue" ? item.related.map(ref => items.find(pr => pr.id === itemId("pr", ref.pr))).filter(Boolean) : [];
    item.lifecycle = lifecycleFor(item, snapshot, linked, snapshot.runFeed?.runs || []);
    item.attention = attentionFor(item, snapshot);
    if (item.kind === "issue") {
      item.authority = { verified: item.lifecycle.current && item.lifecycle.accepted, status: item.lifecycle.current ? item.lifecycle.record?.acceptance?.state || "History not reconciled" : "Unknown - decision evidence stale or unavailable",
        authorizes_implementation: false };
    }
    if (item.lifecycle.runs.length) item.execution = { verified: true, state: item.lifecycle.runs[0].state, name: item.lifecycle.runStatus };
    if (item.state === "OPEN") {
      if (item.lifecycle.needsDecision) Object.assign(item, { queue: "needs-human", disposition: "Observed unresolved decision or mapped human boundary." });
      else if (item.lifecycle.runs.some(r => r.state === "running")) Object.assign(item, { queue: "active", disposition: "Parent tool reports an executing bounded run; this is not new permission." });
      else if (item.lifecycle.queuedRequests.length || item.lifecycle.runs.some(r => r.state === "queued" && r.kind !== "triage")) Object.assign(item, { queue: "authorized", disposition: "Host-observed bounded request is queued; no executing agent inferred." });
    }
    item.primaryAction = primaryAction(item, linked, snapshot.workflowApprovals || []);
    item.primaryActions = Object.fromEntries(["decisions", "permissions", "reviews", "review-followup"].map(scope =>
      [scope, primaryAction(item, linked, snapshot.workflowApprovals || [], scope)]));
    item.nextAction = item.primaryAction.label;
    if (item.kind === "pr") item.linkedState = item.related.length ? item.related.map(ref => `Issue #${ref.issue} (${ref.kind === "closing" ? "closing reference" : "partial/reference"})`).join("; ") : "No linked issue observed";
  }
  const work = projectView(items, defaultView()).visible;
  const sources = Object.fromEntries(Object.entries(snapshot.sources || {}).map(([key, value]) => [key, {
    ...value, status: value.status === "current" && !fresh(value.observedAt, now) ? "stale" : value.status,
  }]));
  return {
    repo: REPO, items, workIds: work.map(i => i.id), project: snapshot.roadmap?.project || null,
    lifecycle: snapshot.lifecycle || null, runFeed: snapshot.runFeed?.feed || null,
    counts: Object.fromEntries(QUEUES.map(([key]) => [key, work.filter(i => i.queue === key).length])),
    sources, attemptedAt: snapshot.attemptedAt || null, fetchedAt: snapshot.fetchedAt || null,
    coverage: { inaccessible, otherRepos, projectComplete: snapshot.roadmap?.complete === true,
      pullDetails: Object.values(snapshot.pulls || {}).reduce((counts, record) => {
        const status = record.status === "current" && !fresh(record.observedAt, now) ? "stale" : record.status;
        counts[status] = (counts[status] || 0) + 1;
        return counts;
      }, {}),
      relationships: "Closing links and explicit PR-body references loaded. Selected issue timelines are expanded on selection. Mentions do not prove scope; relationship discovery is not globally complete." },
    limitation: "Canonical scope records are evidence, not run permission. Run status is unavailable unless the parent reports mapped host observations. Labels, PRs and Horizon are never implementation authority.",
  };
}

const GUIDES = {
  3015: {
    problem: "Removing a skill from the manifest can remove its deployed copy but leave the downloaded source in apm_modules. The cleanup command can then say there is nothing to remove.",
    example: "Install a skill, delete its entry from apm.yml, run apm install and apm prune. The reporter still found its old package directory.",
    why: "Unused sources remain on disk and the cleanup message no longer matches what is there.",
    tradeoff: "Broader cleanup removes leftovers, but must preserve retained packages, nested contents and containing ancestors.",
    diagram: ["Remove manifest entry", "Refresh deployed files", "Prune unused managed roots"],
  },
  2881: {
    problem: "An instruction can contain a link that works in its source folder but breaks after APM folds it into CLAUDE.md.",
    example: "A link to ../context/conventions.context.md is copied to the project root unchanged. It then points outside the folder that contains the context.",
    why: "Claude can lose the supporting guidance even though compilation finishes successfully.",
    tradeoff: "Rewrite links for their output location while preserving the existing content and target-specific behavior.",
    diagram: ["Instruction source link", "Fold into CLAUDE.md", "Resolve from output location"],
  },
  2928: {
    problem: "Installing a tagged package from a marketplace can look for the version tag in the marketplace repository instead of the package repository.",
    example: "apm install apm-sample-package@sample-marketplace#v1.0.1 can reject a tag that exists on the package's own remote.",
    why: "A published package version becomes unavailable even though the tag is present.",
    tradeoff: "Use the package's remote for version lookup without changing marketplace selection or unrelated reference resolution.",
    diagram: ["Marketplace entry", "Package remote", "Version tag on that remote"],
  },
  3002: {
    problem: "Generated Cursor rules use a quoted YAML list for file patterns rather than Cursor's documented comma-separated form.",
    example: "Two applyTo patterns become separate list entries. Cursor expects a globs value such as services/**/*.py, plugins/**/*.py.",
    why: "Rules can be attached to files incorrectly even though install and compile finish without an error.",
    tradeoff: "Match Cursor's native shape without changing the different list format needed by Claude.",
    diagram: ["Two source patterns", "Cursor formatter", "One comma-separated globs value"],
  },
  2991: {
    problem: "Some generated MCP configurations copy a secret's current value to disk instead of keeping a runtime reference. The reported issue also covers shared GitHub authorization behavior, not only Cursor.",
    example: "A manifest says to read a secret from the environment. After installation the project-local Cursor configuration can contain the value itself, which could be committed.",
    why: "Generated configuration can expose credentials. A Cursor-only PR does not resolve the separate shared-authentication part.",
    tradeoff: "Preserve runtime references and explicit headers while maintaining static values and documented compatibility behavior. Existing files need deliberate repair, not an unapproved bulk rewrite.",
    diagram: ["Manifest secret reference", "Generated runtime reference", "Client reads secret at runtime"],
  },
};

export function recommendation(item, linked) {
  const action = item.primaryAction || primaryAction(item, linked);
  return { title: action.label, whyNow: action.status, do: action.consequence,
    consequence: action.warning || null,
    tradeoff: "Do not repeat scope approval because evidence is missing. Observed checks do not establish full acceptance or merge readiness." };
}

export function makeBrief(item, model, snapshot) {
  const linked = item.kind === "issue"
    ? item.related.map(l => model.items.find(i => i.id === itemId("pr", l.pr))).filter(Boolean) : [];
  const issueNumbers = item.kind === "issue" ? [item.number] : item.related.map(l => l.issue);
  const guideNumber = issueNumbers.find(n => GUIDES[n]);
  const guide = GUIDES[guideNumber];
  const raw = item.kind === "issue" ? snapshot.details?.[item.number]?.data : snapshot.pulls?.[item.number]?.data;
  const scope = (raw?.discussion || []).filter(c => c.body?.includes("apm-scope:")).map(c => ({
    author: c.author, body: c.body, url: safeUrl(c.url), updatedAt: c.updatedAt,
    label: `Scope record by ${c.author || "unknown author"} (observed, not authority-validated)`,
  }));
  const advice = (raw?.discussion || []).filter(c => /advisory|recommendation/i.test(c.body) && !c.body.includes("apm-scope:")).map(c => ({
    author: c.author, body: c.body, url: safeUrl(c.url), updatedAt: c.updatedAt,
  }));
  const rec = recommendation(item, linked);
  const sources = [
    { label: `${item.kind === "issue" ? "Issue" : "Pull request"} #${item.number}: ${item.title}`, url: item.url },
    ...(item.kind === "pr" ? [{ label: `PR #${item.number} diff`, url: `${item.url}/files` }, { label: `PR #${item.number} checks`, url: `${item.url}/checks` }] : []),
    ...linked.flatMap(pr => [{ label: `PR #${pr.number}: ${pr.title}`, url: pr.url }, { label: `PR #${pr.number} diff`, url: `${pr.url}/files` }, { label: `PR #${pr.number} checks`, url: `${pr.url}/checks` }]),
    ...scope.map(s => ({ label: s.label, url: s.url })),
    { label: "Contribution rules (current default branch)", url: `${GH}/blob/main/CONTRIBUTING.md` },
    { label: "APM documentation source", url: `${GH}/tree/main/docs/src/content/docs` },
    ...(model.project ? [{ label: "APM Roadmap", url: model.project.url }] : []),
  ];
  return {
    id: item.id, generatedAt: new Date().toISOString(), kind: guide ? "Source-grounded editorial explanation" : "Source excerpt; editorial brief not yet prepared",
    problem: guide?.problem || item.summary,
    example: guide?.example || "A specific reproducible example has not been independently summarized for this item. Read the source excerpt below; no scenario has been invented.",
    why: guide?.why || "The issue or PR describes the reported impact. Its priority and scope must be read from the source, not inferred from labels.",
    explanationSource: guideNumber ? `${GH}/issues/${guideNumber}` : item.url,
    recommendation: rec, mainTradeoff: rec.tradeoff, scopeTradeoff: guide?.tradeoff || null,
    alternatives: [
      "Inspect the issue, diff and evidence before forming a judgment; this takes more time but reduces uncertainty.",
      "Ask the existing contributor for the specific missing explanation outside this canvas; preserve their ownership.",
      "Defer the decision without claiming delivery or starting another implementation.",
    ],
    wouldDo: "Source links navigate; updates read GitHub. Workflow permission has a separate exact account, commit and run preview plus explicit confirmation. Other operational requests reach the parent and retain real host gates and verified receipts.",
    wouldNotDo: "Reading or selecting never changes GitHub. Acceptance never starts delivery. Workflow permission does not approve a code review, merge, enable auto-merge or guarantee passing CI. Other agent and repository operations retain their existing parent gates.",
    scope, advice, sources: sources.filter(s => safeUrl(s.url)), diagram: guide?.diagram || null,
    acceptance: "Acceptance criteria are not independently verified. Check failures are observed outcomes, not a diagnosis. Optional advisory improvements are not automatically acceptance failures.",
    driverEvidence: item.lifecycle?.runs.length ? `Mapped host observations: ${item.lifecycle.runStatus}. These are not GitHub check or acceptance evidence.` : "Run status unavailable. No local test or agent completion claim is presented as GitHub evidence.",
    sourceExcerpt: raw?.body || item.summary, sourceUpdatedAt: item.updatedAt,
  };
}
