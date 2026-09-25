import { projectView, ATTENTION_SCOPES, attentionCounts, attentionReason, contextualAction, inScope } from "./scope.mjs";
import { ACTIONS } from "./actions.mjs";
import { renderDecisionHtml } from "./decision-view.mjs";
import { pollDelay } from "./async-view.mjs";

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="canvas-token"]').content;
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
let state;
let dirty = false;
let version = 0;
const clientId = crypto.randomUUID();
const selectionCache = new Map();
let approvalUi = null;
let pollTimer, clockTimer, polling = false, pollController;
const limits = new Map();
const batch = new Set();
let composer = null;
const explanationAttempts = new Set();
const explaining = new Set();
let attentionScope = "decisions";

function url(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" && u.hostname === "github.com" && !u.username && !u.password && !u.port &&
      (u.pathname.startsWith("/microsoft/apm/") || /^\/orgs\/microsoft\/projects\/\d+(\/|$)/.test(u.pathname)) ? u.href : null;
  } catch { return null; }
}
function link(label, value) {
  const target = url(value);
  return target ? `<a href="${escape(target)}" target="_blank" rel="noopener noreferrer">${escape(label)}</a>` : escape(label);
}
function date(value) { return !value ? "Not observed" : Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Unknown source time"; }
function shortHead(value) { return value ? value.slice(0, 12) : "Unknown"; }
function itemFor(id) { return state.items.find(i => i.id === id); }

async function request(path, data, signal) {
  const timeout = AbortSignal.timeout(path.includes("workflow-approvals/preview") ? 90000 : 30000);
  const response = await fetch(path, { ...(data === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Canvas-Token": token }, body: JSON.stringify(data),
  }), signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Local read returned ${response.status}.`);
  return result;
}
function fail(error) { $("error").hidden = false; $("error").textContent = error.message; }

function stateLabel(item) {
  const { key, label } = item.displayState;
  return `<span class="state-label" data-state="${escape(key)}"><span class="state-dot" aria-hidden="true"></span>${escape(label)}</span>`;
}

function freshness(evidence) {
  if (evidence.status === "outdated-head") return '<span class="freshness freshness-stale">Stale: head changed</span>';
  if (!evidence.observedAt) return '<span class="freshness freshness-unknown">Current-head evidence unknown</span>';
  if (!evidence.fresh) return '<span class="freshness freshness-stale">Stale or unverified source read</span>';
  if (!evidence.complete) return '<span class="freshness freshness-stale">Observed, partial coverage</span>';
  return '<span class="freshness freshness-observed">Observed on current head</span>';
}

function renderQueues() {
  const followupOpen = document.querySelector('[data-queue-disclosure="review-followup"]')?.open;
  const projection = projectView(state.items, state.view);
  const visible = projection.visible;
  for (const id of batch) if (!visible.some(item => item.id === id)) batch.delete(id);
  $("scope-heading").textContent = projection.label;
  $("scope-total").textContent = projection.filteredTotal;
  $("count-unit").textContent = projection.kind === "issues" ? "issues" : "PRs";
  $("queue-help").textContent = `Each ${projection.kind === "issues" ? "issue" : "PR"} appears once here. Counts reflect this queue's filters.`;
  $("attention-purpose").textContent = {
    decisions: "Decide the proposed issue scope, not whether CI may run.",
    permissions: "Decide whether contributor workflows may run. This is not a code review.",
    reviews: "Inspect the proposed code, independently of workflow permission or CI results.",
    "review-followup": "Contributor and review follow-up; excluded from the actionable PR review count.",
  }[state.view.scope] || "";
  $("attention-purpose").hidden = !$("attention-purpose").textContent;
  const attentionActive = ATTENTION_SCOPES.includes(state.view.scope) || state.view.scope === "review-followup";
  if (ATTENTION_SCOPES.includes(state.view.scope)) attentionScope = state.view.scope;
  const counts = attentionCounts(state.items);
  $("attention-nav").hidden = !attentionActive;
  $("attention-home").setAttribute("aria-current", attentionActive ? "page" : "false");
  $("attention-groups").innerHTML = [["decisions", "Scope decisions", "issues"], ["permissions", "Workflow permissions", "PRs"], ["reviews", "PR reviews", "PRs"]].map(([scope, label, unit]) =>
    `<button type="button" data-view="${scope}" aria-current="${scope === state.view.scope || scope === "reviews" && state.view.scope === "review-followup" ? "page" : "false"}">${label} <span class="counter">${counts[scope]}</span> <span class="count-unit">${unit}</span></button>`).join("");
  $("scope-basis").textContent = projection.basis;
  $("count-basis").textContent = projection.facetBasis;
  $("planning-filter").hidden = state.view.scope !== "roadmap";
  $("horizons").innerHTML = [["All", "All planning"], ["Unset", "Accepted, not scheduled"], ["Now", "Now"], ["Next", "Next"], ["Later", "Later"]].map(([value, label]) =>
    `<button type="button" data-horizon="${value}" aria-pressed="${state.view.horizon === value}">${label}</button>`).join("");
  document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-current", button.dataset.view === state.view.scope ||
    button.closest("#attention-groups") && button.dataset.view === "reviews" && state.view.scope === "review-followup" ? "page" : "false"));
  $("batch-controls").hidden = state.view.scope !== "triage";
  const groups = state.view.scope === "roadmap"
    ? [["Accepted, not scheduled", visible.filter(i => i.lifecycle.accepted && !i.lifecycle.planned)], ...["Now", "Next", "Later"].map(h => [`Planned: ${h}`, visible.filter(i => i.lifecycle.planned && i.horizon === h)])]
    : [["", visible]];
  $("queues").innerHTML = groups.map(([label, items]) => {
    const limit = limits.get(label || state.view.scope) || 30;
    return `${label ? `<h3 class="list-group">${label}</h3>` : ""}${items.slice(0, limit).map(item => `<div class="list-row">
      ${state.view.scope === "triage" ? `<input type="checkbox" data-batch="${escape(item.id)}" aria-label="Include issue ${item.number} in triage batch"${batch.has(item.id) ? " checked" : ""}>` : ""}
      <button type="button" class="work-row" data-select="${escape(item.id)}" aria-pressed="${state.view.selectedId === item.id}">
        <span class="row-context">${item.kind === "pr" ? "PR" : "Issue"} #${item.number} <span>${escape(ATTENTION_SCOPES.includes(state.view.scope) || state.view.scope === "review-followup" ? "" : item.lifecycle.stage)}</span></span>
        <span class="row-title">${escape(item.title)}</span>
        <span class="row-state">${stateLabel(item)}<span class="row-linked">${escape(item.linkedState || "")}</span></span>
        <span class="row-reason">${escape(attentionReason(item, state.view.scope) || "")}</span>
        <span class="row-next">${item.lifecycle.plannedUnverified ? "Scope needs verification. " : ""}${escape(contextualAction(item, state.view.scope).label)}</span>
      </button></div>`).join("")}${items.length > limit ? `<button class="more" data-more="${escape(label || state.view.scope)}" type="button">Show more ${projection.kind === "issues" ? "issues" : "PRs"}</button>` : ""}`;
  }).join("") || `<p class="empty">${state.view.scope === "decisions" ? "No unresolved scope recommendation is shown. Missing history is not proof that no decision remains; check Triage or update GitHub." :
    state.view.scope === "permissions" ? "No workflow permission blocker is shown for this maintainer account. Missing or stale checks do not establish that every workflow can run." :
    state.view.scope === "reviews" ? "No actionable PR review is shown for the observed account. Check review follow-up or update GitHub; missing ownership is not a completed review." :
      "No matching work in this view. Adjust filters or update GitHub."}</p>`;
  if (state.followingApproval?.outsideQueue) {
    const followed = itemFor(state.followingApproval.itemId);
    $("queues").insertAdjacentHTML("afterbegin", `<section class="followed-item" aria-label="Following workflow approval"><strong>Following workflow approval</strong>
      <p>Not included in the ${escape(projection.label)} count.</p><span class="work-row" aria-current="true"><span class="row-context">${followed.kind === "pr" ? "PR" : "Issue"} #${followed.number}</span>
      <span class="row-title">${escape(followed.title)}</span><span class="row-next">${escape(followed.nextAction)}</span></span>
      <button type="button" data-stop-follow>Stop following this item</button></section>`);
  }
  const unlinked = state.items.filter(i => i.kind === "pr" && i.state === "OPEN" && !i.related.length);
  const followups = state.items.filter(item => inScope(item, "review-followup"));
  $("unlinked").innerHTML = state.view.scope === "delivery" ? `<section class="contribution-admission"><h3>Separate contribution admission</h3><p>${unlinked.length} observed open PRs without a loaded issue link. Not included in the issue total.</p><button data-view="admission" type="button">Inspect unlinked contributions</button></section>` :
    ["reviews", "review-followup"].includes(state.view.scope) ? `<section class="contribution-admission"><details data-queue-disclosure="review-followup"${state.view.scope === "review-followup" ? " open" : ""}><summary>Review follow-up: ${followups.length} PRs</summary><p>Drafts, requested changes, recorded approvals, own contributions and unverified ownership are separate from actionable reviews.</p>
      <button data-view="${state.view.scope === "reviews" ? "review-followup" : "reviews"}" type="button">${state.view.scope === "reviews" ? "Inspect review follow-up" : "Back to actionable reviews"}</button></details></section>` : "";
  if (followupOpen) document.querySelector('[data-queue-disclosure="review-followup"]')?.setAttribute("open", "");
  $("back-to-list").textContent = projection.kind === "issues" ? "Back to issues" : "Back to PRs";
  $("batch-count").textContent = batch.size ? `${batch.size} selected (maximum 10)` : "None selected";
}

