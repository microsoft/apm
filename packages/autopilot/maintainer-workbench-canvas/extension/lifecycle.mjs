import { TTL } from "./config.mjs";

export function lifecycleFor(item, snapshot, linked = [], runs = []) {
  const record = item.kind === "issue" ? snapshot.lifecycle?.issues?.[item.number] : null;
  const observed = Boolean(record?.complete && record.updatedAt === item.updatedAt && Number.isFinite(Date.parse(record.observedAt)));
  const current = observed && snapshot.lifecycle?.status === "current" && record.status === "current" &&
    Date.now() - Date.parse(record.observedAt) < TTL;
  const accepted = Boolean(observed && record.acceptance?.state === "record-present");
  const deferred = Boolean(observed && record.disposition?.kind === "deferred");
  const actor = snapshot.lifecycle?.actor;
  const linkedTargets = new Set([item.id, ...linked.map(p => p.id)]);
  const observedRuns = runs.filter(r => r.current !== false && r.targetIds.some(id => linkedTargets.has(id)) && Date.now() - Date.parse(r.observedAt) < TTL);
  const humanRun = observedRuns.find(r => ["waiting-human", "plan-required"].includes(r.state));
  const assessing = observedRuns.some(r => r.kind === "triage" && r.state === "running");
  const queuedRequests = (snapshot.bridgeRequests || []).filter(r => r.status === "queued" && r.gate &&
    ["implement", "review", "recover", "resume"].includes(r.kind) && r.lockedIds.some(id => linkedTargets.has(id)) &&
    r.receipts.some(receipt => ["capacity-observed", "session-observed"].includes(receipt.step) && receipt.outcome === "verified") &&
    Date.now() - Date.parse(r.updatedAt) < TTL);
  const openPrs = linked.filter(p => p.state === "OPEN");
  const permission = openPrs.some(p => p.evidence?.actionRequired);
  const pendingAdvice = observed && record.recommendation?.status === "unresolved" && actor && record.responsibleActor === actor.login;
  const scopeDecision = item.kind === "issue" && item.state === "OPEN" && Boolean(pendingAdvice);
  const humanAttention = item.state === "OPEN" && Boolean(humanRun && humanRun.actor === actor?.login);
  const needsDecision = item.state === "OPEN" && Boolean(pendingAdvice || (humanRun && humanRun.actor === actor?.login) || (permission && actor?.responsible));
  const planned = Boolean(item.placement && item.placement.archived === false && ["Now", "Next", "Later"].includes(item.horizon));
  let stage = item.kind === "pr" ? "Contribution admission" : !observed ? "Checking decision history" : "Not assessed";
  if (observed && record.labelMismatch) stage = "Recorded status needs reconciliation";
  if (observed && record.followup) stage = "Human discussion to reconcile";
  if (record?.recommendation && observed) stage = pendingAdvice ? "Recommendation ready" : "Recommendation retained as context";
  if (accepted) stage = planned ? `Planned: ${item.horizon}` : "Accepted, not scheduled";
  if (deferred) stage = "Deferred";
  if (observed && record.design) stage = "Design needed";
  if (openPrs.length) stage = "Contributor delivery";
  if (observedRuns.some(r => r.kind === "triage" && r.state === "queued")) stage = "Assessment queued";
  if (assessing) stage = "Agent assessing";
  if (queuedRequests.length || observedRuns.some(r => r.state === "queued" && r.kind !== "triage")) stage = "Queued to start";
  if (observedRuns.some(r => r.state === "running" && r.kind !== "triage")) stage = "In delivery";
  if (humanRun) stage = humanRun.state === "plan-required" ? "Plan approval needed" : "Human decision needed";
  if (permission) stage = "Workflow permission needs review";
  if (item.state !== "OPEN") stage = item.displayState?.label || "Closed";
  return { stage, current, observed, lastKnown: observed && !current, accepted, deferred, planned, needsDecision, scopeDecision, humanAttention,
    scopeReason: scopeDecision ? "A scope recommendation needs the responsible maintainer's judgment." : null,
    humanReason: humanAttention ? humanRun.blocker || (humanRun.state === "plan-required" ? "Existing work needs plan approval in its original conversation." : "Existing work is waiting for maintainer input.") : null,
    assessing, design: observed && Boolean(record.design),
    triage: item.state === "OPEN" && !accepted && !deferred,
    roadmap: item.state === "OPEN" && item.placement?.archived !== true && (accepted || planned),
    delivery: item.state === "OPEN" && Boolean(openPrs.length || observedRuns.length || queuedRequests.length),
    plannedUnverified: planned && !accepted, record: record || null, runs: observedRuns, queuedRequests: queuedRequests.map(r => r.id),
    runStatus: observedRuns.length ? observedRuns.map(r => `${r.name}: ${r.state}`).join("; ") :
      queuedRequests.length ? "Host-confirmed request waiting for capacity; no executing agent observed" : "Run status unavailable",
    explanation: item.kind === "pr" ? "PR admission and review eligibility remain with the existing workflow. Linked issue scope is separate; PR metadata alone is not approval." :
      !current ? [record?.error || snapshot.lifecycle?.error,
        observed ? `Last-known decision history from ${record.observedAt}. It remains visible, not current authorization. Select the issue to recheck its discussion before an action.`
          : "Complete decision history is being reconciled; labels do not establish acceptance."].filter(Boolean).join(" ")
      : record.followup || (record.labelMismatch ? "The observed label has no matching current human record. Inspect existing history; do not request approval again by default."
        : accepted ? "Canonical human scope record observed. A fresh bounded run mandate is still separate."
          : deferred ? "Human deferral observed; scope approval history is preserved." : "Discussion and human decision history reconciled."),
  };
}

