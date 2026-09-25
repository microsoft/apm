export const VIEW_SCHEMA = 3;
export const HORIZONS = ["All", "Now", "Next", "Later", "Unset"];
export const ATTENTION_SCOPES = ["decisions", "permissions", "reviews"];
export const SCOPES = [
  ["decisions", "Scope decisions"], ["permissions", "Workflow permissions"], ["reviews", "PR reviews"],
  ["review-followup", "PR follow-up"], ["triage", "Triage"], ["roadmap", "Roadmap"], ["delivery", "Delivery"],
  ["deferred", "Deferred"], ["history", "History"],
  ["admission", "Contribution admission"],
  ["roadmap-open", "Open roadmap issues"],
  ["roadmap-history", "Roadmap history"],
  ["repository-open", "Open repository issues"],
  ["repository-history", "Repository issue history"],
  ["repository-prs", "Repository pull requests"],
];

export function defaultView() {
  return { selectedId: null, scope: "decisions", horizon: "All", queue: "all", search: "", person: "", area: "" };
}

export function inScope(item, scope) {
  if (scope === "permissions") return item.kind === "pr" && item.state === "OPEN" && Boolean(item.attention?.permission);
  if (scope === "reviews") return item.kind === "pr" && item.state === "OPEN" && Boolean(item.attention?.review);
  if (scope === "review-followup") return item.kind === "pr" && item.state === "OPEN" && Boolean(item.attention?.reviewFollowup);
  if (scope === "admission") return item.kind === "pr" && item.state === "OPEN" && !item.related.length;
  if (scope === "history") return item.kind === "issue" && (item.state !== "OPEN" || item.placement?.archived === true);
  if (scope === "delivery" && item.kind === "pr") return false;
  if (["decisions", "triage", "roadmap", "delivery", "deferred"].includes(scope)) {
    if (item.kind !== "issue" || item.state !== "OPEN") return false;
    return Boolean(item.lifecycle?.[{ decisions: "scopeDecision", triage: "triage", roadmap: "roadmap", delivery: "delivery", deferred: "deferred" }[scope]]);
  }
  if (scope === "repository-prs") return item.kind === "pr";
  if (item.kind !== "issue") return false;
  if (scope === "roadmap-open") return item.state === "OPEN" && item.placement?.archived === false;
  if (scope === "roadmap-history") return Boolean(item.placement) && (item.state !== "OPEN" || item.placement.archived === true);
  if (scope === "repository-open") return item.state === "OPEN";
  if (scope === "repository-history") return item.state !== "OPEN";
  return false;
}