function evidence(pr) {
  const e = pr.evidence;
  if (!e) return "";
  return `<details class="evidence"><summary>PR #${pr.number}: ${escape(e.status)} ${freshness(e)}</summary>
    <p>${escape(e.summary)}</p><p class="evidence-meta">Observed GitHub evidence. Head <code>${escape(shortHead(e.head))}</code> | Checks read ${date(e.observedAt)} | Workflows read ${date(e.workflowsObservedAt)} | Pagination ${e.complete ? "complete for returned contexts/runs" : "partial or unavailable"}.</p>
    ${e.error ? `<p class="failed">${escape(e.error)}</p>` : ""}
    <div class="table-scroll"><table><thead><tr><th>Check / platform label</th><th>Outcome</th><th>Source time</th></tr></thead><tbody>${e.checks.map(c => `<tr><td>${link(c.name, c.url)}<br><span class="muted">${escape(c.platform)}</span></td><td class="${["FAILURE", "TIMED_OUT", "STARTUP_FAILURE"].includes(c.conclusion) ? "failed" : c.conclusion === "SUCCESS" ? "outcome-success" : ""}">${escape(c.conclusion || c.status)}</td><td>${date(c.completedAt || c.startedAt)}</td></tr>`).join("")}</tbody></table></div>
    ${!e.checks.length ? '<p class="empty">No current-head check contexts observed. Zero checks does not mean approval is needed.</p>' : ""}
    <h3 class="section">Workflow runs on this head</h3><div class="links">${e.runs.map(r => link(`${r.name}: ${r.conclusion || r.status} / attempt ${r.attempt} / ${date(r.updatedAt)}`, r.url)).join("") || "No workflow runs observed."}</div>
    <p class="evidence-meta">Platform comes from the job name, not runner telemetry. This prototype does not inspect logs or infer a failure cause. Neutral eligibility advice is not acceptance evidence.</p>
    ${pr.history?.length ? `<details><summary>Historical heads, excluded from current status</summary>${pr.history.map(h => `<p class="evidence-meta">Head <code>${escape(shortHead(h.head))}</code> / observed ${date(h.observedAt)}</p><ul>${h.outcomes.map(o => `<li>${link(o.name, o.url)}: ${escape(o.conclusion)}</li>`).join("")}</ul>`).join("")}</details>` : ""}
  </details>`;
}

