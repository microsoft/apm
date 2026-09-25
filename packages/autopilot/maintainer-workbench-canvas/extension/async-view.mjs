const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const explanationPending = value => ["sending", "awaiting-agent", "generating"].includes(value);
export const approvalPending = value => ["approving", "uncertain"].includes(value);
export const approvalNeedsObservation = r => approvalPending(r.status) || Boolean(r.observationError) || r.receipts?.some(receipt => ["approved", "uncertain"].includes(receipt.outcome)) &&
  (!r.observedRuns?.length || r.observedRuns.some(run => run.status !== "completed" || run.conclusion === "action_required"));
export function pollDelay(state) {
  const selected = state?.items?.find(item => item.id === state?.view?.selectedId);
  const linked = selected?.kind === "pr" ? [selected] : state?.items?.filter(item => selected?.related?.some(ref => ref.pr === item.number && item.kind === "pr")) || [];
  return state?.refreshing || state?.detailLoading || state?.checksRead?.status === "reading" ||
    explanationPending(state?.explanation?.status) || state?.explanation?.activeCount > 0 || linked.some(pr => pr.evidence?.pending || pr.evidence?.actionRequired) ||
    state?.workflowApprovals?.requests?.some(approvalNeedsObservation) ||
    state?.bridge?.requests?.some(r => !["preview", "completed", "failed", "cancelled", "blocked"].includes(r.status)) ? 2000 : 10000;
}
export function elapsedHtml(startedAt) {
  return startedAt ? `<span class="elapsed" data-elapsed="${esc(startedAt)}">Just started</span>` : "";
}
export function selectedProgress(state) {
  const progress = state.selectedRead;
  if (!progress || progress.id !== state.view.selectedId || !["sending", "queued", "reading", "error"].includes(progress.status)) return "";
  return `<section class="selected-progress${progress.status === "error" ? " failed" : ""}" role="status" aria-live="polite">
    <strong>${progress.status === "error" ? "Could not update this item" : "Updating this item from GitHub"}</strong>
    <p>${esc(progress.error || progress.message)} ${progress.status !== "error" ? elapsedHtml(progress.startedAt) : ""}</p>
    ${progress.status === "error" ? '<button type="button" data-selected-retry>Retry this item</button>' : '<p class="slow-guidance" data-slow hidden>GitHub is taking longer than usual. Cached details remain available; you can choose another issue.</p>'}</section>`;
}
export function explanationProgress(context, error) {
  const status = context?.status;
  const labels = { sending: "Sending explanation request", "awaiting-agent": "Waiting for background agent", generating: "Generating in background",
    failed: "Explanation failed", uncertain: "Explanation delivery is uncertain", cancelled: "Stopped waiting", superseded: "Sources changed" };
  const detail = { sending: "Sending a bounded source packet. Delivery is not completion.",
    "awaiting-agent": "The request was delivered. A background agent will read and claim this item's source packet.",
    generating: "A background agent claimed this item and is preparing its source-grounded explanation. Other items can generate independently.",
    uncertain: "Delivery or generation could not be verified. Nothing will be sent again automatically.",
    cancelled: "This explanation will no longer be accepted. No issue or implementation was changed." };
  return `<strong>${esc(labels[status] || (context?.busy ? "Explanation request capacity reached" : "Plain-English explanation not prepared"))}</strong>
    <p>${esc(error || detail[status] || (context?.available ? "The current status, source and actions remain available." : "The explanation connection is unavailable. Current status and source links remain available."))}</p>
    ${explanationPending(status) ? `<p>${elapsedHtml(context.createdAt || context.updatedAt)} <span class="muted">No automatic resend.</span></p>
      <p class="slow-guidance" data-slow hidden>The background explanation has not finished. Keep browsing or prepare context for another item; this request is not resent automatically. You can stop this explanation below.</p>` : ""}
    ${context?.busy ? "<p>All saved request slots are active. Wait for a result or explicitly stop a request before starting another.</p>" : ""}`;
}
export function workflowSummary(pr, link, date) {
  const e = pr.evidence;
  if (!e?.runs?.length) return `<p class="check-timing">Workflows not observed. Checks last read ${date(e?.observedAt)}.</p>`;
  return `<ul class="compact-runs">${e.runs.map(run => {
    const label = run.conclusion === "action_required" ? "Needs permission" : run.status === "completed" ?
      `Completed: ${(run.conclusion || "outcome unknown").replaceAll("_", " ")}` : ["in_progress", "waiting", "pending"].includes(run.status) ?
      (run.status === "in_progress" ? "Running" : "Waiting") : run.status === "queued" ? "Queued" : run.status || "Unknown";
    return `<li>${link(run.name, run.url)} <span>${esc(label)}${run.attempt ? ` / attempt ${run.attempt}` : ""}</span></li>`;
  }).join("")}</ul><p class="check-timing">Workflows checked ${date(e.workflowsObservedAt || e.observedAt)}${!e.fresh ? " (last known, needs updating)" : ""}.</p>`;
}
export function approvalFor(state) {
  const item = state.items.find(i => i.id === state.view.selectedId);
  const targets = new Set(item ? [item.id, ...(item.related || []).map(r => `microsoft/apm/pr/${r.pr}`)] : []);
  const local = targets.has(state.approvalUi?.targetId) ? state.approvalUi : null;
  const record = local?.record || [...(state.workflowApprovals?.requests || [])].filter(r => targets.has(r.targetId)).sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))[0];
  return { local, record };
}
export function workflowApprovalHtml(state, link, date) {
  const { local, record } = approvalFor(state);
  if (!local && !record) return "";
  const target = state.items.find(i => i.id === (local?.targetId || record?.targetId));
  const source = target ? link(`Open PR #${target.number} on GitHub`, target.url) : "";
  if (local?.loading) return `<section class="workflow-approval selected-progress" role="status"><h3>Checking workflow approval eligibility</h3><p>Reading the current account, PR commit and exact workflow runs. Nothing is being approved. ${elapsedHtml(local.startedAt)}</p>${source}</section>`;
  if (local?.error) return `<section class="workflow-approval" role="alert"><h3>Workflow approval could not proceed</h3><p class="failed">${esc(local.error)}</p>${source}<p>Do not assume approval or retry a submission with an uncertain outcome. The recorded status is kept below.</p>${record ? receiptHtml(record, link, date) : ""}</section>`;
  if (!record) return "";
  const expired = Date.parse(record.expiresAt) <= Date.now();
  return `<section class="workflow-approval" aria-label="Workflow permission approval">
    <h3>${record.status === "preview" ? "Confirm workflow permission" : "Workflow approval: " + esc(record.status)}</h3>
    <p>${esc(record.message || "")}</p>
    ${record.status === "preview" ? `<p>Signed in as <strong>${esc(record.actor)}</strong>. PR <strong>#${record.prNumber}</strong>, commit <code title="${esc(record.head)}">${esc(record.head?.slice(0, 12))}</code>.</p>
      <details data-disclosure="approval-commit"><summary>Full commit</summary><code class="full-sha">${esc(record.head)}</code></details>
      <ul class="approval-runs">${record.runs.map(run => `<li>${link(run.name, run.url)} / attempt ${esc(run.attempt)}</li>`).join("")}</ul>
      <p class="attention"><strong>This runs contributor code</strong> with the workflow's configured permissions. The workflow can deploy or publish if its configuration allows it.</p>
      ${record.autoMerge ? '<p class="attention"><strong>Auto-merge is ON.</strong> Successful checks may allow this PR to merge.</p>' : "<p>Auto-merge is off at this preview.</p>"}
      <p>This grants workflow permission only. It does not approve the code review or enable auto-merge.</p>
      <p class="check-timing">Preview expires ${date(record.expiresAt)}. Account, commit, run set and auto-merge are rechecked at confirmation.</p>
      ${expired ? '<p class="failed">This preview expired. Review a fresh preview before confirming.</p>' : `<label class="confirm-check"><input type="checkbox" data-approval-ack${local?.acknowledged ? " checked" : ""}${local?.submitting ? " disabled" : ""}> I reviewed the exact workflows and understand they will run contributor code.</label>
      <button type="button" class="primary-action" data-primary="true" data-confirm-workflows="${esc(record.id)}"${!local?.acknowledged || local?.submitting ? " disabled" : ""}>${local?.submitting ? "Submitting this confirmation..." : `Approve ${record.runs.length} workflows for PR #${record.prNumber}`}</button>`}
      <button type="button" data-close-workflow-preview>Close preview</button>` :
      `<p>${approvalPending(record.status) ? "Checking recorded outcomes; approvals are never automatically resent." : "Permission approval is not a passing CI result. Current workflow states are shown separately."}</p>${receiptHtml(record, link, date)}`}
    <p>${source}</p></section>`;
}
function receiptHtml(record, link, date) {
  return `${record.observationError ? `<p class="failed">${esc(record.observationError)} Last-known run observations below may be stale.</p>` : ""}<ul class="approval-receipts">${(record.receipts || []).map(r => `<li>${link(r.name, r.url)}: <strong>${esc(r.outcome)}</strong>. ${esc(r.message)} <span class="muted">${date(r.observedAt)}</span></li>`).join("")}</ul>
    ${(record.observedRuns || []).length ? `<h4>Observed after permission approval</h4><ul>${record.observedRuns.map(r => `<li>${link(r.name, r.url)}: ${esc(r.conclusion || r.status)} / attempt ${esc(r.attempt)}. Checked ${date(r.observedAt)}.</li>`).join("")}</ul>` : ""}`;
}