export function projectView(items, view) {
  const scope = view.scope || "roadmap-open";
  const universe = items.filter(item => inScope(item, scope));
  const search = view.search.trim().toLowerCase().replace(/^#/, "");
  const matches = universe.filter(item => {
    if (view.queue !== "all" && item.queue !== view.queue) return false;
    if (view.area && !item.labels.includes(view.area)) return false;
    if (view.person && ![item.author, ...item.assignees, ...item.requestedReviewers].includes(view.person)) return false;
    return !search || `${item.number} ${item.title} ${item.labels.join(" ")} ${item.linkedState || ""}`.toLowerCase().includes(search);
  });
  const visible = matches.filter(item => view.horizon === "All" || item.horizon === view.horizon);
  const horizonOrder = { Now: 0, Next: 1, Later: 2, Unset: 3 };
  visible.sort((a, b) => horizonOrder[a.horizon] - horizonOrder[b.horizon] || Date.parse(b.updatedAt) - Date.parse(a.updatedAt) || b.number - a.number);
  const horizonCounts = Object.fromEntries(HORIZONS.map(horizon => [horizon, matches.filter(i => horizon === "All" || i.horizon === horizon).length]));
  const queueCounts = visible.reduce((counts, item) => ({ ...counts, [item.queue]: (counts[item.queue] || 0) + 1 }), {});
  return {
    scope, label: SCOPES.find(([key]) => key === scope)?.[1] || "Unknown scope",
    universe, matches, visible, horizonCounts, queueCounts,
    universeTotal: universe.length, filteredTotal: visible.length,
    kind: ["permissions", "reviews", "review-followup", "repository-prs", "admission"].includes(scope) ? "pull requests" : "issues",
    basis: {
      decisions: "Unique issues with a scope recommendation awaiting the responsible maintainer's judgment. Workflow permission is not scope acceptance. Last-known recommendations require a fresh read before acting.",
      permissions: "Unique open PRs with an observed workflow permission blocker for the maintainer account. Exact run eligibility is checked in the preview; a reported blocker alone does not authorize approval.",
      reviews: "Unique open PRs available for review by the observed maintainer or directly requested reviewer. CI permission and results do not block inspection. This does not assign you a review or establish merge readiness.",
      "review-followup": "Open drafts, requested changes, recorded approvals, own contributions and unverified review ownership. These are not counted as actionable PR reviews; inspect their recorded follow-up.",
      triage: "Issues being assessed or needing reconciled disposition. Old advisory markers do not undo human acceptance or deferral.",
      roadmap: "Observed accepted issues awaiting scheduling and planned Now/Next/Later issues. Last-known scope is rechecked before actions; planned work without verified scope stays visible as an exception.",
      delivery: "Unique issues with observed contributor PRs or mapped runs. Unlinked PR admission is shown separately, never added to this issue count.",
      deferred: "Human deferral observed. This does not close work, withdraw approval or automatically set Later.",
      history: "Closed or archived issues: completed, not planned, archived-open and unknown closure remain distinct. A merged partial PR alone does not complete an issue.",
      admission: "Open PRs without a loaded issue relationship. Admission is separate from the Roadmap issue count; missing links may still need discovery.",
    }[scope] || (scope === "roadmap-open"
      ? "Open issues with observed, nonarchived APM Roadmap membership. Closed issues, archived items and PRs do not count; linked PRs remain supporting context."
      : scope === "roadmap-history"
        ? "Observed Roadmap issues that are closed or archived. PRs are supporting context, not extra issues."
        : scope === "repository-prs"
          ? "Loaded repository PRs, including observed historical PRs. This is not the Roadmap issue count or a complete closed-PR archive."
          : scope === "repository-open"
            ? "Open repository issues, including any outside APM Roadmap. PRs are supporting context."
            : "Loaded closed repository issues. This is not a complete repository archive."),
    facetBasis: "Scope decisions count issues. Workflow permissions and PR reviews count unique PRs, including unlinked contributions. One PR may need both permission and review; do not add these counts. Multiple PRs for one issue stay distinct.",
  };
}

export function reconcileSelection(items, view, followingId = null) {
  const visible = projectView(items, view).visible;
  if (visible.some(item => item.id === view.selectedId)) return { view, notice: null };
  if (followingId === view.selectedId && items.some(item => item.id === followingId)) return {
    view, notice: "This item left the current queue; kept open while you follow its workflows. It is not included in the queue count.",
  };
  const selectedId = visible[0]?.id || null;
  if (view.selectedId === selectedId) return { view, notice: null };
  const prior = items.find(item => item.id === view.selectedId);
  return {
    view: { ...view, selectedId },
    notice: selectedId
      ? `${prior ? `${prior.kind === "pr" ? "PR" : "Issue"} #${prior.number} is outside the active scope or filters. ` : view.selectedId ? "The previous selection is not in this visible scope. " : ""}Selected ${visible[0].kind === "pr" ? "PR" : "issue"} #${visible[0].number}.`
      : "No matching issue or PR is available in this scope and filters. The previous item is not shown as current.",
  };
}

export function restoreView(saved, items, followingId = null) {
  const migrated = saved?.schema !== VIEW_SCHEMA;
  const input = migrated ? { ...defaultView(), selectedId: saved?.data?.selectedId || null } : { ...defaultView(), ...saved.data };
  const selected = items.find(item => item.id === input.selectedId);
  let attentionNotice = null;
  if (input.scope === "decisions" && selected && !inScope(selected, "decisions") && !followingId && selected.lifecycle?.delivery) {
    input.scope = "delivery";
    attentionNotice = "Attention is now split into scope decisions, workflow permissions and PR reviews. Your selected issue is kept open in Delivery.";
  }
  const result = reconcileSelection(items, input, followingId);
  return {
    ...result, migrated,
    notice: [migrated ? "View updated: Scope decisions is the default attention group. Triage, Roadmap and Delivery remain issue views." : null, attentionNotice, result.notice].filter(Boolean).join(" ") || null,
  };
}

export function contextualAction(item, scope) {
  return item.primaryActions?.[scope] || item.primaryAction;
}

export function applyAttentionView(model, view) {
  for (const item of model.items) {
    item.primaryAction = contextualAction(item, view.scope);
    item.nextAction = item.primaryAction.label;
  }
  return model;
}

export function attentionCounts(items) {
  return Object.fromEntries(ATTENTION_SCOPES.map(scope => [scope, new Set(items.filter(item => inScope(item, scope)).map(item => item.id)).size]));
}

export function attentionReason(item, scope) {
  if (scope === "decisions") return item.lifecycle.scopeReason;
  if (scope === "permissions") return item.attention?.permissionReason;
  if (["reviews", "review-followup"].includes(scope)) return item.attention?.reviewReason;
  return item.lifecycle.humanReason || item.lifecycle.stage;
}