function people(item) {
  return `<dl class="people">
    <dt>Author</dt><dd>${escape(item.author)}</dd>
    <dt>Assignees</dt><dd>${escape(item.assignees.join(", ") || "None observed")}</dd>
    <dt>Requested reviewers</dt><dd>${escape(item.kind === "issue" ? "See each linked PR below" : item.requestedReviewers.join(", ") || "None observed")}</dd>
    <dt>Execution agent</dt><dd>${escape(item.execution.name)}</dd>
    <dt>Implementation authority</dt><dd>${escape(item.authority.status)}</dd>
  </dl>`;
}

function renderDecision() {
  const item = itemFor(state.view.selectedId);
  const brief = state.brief;
  if (!item || !projectView(state.items, state.view).visible.some(i => i.id === item.id) && state.followingApproval?.itemId !== item.id) {
    $("decision").innerHTML = `<h2>No matching item in this view</h2><p class="empty">${state.refreshing ? "Reading project evidence. No missing data is being treated as completion." : "Change the filters or choose an explicit history/repository scope to inspect other work. No out-of-scope item is shown as the current selection."}</p>`;
    return;
  }
  const preserved = new Set([...$("decision").querySelectorAll("details[open]")].map(d => d.querySelector("summary")?.textContent));
  if (approvalUi?.record) {
    const latest = state.workflowApprovals?.requests?.find(r => r.id === approvalUi.record.id);
    if (latest && latest.revision >= approvalUi.record.revision) {
      if (latest.revision > approvalUi.record.revision && latest.status !== "preview") approvalUi.error = null;
      approvalUi.record = latest;
    }
  }
  $("decision").innerHTML = renderDecisionHtml({ ...state, approvalUi }, { link, stateLabel, evidence, people, date });
  for (const d of $("decision").querySelectorAll("details")) if (preserved.has(d.querySelector("summary")?.textContent)) d.open = true;
}

function renderRequests() {
  const bridge = state.bridge;
  const freshFeed = bridge.feed && Date.now() - Date.parse(bridge.feed.observedAt) < 300000;
  $("feed-status").textContent = freshFeed
    ? `Mapped host observation ${date(bridge.feed.observedAt)} (${bridge.feed.complete ? "complete for the reported scope" : "partial"}). No sessions are inferred from names.`
    : "Run status unavailable or stale. No agent count is inferred.";
  $("request-list").innerHTML = [...bridge.requests].reverse().slice(0, 20).map(r => `<details class="request-row"${!["completed", "cancelled"].includes(r.status) ? " open" : ""}><summary>${escape(ACTIONS[r.kind]?.label || r.kind)} - ${escape(r.status)}</summary>
    <p>${escape(r.message)}</p><p class="evidence-meta">Request ${escape(r.id)} / revision ${r.revision} / ${date(r.updatedAt || r.createdAt)}. ${r.targetIds.map(id => escape(id)).join(", ") || "Fixture, no work target"}</p>
    ${r.draft ? `<button type="button" data-use-draft="${r.id}">Review and edit proposed scope</button>` : ""}
    ${!["completed", "failed", "blocked", "cancelled"].includes(r.status) ? `<button type="button" data-cancel="${r.id}">${r.status === "preview" ? "Discard preview" : "Request cancellation"}</button>` : ""}
    <ul>${r.receipts.map(receipt => `<li>${escape(receipt.step)}: ${escape(receipt.outcome)} / ${escape(receipt.tool)} / ${date(receipt.observedAt)} ${receipt.url ? link("Verified source", receipt.url) : escape(receipt.reference)}</li>`).join("")}</ul></details>`).join("") ||
    '<p class="muted">No canvas requests recorded. This does not mean no agents are working elsewhere.</p>';
  if (bridge.error) $("request-feedback").textContent = bridge.error;
}

