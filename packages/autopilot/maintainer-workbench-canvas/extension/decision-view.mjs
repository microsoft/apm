import { selectedProgress, explanationProgress, workflowSummary, workflowApprovalHtml, approvalFor } from "./async-view.mjs";
import { contextualAction } from "./scope.mjs";
export const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const preparationLabels = {
  triage: "Prepare agent assessment", "scope-draft": "Prepare scope decision", "scope-accept": "Prepare acceptance record",
  "scope-withdraw": "Prepare scope withdrawal", horizon: "Prepare scheduling decision", implement: "Prepare bounded implementation",
  defer: "Prepare deferral", design: "Prepare design question", decline: "Prepare decline decision",
  admission: "Prepare contribution assessment", review: "Prepare agent review", recover: "Prepare review or CI follow-up", resume: "Prepare continuation",
};
const button = (kind, id) => `<button type="button" data-action="${kind}" data-target="${escape(id)}">${preparationLabels[kind]}</button>`;

export function checkSummary(pr) {
  const e = pr.evidence;
  if (!e?.fresh) return "Current checks need refreshing";
  const counts = new Map();
  for (const c of e.checks) {
    const outcome = (c.conclusion || c.status).toLowerCase().replaceAll("_", " ");
    counts.set(outcome, (counts.get(outcome) || 0) + 1);
  }
  const blocked = e.runs.filter(r => r.conclusion === "action_required").length;
  return [counts.size ? [...counts].map(([name, count]) => `${count} ${name}`).join(", ") : "No check contexts returned",
    blocked ? `${blocked} workflow${blocked === 1 ? "" : "s"} awaiting permission` : "",
    !e.complete ? "partial coverage" : ""].filter(Boolean).join("; ");
}

function explanation(state, item, link) {
  const context = state.explanation;
  if (context?.status === "completed") {
    const r = context.result;
    return `<section class="plain-context"><h3>The problem</h3><p>${escape(r.problem)}</p>
      <div class="example"><h3>A concrete example <span class="muted">(${r.exampleKind === "reported" ? "reported, not independently tested" : "illustrative scenario"})</span></h3><p>${escape(r.example)}</p></div>
      <h3>Why it matters</h3><p>${escape(r.impact)}</p>
      ${r.tradeoff ? `<p class="context-tradeoff"><strong>Main trade-off:</strong> ${escape(r.tradeoff)}</p>` : ""}
      <details data-disclosure="context-sources"><summary>Explanation sources</summary><p class="muted">Agent-written explanation, not an approval or test result.</p><div class="links">${r.citations.map(url => link(context.sources.find(s => s.url === url)?.label || "Source", url)).join("")}</div></details></section>`;
  }
  const error = context?.error || state.explanationError;
  const pending = ["sending", "awaiting-agent", "generating"].includes(context?.status);
  const missing = context?.status === "missing";
  const ownPending = context?.pending?.filter(r => r.targetId === item.id) || [];
  const otherPending = context?.pending?.filter(r => r.targetId !== item.id) || [];
  return `<section class="explanation-state" aria-live="polite"><h3>Plain-English context</h3>
    ${explanationProgress(context, error)}
    <p>${link("Read the original issue or PR", item.url)}</p>
    ${context?.sourceChanged ? '<p class="attention">Sources changed while this explanation was pending. The background agent must use the latest claimed packet; an obsolete result will not replace current context.</p>' : ""}
    ${context?.available && !pending ? `<button data-explain="retry" type="button"${context.busy ? " disabled" : ""}>${missing ? "Prepare explanation" : "Retry explanation"}</button>` : ""}
    ${ownPending.length ? `<div class="context-recovery"><p>Stopping only discards this explanation, not implementation work.</p>${ownPending.map(r => `<button type="button" data-cancel-explanation="${r.id}">Stop this explanation</button>`).join("")}</div>` : ""}
    ${otherPending.length ? `<p class="muted">${otherPending.length} other explanation${otherPending.length === 1 ? "" : "s"} pending independently. Selecting another item does not cancel them.</p>` : ""}</section>`;
}