export function attentionFor(item, snapshot) {
  if (item.kind !== "pr" || item.state !== "OPEN") return null;
  const actor = snapshot.lifecycle?.actor;
  const identityKnown = Boolean(actor?.login && actor.type === "User");
  const selfAuthored = identityKnown && item.author.toLowerCase() === actor.login.toLowerCase();
  const requested = identityKnown && item.requestedReviewerLogins.some(login => login.toLowerCase() === actor.login.toLowerCase());
  const maintainer = identityKnown && actor.responsible === true;
  const ownershipCurrent = identityKnown && Date.now() - Date.parse(actor.observedAt) < TTL;
  const reviewRole = identityKnown && !selfAuthored && item.author !== "Unknown" && (requested || maintainer);
  let reviewState = "needs-review";
  let reviewReason = requested ? `Review requested from ${actor.login}.` :
    maintainer ? "Available for maintainer review; no personal reviewer assignment is inferred." :
      "Review ownership is not established for the current account.";
  if (selfAuthored) { reviewState = "own-contribution"; reviewReason = "Your contribution needs review from someone else."; }
  else if (item.isDraft) { reviewState = "draft"; reviewReason = "Draft: waiting for the contributor to mark it ready for review."; }
  else if (item.reviewDecision === "CHANGES_REQUESTED") { reviewState = "changes-requested"; reviewReason = "Changes requested: contributor follow-up is recorded. Inspect later revisions without assuming it is resolved."; }
  else if (item.reviewDecision === "APPROVED") { reviewState = "review-recorded"; reviewReason = "GitHub reports approval already recorded. This does not establish merge readiness."; }
  else if (!reviewRole) reviewState = "ownership-unknown";
  if (!item.metadataFresh || identityKnown && !ownershipCurrent) reviewReason += " Last-known metadata or account role; GitHub opens the current source.";
  const approvals = (snapshot.workflowApprovals || []).filter(record => record.targetId === item.id && record.head === item.head);
  const approvalPending = approvals.some(record => ["approving", "uncertain"].includes(record.status));
  const gates = item.evidence?.runs.filter(run => run.conclusion === "action_required") || [];
  const uncovered = gates.filter(run => !approvals.some(record =>
    record.runs?.some(saved => saved.id === run.id && saved.attempt === run.attempt) &&
    record.receipts?.some(receipt => receipt.runId === run.id && ["approved", "already-started", "already-completed"].includes(receipt.outcome))));
  const permission = Boolean(item.evidence?.actionRequired && maintainer && actor.canWrite === true &&
    !approvalPending && (!gates.length || uncovered.length));
  const gateCount = uncovered.length;
  return {
    permission, permissionReason: permission ? `${gateCount ? `${gateCount} observed workflow${gateCount === 1 ? "" : "s"} need` : "GitHub checks report"} a permission decision.${item.evidence?.fresh ? "" : " Last known; refresh before acting."}` : null,
    review: reviewRole && reviewState === "needs-review", reviewState, reviewReason,
    requestedFromActor: Boolean(requested), actor: actor?.login || null,
    ownershipCurrent,
    reviewFollowup: !(reviewRole && reviewState === "needs-review"),
  };
}