function openComposer(kind, targetIds, values = {}) {
  const item = itemFor(targetIds[0]);
  const approval = item?.lifecycle?.record?.acceptance;
  composer = { id: crypto.randomUUID(), kind, targetIds, values: { ...(kind === "implement" && approval?.approvalUrl ? { approvalUrl: approval.approvalUrl } : {}), ...values }, preview: null };
  const fields = [];
  const input = (name, label, value = "", required = true) => `<label>${label}<input name="${name}" value="${escape(value)}" maxlength="2000"${required ? " required" : ""}></label>`;
  const textarea = (name, label) => `<label>${label}<textarea name="${name}" rows="3" maxlength="2000" required>${escape(composer.values[name] || "")}</textarea></label>`;
  if (["triage", "admission"].includes(kind)) fields.push('<label>Publication<select name="mode"><option value="preview">Preview only - no GitHub writes</option><option value="publish">Publish advice and disclosed metadata effects</option></select></label>');
  if (kind === "scope-accept") {
    fields.push(input("area", "Governance area", composer.values.area || "project"), textarea("scope", "Bounded scope"),
      textarea("doneWhen", "Done when"), textarea("outOfScope", "Out of scope"),
      input("reviewContact", "Proposed review contact (@login); consent is checked separately", composer.values.reviewContact));
  }
  if (kind === "scope-withdraw") fields.push(input("area", "Governance area of the approval being withdrawn", approval?.record?.Area || "project"));
  if (["horizon", "scope-accept"].includes(kind)) fields.push(`<label>${kind === "scope-accept" ? "Optional planning change" : "Horizon"}<select name="horizon">${kind === "scope-accept" ? '<option value="">Leave planning unchanged</option>' : ""}${["Now", "Next", "Later", "Unset"].map(h => `<option value="${h}"${composer.values.horizon === h ? " selected" : ""}>${h === "Unset" ? "Unscheduled" : h}</option>`).join("")}</select></label>`);
  if (kind === "implement") fields.push(input("approvalUrl", "Nominated existing human scope comment", composer.values.approvalUrl));
  if (["defer", "design", "decline", "scope-withdraw", "implement", "recover", "resume"].includes(kind)) fields.push(textarea("reason",
    kind === "design" ? "Concrete design question" : kind === "decline" ? "Reason and useful alternative for the contributor" : kind === "implement" ? "Bounded run objective and review-capacity context" : "Reason and intended boundary"));
  if (kind === "resume") fields.push(input("runId", "Observed existing run ID", composer.values.runId));
  $("action-composer").hidden = false;
  $("action-composer").innerHTML = `<h2>${ACTIONS[kind].label}</h2><p>${targetIds.map(id => escape(id)).join(", ") || "Fixture only; no repository target"}</p>
    <p class="action-effects">${escape(ACTIONS[kind].effects)}</p>
    <p class="muted">Actor, evidence version and ownership are checked again. This prepares a request; it does not grant a worker authority.</p>
    <form id="composer-form">${fields.join("")}<div class="action-buttons"><button class="primary-action" type="submit">Preview exact request</button><button id="close-composer" type="button">Close composer</button></div></form>
    <div id="exact-preview" aria-live="polite"></div>`;
  $("composer-form").addEventListener("submit", async event => {
    event.preventDefault();
    const current = composer;
    const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
    try {
      const params = Object.fromEntries([...new FormData(event.target)].filter(([, value]) => value !== ""));
      current.preview = await request("/api/requests/preview", { id: current.id, version: 1, kind, targetIds, params });
      if (composer !== current) { state = await request("/api/state"); renderRequests(); return; }
      for (const el of event.target.elements) el.disabled = el.id !== "close-composer";
      $("exact-preview").innerHTML = `<h3>Confirm these exact effects</h3><p>${escape(current.preview.effects)}</p><pre class="source-text">${escape(current.preview.scopeRecord?.body || JSON.stringify(current.preview.params, null, 2))}</pre>
        ${current.preview.scopeRecord?.reviewContactConfirmationNeeded ? '<p class="attention">Different review contact: roster membership is not consent. Parent must confirm review capacity and contact agreement.</p>' : ""}
        <p>Evidence watermark <code>${escape(current.preview.watermark.slice(0, 16))}</code>. Actor ${escape(current.preview.actor || "fixture only")}. The parent will request a real host confirmation before consequential effects.</p>
        <label class="confirm-check"><input id="confirm-effects" type="checkbox"> I have reviewed this request and its stated effects.</label>
        <button id="confirm-request" class="primary-action" type="button">Send request to parent</button>`;
      $("confirm-request").addEventListener("click", () => confirmComposer(current));
      state = await request("/api/state"); renderRequests();
    } catch (error) { fail(error); }
    finally { if (!current.preview) submit.disabled = false; }
  });
  $("close-composer").addEventListener("click", () => { $("action-composer").hidden = true; composer = null; $("decision").focus({ preventScroll: true }); });
  document.body.classList.add("detail-open");
  $("action-composer").focus({ preventScroll: true }); $("action-composer").scrollIntoView();
}