export function renderDecisionHtml(state, { link, stateLabel, evidence, people, date }) {
  const item = state.items.find(i => i.id === state.view.selectedId);
  if (!item) return '<h2>No matching item in this view</h2><p class="empty">Adjust the queue filters or refresh the sources.</p>';
  const brief = state.brief || { scope: [], advice: [], sources: [], sourceExcerpt: "Full discussion is loading. The source link remains available.",
    wouldDo: "Read this item's discussion and linked PRs.", wouldNotDo: "No issue or implementation changes are made by reading." };
  const a = contextualAction(item, state.view.scope), result = state.explanation?.status === "completed" ? state.explanation.result : null;
  const prs = item.kind === "pr" ? [item] : item.related.map(r => state.items.find(i => i.id === `microsoft/apm/pr/${r.pr}`)).filter(Boolean);
  const lc = item.lifecycle;
  const approval = approvalFor(state);
  const reviewing = ["reviews", "review-followup"].includes(state.view.scope);
  const showApproval = !reviewing || Boolean(approval.local && !approval.local.closed) || approval.record && approval.record.status !== "preview";
  const hasPreview = showApproval && (approval.local?.loading || approval.record?.status === "preview" && !approval.local?.closed);
  const primary = hasPreview ? "" : a.type === "navigate" ? link(a.label, a.url).replace("<a ", '<a class="primary-action" data-primary="true" ') :
    `<button class="primary-action" data-primary="true" type="button" ${a.type === "prepare" ? `data-action="${a.kind}" data-target="${escape(a.targetId)}"${a.runId ? ` data-run="${escape(a.runId)}"` : ""}` : `data-primary-effect="${a.type}" data-target="${escape(a.targetId)}"`}>${escape(a.label)}</button>`;
  const board = item.placement ? `${item.placement.archived ? "Archived in" : "In"} APM project${item.placement.status ? ` / ${item.placement.status}` : ""}` : item.boardKnown ? "Not in the loaded APM project" : "Project membership unknown";
  const options = item.kind === "issue" ? ["scope-draft", ...(lc.accepted ? ["horizon", "scope-withdraw"] : ["scope-accept", "triage"]),
    ...(!prs.some(p => p.state === "OPEN") && !lc.runs.length && !lc.queuedRequests.length && lc.accepted && !lc.deferred && !lc.design ? ["implement"] : []),
    "defer", "design", "decline"].filter(kind => kind !== a.kind) : [];
  const linked = prs.map(pr => `<div class="compact-pr">${item.kind === "issue" ? link(`PR #${pr.number}`, pr.url) : `<strong>PR #${pr.number}</strong>`} ${stateLabel(pr)}
    <span class="pr-title">${escape(pr.title)}</span><p>${escape(checkSummary(pr))}</p>
    ${workflowSummary(pr, link, date)}
    ${item.kind === "issue" && item.related.find(r => r.pr === pr.number)?.kind !== "closing" ? '<p class="muted">Referenced contribution; does not by itself complete this issue.</p>' : ""}</div>`).join("") +
    (item.kind === "issue" ? item.related.filter(ref => !prs.some(pr => pr.number === ref.pr)).map(ref =>
      `<div class="compact-pr">${link(`PR #${ref.pr}`, `https://github.com/microsoft/apm/pull/${ref.pr}`)}<p>Linked PR details unavailable; current state and scope are not established.</p></div>`).join("") : "");
  const issueLinks = item.kind === "pr" ? `<section class="related-issues" aria-label="Related issues"><strong>Related issues</strong>
    ${item.related.length ? item.related.map(ref => `<p>${link(`Issue #${ref.issue}`, `https://github.com/microsoft/apm/issues/${ref.issue}`)}: ${ref.kind === "closing" ? "Closing reference; completion is not inferred here." : "Partial contribution or reference; does not by itself complete the issue."}</p>`).join("") : "<p>No issue relationship observed. Contribution admission and code review remain separate.</p>"}</section>` : "";
  return `<header class="decision-heading"><h2${!result ? ' class="source-heading"' : ""}>${escape(result?.title || item.title)}</h2>
    <p class="selection-meta">${link(`${item.kind === "issue" ? "Issue" : "PR"} #${item.number}`, item.url)}${stateLabel(item)}
      ${lc.accepted ? `<span>Scope agreed${lc.current ? "" : " (last known)"}</span>` : ""}<span class="horizon-badge">Horizon ${escape(item.horizon)}</span></p></header>
    ${selectedProgress(state)}
    <section class="next-step" aria-label="Recommended next action"><p class="current-status">${escape(a.status)}</p>
      ${primary}<p class="consequence">${escape(a.consequence)}</p>${a.warning ? `<p class="attention">${escape(a.warning)}</p>` : ""}
      ${result?.decisionQuestion ? `<p class="decision-question"><strong>${result.decisionQuestion.state === "resolved" ? "Agreed direction" : result.decisionQuestion.state === "unclear" ? "Question to check" : "Judgment to make"}:</strong> ${escape(result.decisionQuestion.text)}</p>` : ""}
    </section>
    ${approval.local?.closed || !showApproval ? "" : workflowApprovalHtml(state, link, date)}
    ${issueLinks}
    <section class="related-summary" aria-label="Related pull requests">${linked || '<p class="muted">No linked PR observed in the loaded sources.</p>'}</section>
    ${prs.length ? `<p class="selected-checks-timing">Selected checks update every 10 sec while waiting or running.
      <span data-check-timing>${state.checksRead?.status === "reading" ? "Checking now..." : ""}</span>
      ${state.checksRead?.error ? `<span class="failed">${escape(state.checksRead.error)}</span> <button type="button" data-checks-retry>Retry selected checks</button>` : ""}</p>` : ""}
    ${reviewing && item.attention?.permission ? `<p class="separate-permission">This PR also has a workflow permission blocker. It does not prevent inspecting the diff.
      <button type="button" data-primary-effect="approve-workflows" data-target="${escape(item.id)}">Inspect workflow permission separately</button></p>` : ""}
    ${explanation(state, item, link)}
    <details class="section more-actions" data-disclosure="actions"><summary>Other actions</summary>
      <p class="muted">Prepare a separate request. Each preview describes its exact effects before confirmation.</p>
      <div class="action-buttons">${options.map(kind => button(kind, item.id)).join("")}</div>
      ${lc.runs.filter(r => ["waiting-human", "blocked"].includes(r.state) && r.id !== a.runId).map(r =>
        `<button type="button" data-action="resume" data-target="${escape(item.id)}" data-run="${escape(r.id)}">Prepare continuation of existing work</button>`).join("")}
      ${prs.filter(p => p.state === "OPEN").map(pr => `<div class="pr-actions"><h3>PR #${pr.number}</h3>
        ${pr.autoMerge === "ON" ? '<p class="attention">Auto-merge is on. A qualifying check or review change could allow merging.</p>' : ""}
        <div class="action-buttons">${["admission", "review", "recover"].map(kind => button(kind, pr.id)).join("")}</div>
        ${a.url !== `${pr.url}/files` ? link(`Open PR #${pr.number} for human review`, `${pr.url}/files`) : ""}
        ${a.url !== pr.url && item.kind !== "issue" ? link(`Open PR #${pr.number} on GitHub`, pr.url) : ""}</div>`).join("")}
    </details>
    <details class="section" data-disclosure="people"><summary>People and existing work</summary>${people(item)}
      ${prs.filter(p => p !== item).map(p => `<h3>PR #${p.number}</h3>${people(p)}`).join("")}
      ${lc.runs.map(r => `<p>${escape(r.name)}: ${escape(r.state)}. ${escape(r.blocker || "")}</p>`).join("")}
      <p>Execution: ${item.execution.verified ? escape(item.execution.name) : "Not observed"}.</p></details>
    <details class="section" data-disclosure="evidence"><summary>Technical checks and source records</summary>
      <p>${escape(board)}. <span>${escape(item.lifecycle.stage)}</span>. Item changed ${date(item.updatedAt)}; metadata read ${date(item.observedAt)}.</p>
      ${prs.map(pr => `<p>PR #${pr.number}: review ${escape(pr.reviewDecision)}; mergeability ${escape(pr.mergeable)} / ${escape(pr.mergeState)}.</p>${evidence(pr)}`).join("")}
      <p>${escape(lc.explanation)}</p>
      ${lc.record?.acceptance?.approvalUrl ? link("Canonical human scope record", lc.record.acceptance.approvalUrl) : ""}
      ${brief.scope.map(s => `<div class="source-record">${link(s.label, s.url)}<pre class="source-text">${escape(s.body)}</pre></div>`).join("")}
      ${brief.advice.map(s => `<div class="source-record">${link(`Advisory by ${s.author}`, s.url)}<pre class="source-text">${escape(s.body)}</pre></div>`).join("")}
      ${state.detailError ? `<p class="failed">${escape(state.detailError)}</p>` : ""}
      <div class="links">${brief.sources.filter(s => s.url !== a.url && !prs.some(p => s.url === p.url || s.url === `${p.url}/checks`)).map(s => link(s.label, s.url)).join("")}</div>
    </details>
    <details class="section" data-disclosure="original"><summary>Original title and full issue text</summary><h3>${escape(item.title)}</h3><pre class="source-text">${escape(brief.sourceExcerpt)}</pre></details>
    <details class="section" data-disclosure="mechanics"><summary>What this view does</summary><p>${escape(brief.wouldDo)}</p><p>${escape(brief.wouldNotDo)}</p></details>`;
}