async function confirmComposer(current) {
  if (!$("confirm-effects").checked) { fail(new Error("Review the exact effects before sending this request.")); return; }
  const button = $("confirm-request"); button.disabled = true;
  try {
    const r = await request("/api/requests/confirm", { id: current.preview.id, revision: current.preview.revision, watermark: current.preview.watermark });
    $("request-feedback").textContent = r.message;
    if (composer === current) $("exact-preview").innerHTML = `<h3>${escape(r.status)}</h3><p>${escape(r.message)}</p><p>No acceptance or running worker is inferred from transport acknowledgement.</p>`;
    state = await request("/api/state"); renderRequests();
  } catch (error) { fail(new Error(`${error.message} Check the request journal before retrying.`)); button.disabled = false; }
}
async function cancelRequest(id) {
  try {
    const r = state.bridge.requests.find(r => r.id === id);
    const result = await request("/api/requests/cancel", { id, revision: r.revision });
    $("request-feedback").textContent = result.message;
    state = await request("/api/state"); renderRequests();
  } catch (error) { fail(error); }
}

function renderSources() {
  const names = { roadmap: "APM Roadmap", issues: "Open issues", prs: "Open pull requests" };
  const histories = state.items.filter(item => item.kind === "issue" && item.state === "OPEN");
  const currentHistories = histories.filter(item => item.lifecycle.current).length;
  const lastKnownHistories = histories.filter(item => item.lifecycle.lastKnown).length;
  $("source-health").innerHTML = `<ul>${Object.entries(names).map(([key, name]) => {
    const s = state.sources[key];
    return `<li><strong>${name}:</strong> ${s ? `${escape(s.status)}; ${s.count ?? "unknown"} of ${s.total ?? "unknown"} records, ${s.pages ?? "unknown"} pages. Read ${date(s.observedAt)}. Last attempt ${date(s.attemptedAt)}.${s.error ? ` ${escape(s.error)}` : ""}` : "not observed"}</li>`;
  }).join("")}</ul><p><strong>Decision history:</strong> ${currentHistories} current, ${lastKnownHistories} last-known, ${histories.length - currentHistories - lastKnownHistories} unavailable. Unchanged discussions are reused for up to 30 minutes without extending action freshness; changed issues and the selected issue are rechecked sooner. ${escape(state.lifecycle?.error || "")} ${state.lifecycle?.trustedSha ? link("Canonical governance revision", `https://github.com/microsoft/apm/blob/${state.lifecycle.trustedSha}/GOVERNANCE.md`) : "Trusted governance revision not available."}</p><p><strong>PR detail reads:</strong> ${Object.entries(state.coverage.pullDetails).map(([status, count]) => `${count} ${escape(status)}`).join(", ") || "not observed"}.</p><p>${escape(state.coverage.relationships)}</p><p>${escape(state.limitation)}</p><p>${state.coverage.inaccessible} draft or inaccessible Roadmap records and ${state.coverage.otherRepos} other-repository records are not issue/PR queue entities. Historical coverage is the Roadmap plus observed links and the explicitly requested #3061 example, not all closed repository work.</p>`;
}

function populateSelect(id, values, title) {
  const select = $(id);
  const content = `<option value="">${title}</option>${[...new Set(values)].sort().map(s => `<option value="${escape(s)}">${escape(s)}</option>`).join("")}`;
  if (select.innerHTML !== content) select.innerHTML = content;
  select.value = state.view[id];
}

function render() {
  if (state.brief && state.view.selectedId) {
    selectionCache.set(state.view.selectedId, { brief: state.brief, explanation: state.explanation });
    if (selectionCache.size > 50) selectionCache.delete(selectionCache.keys().next().value);
  }
  const scroll = [window.scrollX, window.scrollY];
  const panes = [...document.querySelectorAll(".queue-pane,.detail-pane")].map(el => [el, el.scrollTop, el.scrollLeft]);
  const active = document.activeElement;
  const focus = ["horizon", "select", "view", "action", "cancel", "useDraft", "batch", "primary", "explain", "primaryEffect", "cancelExplanation", "approvalAck", "confirmWorkflows", "selectedRetry", "checksRetry", "closeWorkflowPreview"].find(key => Object.hasOwn(active?.dataset || {}, key));
  const focusedLink = active?.tagName === "A" ? { href: active.href, text: active.textContent } : null;
  const focusValue = focus ? active.dataset[focus] : null;
  const focusTarget = active?.dataset?.target;
  const focusedSummary = active?.tagName === "SUMMARY" ? active.textContent : null;
  const checks = Object.values(state.sources);
  const incomplete = !checks.length || checks.some(s => s.status !== "current") || !state.lifecycle?.complete || state.lifecycle?.status !== "current";
  $("status").textContent = state.refreshing ? "Checking GitHub for portfolio updates. Previous information remains visible." :
    state.refreshProgress?.status === "error" ? state.refreshProgress.error :
    incomplete ? "Some portfolio sources are incomplete or need updating. See Sources, coverage and limits." : "Portfolio sources loaded.";
  $("refresh").disabled = state.refreshing;
  $("refresh").textContent = state.refreshing ? "Checking GitHub..." : "Check GitHub for updates";
  $("error").hidden = !state.error;
  if (state.error) $("error").textContent = state.error;
  if (state.project) { $("project-link").href = url(state.project.url); $("project-link").textContent = state.project.title; }
  $("view-notice").hidden = !state.viewNotice;
  $("view-notice").textContent = state.viewNotice || "";
  if (document.activeElement !== $("search")) $("search").value = state.view.search;
  const universe = projectView(state.items, state.view).universe;
  populateSelect("person", universe.flatMap(i => [i.author, ...i.assignees, ...i.requestedReviewers]).filter(p => p !== "Unknown"), "Anyone");
  populateSelect("area", universe.flatMap(i => i.labels), "All areas");
  renderQueues(); renderDecision(); renderSources(); renderRequests();
  if (focus) [...document.querySelectorAll("button,input,a")].find(el => el.dataset[focus] === focusValue && (!focusTarget || el.dataset.target === focusTarget))?.focus({ preventScroll: true });
  if (focusedSummary) [...document.querySelectorAll("summary")].find(el => el.textContent === focusedSummary)?.focus({ preventScroll: true });
  if (focusedLink && !focus) [...document.querySelectorAll("a")].find(el => el.href === focusedLink.href && el.textContent === focusedLink.text)?.focus({ preventScroll: true });
  for (const [el, top, left] of panes) { el.scrollTop = top; el.scrollLeft = left; }
  if (window.scrollX !== scroll[0] || window.scrollY !== scroll[1]) window.scrollTo(...scroll);
  updateClocks();
}

async function explain(retry = false) {
  const context = state?.explanation;
  if (!context?.available || explaining.has(context.targetId) || state.detailLoading || dirty || document.hidden) return;
  if (!retry && (context.status !== "missing" || context.busy || explanationAttempts.has(context.contentKey) || state.detailStatus === "not-loaded")) return;
  explaining.add(context.targetId); explanationAttempts.add(context.contentKey);
  const before = version;
  state.explanation = { ...context, status: "sending", createdAt: new Date().toISOString() };
  state.explanationError = null;
  render(); schedulePoll(500);
  try {
    await request("/api/explanations/request", { targetId: context.targetId, contentKey: context.contentKey, ...(retry ? { retry: true } : {}) });
    const result = await request("/api/state");
    if (version === before && state.view.selectedId === result.view.selectedId) { state = result; render(); }
  } catch (error) {
    if (state.view.selectedId === context.targetId) {
      state.explanation = { ...context, status: "uncertain" };
      state.explanationError = `${error.message} Delivery is uncertain; check the recorded request before retrying.`;
      render();
    }
  } finally { explaining.delete(context.targetId); schedulePoll(500); }
}
async function cancelExplanation(id) {
  const pending = state.explanation?.pending?.find(r => r.id === id);
  if (!pending) return;
  const before = version;
  try {
    await request("/api/explanations/cancel", { id, revision: pending.revision });
    const result = await request("/api/state");
    if (before === version) { state = result; render(); }
  } catch (error) { fail(error); }
}

async function select(id) {
  const before = ++version;
  dirty = false;
  localSelection(id);
  state.selectedRead = { id, status: "sending", startedAt: new Date().toISOString(), message: "Opening cached details while the selected item is checked. You can choose another issue." };
  state.detailLoading = true;
  render();
  if (matchMedia("(max-width:680px)").matches) { document.body.classList.add("detail-open"); $("decision").scrollIntoView({ behavior: "instant" }); $("decision").focus({ preventScroll: true }); }
  schedulePoll(500);
  try {
    const result = await request("/api/select", { id, intent: { client: clientId, sequence: before } });
    if (version === before) { state = result; render(); }
  } catch (error) {
    if (version === before) { state.detailLoading = false; state.selectedRead = { ...state.selectedRead, status: "error", error: error.message }; render(); }
  } finally { schedulePoll(500); }
}

function localSelection(id) {
  const cached = selectionCache.get(id);
  state = { ...state, view: { ...state.view, selectedId: id }, brief: cached?.brief || null,
    followingApproval: null, explanation: null, explanationError: null, detailStatus: "not-loaded", detailError: null, selectedRead: null, checksRead: null };
}

async function filter(patch) {
  const focusedHorizon = document.activeElement?.dataset?.horizon;
  const before = ++version;
  dirty = true;
  if (patch.scope) { batch.clear(); patch = { horizon: "All", queue: "all", search: "", person: "", area: "", ...patch }; }
  if (patch.scope) approvalUi = null;
  state.view = { ...state.view, ...patch };
  state.followingApproval = null;
  const visible = projectView(state.items, state.view).visible;
  if (!visible.some(item => item.id === state.view.selectedId)) localSelection(visible[0]?.id || null);
  render();
  try {
    const result = await request("/api/view", { ...patch, intent: { client: clientId, sequence: before } });
    if (version !== before) return;
    state = result; render();
    if (focusedHorizon) document.querySelector(`[data-horizon="${focusedHorizon}"]`)?.focus({ preventScroll: true });
  } catch (error) { fail(error); }
  finally { if (version === before) { dirty = false; schedulePoll(500); } }
}

async function previewWorkflows(targetId) {
  const local = approvalUi = { targetId, loading: true, startedAt: new Date().toISOString(), acknowledged: false };
  render();
  try {
    local.record = await request("/api/workflow-approvals/preview", { targetId });
  } catch (error) { local.error = error.message; }
  finally { local.loading = false; if (approvalUi === local) render(); schedulePoll(500); }
}

async function confirmWorkflows(id) {
  const local = approvalUi;
  if (!local?.acknowledged || local.submitting || local.record?.id !== id) return;
  local.submitting = true; render();
  try {
    local.record = await request("/api/workflow-approvals/confirm", { id, revision: local.record.revision, confirmed: true });
    local.acknowledged = false;
  } catch (error) {
    local.error = `${error.message} Do not submit again while the outcome is unknown. Recorded receipts will be reconciled with GitHub.`;
  } finally { local.submitting = false; if (local === approvalUi) render(); schedulePoll(500); }
}

async function selectedChecks() {
  if (!state?.view.selectedId || document.hidden) return;
  const before = version, id = state.view.selectedId;
  try {
    const result = await request("/api/checks", { id });
    if (before === version) { state = result; render(); }
  } catch (error) {
    if (before === version) { state.checksRead = { id, status: "error", error: error.message }; render(); }
  }
  schedulePoll(500);
}

document.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.select) select(button.dataset.select);
  if (button.dataset.view) { document.body.classList.remove("detail-open"); filter({ scope: button.dataset.view }); }
  if (button.dataset.action) openComposer(button.dataset.action, [button.dataset.target], button.dataset.run ? { runId: button.dataset.run } : {});
  if (button.dataset.explain) explain(true);
  if (button.dataset.cancelExplanation) cancelExplanation(button.dataset.cancelExplanation);
  if (button.dataset.primaryEffect === "refresh") select(state.view.selectedId);
  if (Object.hasOwn(button.dataset, "selectedRetry")) select(state.view.selectedId);
  if (Object.hasOwn(button.dataset, "checksRetry")) selectedChecks();
  if (Object.hasOwn(button.dataset, "stopFollow")) filter({});
  if (button.dataset.primaryEffect === "approve-workflows") previewWorkflows(button.dataset.target);
  if (button.dataset.primaryEffect === "observe-workflows") {
    if (approvalUi) approvalUi.closed = false;
    render(); document.querySelector(".workflow-approval")?.scrollIntoView(); selectedChecks();
  }
  if (button.dataset.confirmWorkflows) confirmWorkflows(button.dataset.confirmWorkflows);
  if (Object.hasOwn(button.dataset, "closeWorkflowPreview")) {
    const record = approvalUi?.record || state.workflowApprovals?.requests?.find(r => r.status === "preview");
    approvalUi = { targetId: record?.targetId, closed: true }; render();
  }
  if (button.dataset.primaryEffect === "observe") { $("request-panel").open = true; $("request-panel").scrollIntoView(); $("request-panel").querySelector("summary").focus({ preventScroll: true }); }
  if (button.dataset.cancel) cancelRequest(button.dataset.cancel);
  if (button.dataset.useDraft) {
    const r = state.bridge.requests.find(r => r.id === button.dataset.useDraft);
    if (r?.draft) openComposer("scope-accept", r.targetIds, r.draft);
  }
  if (button.dataset.horizon) filter({ horizon: button.dataset.horizon });
  if (button.dataset.more) { limits.set(button.dataset.more, (limits.get(button.dataset.more) || 30) + 30); renderQueues(); }
});
$("refresh").addEventListener("click", async () => {
  try {
    if (state?.minRefreshSeconds > 0) { $("status").textContent = `Refresh is bounded. Please wait ${state.minRefreshSeconds} seconds before another GitHub read.`; return; }
    const before = version;
    const result = await request("/api/refresh", {});
    if (before === version) { state = result; render(); }
    schedulePoll(500);
  } catch (error) { fail(error); }
});
$("attention-home").addEventListener("click", () => { document.body.classList.remove("detail-open"); filter({ scope: attentionScope }); });
for (const id of ["person", "area"]) $(id).addEventListener("change", () => filter({ [id]: $(id).value }));
document.addEventListener("change", event => {
  if (Object.hasOwn(event.target.dataset, "approvalAck")) {
    const record = approvalUi?.record || state.workflowApprovals?.requests?.find(r => r.status === "preview");
    approvalUi = { ...approvalUi, targetId: record?.targetId, record, acknowledged: event.target.checked };
    const button = document.querySelector("[data-confirm-workflows]");
    if (button) button.disabled = !event.target.checked;
    return;
  }
  const id = event.target.dataset.batch;
  if (!id) return;
  if (event.target.checked && batch.size >= 10) { event.target.checked = false; fail(new Error("Triage is bounded to 10 selected issues.")); return; }
  event.target.checked ? batch.add(id) : batch.delete(id);
  $("batch-count").textContent = `${batch.size} selected (maximum 10)`;
});
$("triage-batch").addEventListener("click", () => batch.size ? openComposer("triage", [...batch]) : fail(new Error("Select at least one issue for this bounded batch.")));
$("bridge-smoke").addEventListener("click", () => openComposer("smoke", []));
$("back-to-list").addEventListener("click", async () => {
  document.body.classList.remove("detail-open");
  if (state.followingApproval) { await filter({}); $("search").focus({ preventScroll: true }); return; }
  document.querySelector(`.work-row[data-select="${state.view.selectedId}"]`)?.focus({ preventScroll: true });
});
let searchTimer;
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => filter({ search: $("search").value }), 200); });
async function poll() {
  if (dirty || polling || document.hidden) return;
  polling = true;
  pollController = new AbortController();
  const before = version;
  try {
    const result = await request("/api/state", undefined, pollController.signal);
    if (version !== before || dirty || document.hidden) return;
    if (state?.selectedRead?.status === "sending" && state.view.selectedId !== result.view.selectedId) return;
    const fingerprint = data => JSON.stringify([data?.sources, data?.coverage.pullDetails, data?.detailStatus, data?.view, data?.bridge, data?.preparation, data?.explanation,
      data?.selectedRead, data?.checksRead, data?.workflowApprovals, data?.followingApproval, data?.detailError,
      data?.items.map(i => [i.id, i.lifecycle.current, i.lifecycle.lastKnown, i.lifecycle.stage, i.updatedAt, i.evidence]), data?.items.find(i => i.id === data?.view.selectedId)?.lifecycle]);
    const changed = !state || result.fetchedAt !== state.fetchedAt || result.refreshing !== state.refreshing || result.error !== state.error || fingerprint(result) !== fingerprint(state);
    state = result;
    if (changed) {
      render();
      if (state.items.length && state.detailStatus === "not-loaded" && !state.selectedRead && itemFor(state.view.selectedId)) select(state.view.selectedId);
    }
    explain();
  } catch (error) { if (!document.hidden) fail(new Error(`Cannot reach the local canvas provider. Last-rendered data is stale. ${error.message}`)); }
  finally { polling = false; }
}
function updateClocks() {
  if (!state || document.hidden) return;
  const remaining = Math.max(0, Math.ceil((Date.parse(state.nextAutoRefreshAt) - Date.now()) / 1000));
  const next = state.refreshing ? "Checking now" : Number.isFinite(remaining) && remaining > 0 ? `Next check in ${Math.floor(remaining / 60)}m ${remaining % 60}s` : "Next check is due";
  $("update-schedule").textContent = `Auto-update every 4 min while open. Last portfolio check: ${date(state.fetchedAt)}. ${next}. Hidden tabs pause updates.`;
  for (const el of document.querySelectorAll("[data-elapsed]")) {
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(el.dataset.elapsed)) / 1000));
    el.textContent = seconds < 60 ? `${seconds}s elapsed` : `${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed`;
    const guidance = el.closest("section")?.querySelector("[data-slow]");
    if (guidance) guidance.hidden = seconds < 15;
  }
  const timing = document.querySelector("[data-check-timing]");
  if (timing) {
    const read = state.checksRead;
    const left = Math.max(0, Math.ceil((Date.parse(read?.startedAt || "") + 10000 - Date.now()) / 1000));
    timing.textContent = read?.status === "reading" ? "Checking selected workflows now..." :
      read?.finishedAt ? `Last selected check: ${date(read.finishedAt)}.${pollDelay(state) === 2000 && left > 0 ? ` Next in ${left}s.` : ""}` : "Check times are shown per PR.";
  }
}
function schedulePoll(delay = pollDelay(state)) {
  clearTimeout(pollTimer);
  if (!document.hidden) pollTimer = setTimeout(tick, delay);
}
async function tick() {
  await poll();
  schedulePoll();
}
function startClock() { clearInterval(clockTimer); if (!document.hidden) clockTimer = setInterval(updateClocks, 1000); }
document.addEventListener("visibilitychange", () => {
  clearTimeout(pollTimer); startClock();
  if (document.hidden) pollController?.abort();
  else { selectedChecks(); schedulePoll(0); }
});
window.addEventListener("focus", () => { if (!document.hidden) { selectedChecks(); schedulePoll(0); } });
startClock();
await tick();
