import assert from "node:assert/strict";
import { SourceTextModule } from "node:vm";
import { webcrypto } from "node:crypto";
import { readFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { STORE } from "./config.mjs";
import { responsiveStore, deferred, clone } from "./responsive-fixtures.mjs";
import { Explanations } from "./explanations.mjs";
import { startServer } from "./server.mjs";
const { JSDOM, VirtualConsole } = await import(pathToFileURL(join(STORE, "validation-jsdom/node_modules/jsdom/lib/api.js")));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const rootBase = join(STORE, "test-runs");
await mkdir(rootBase, { recursive: true });
const root = await mkdtemp(join(rootBase, "responsive-dom-"));
const f = responsiveStore(root), { store, held } = f;
store.snapshot.prs.nodes.push({ ...clone(store.snapshot.prs.nodes[1]), number: 22, title: "[Fixture] Draft contribution",
  url: "https://github.com/microsoft/apm/pull/22", isDraft: true });
const portfolio = deferred(), delivery = deferred(), initial = clone(store.snapshot);
store.github.portfolio = async () => { await portfolio.promise; return initial; };
store.view.selectedId = "microsoft/apm/issue/3";
for (const n of [1, 3]) held.set(n, deferred());
store.explanations = new Explanations(store, { send: async () => { await delivery.promise; return "fixture-dom-message"; } });
const server = await startServer(store, { instanceId: "dom-fixture" });
const report = { fixtureOnly: true, visualInspection: false, liveInference: false, approvalOperations: 0, assertions: [], exceptions: [] };
const console = new VirtualConsole();
console.on("jsdomError", error => report.exceptions.push(error.message));
const dom = new JSDOM(await (await fetch(server.url)).text(), { url: server.url, runScripts: "outside-only", pretendToBeVisual: true, virtualConsole: console });
const w = dom.window, $ = selector => w.document.querySelector(selector), timers = new Set(), intervals = new Set();
const modules = new Map();
let hidden = false, localReads = 0;
Object.defineProperty(w, "crypto", { value: webcrypto });
Object.defineProperty(w.document, "hidden", { get: () => hidden });
w.matchMedia = query => ({ matches: query.includes("max-width") && w.innerWidth < 680, addListener() {}, removeListener() {} });
w.HTMLElement.prototype.scrollIntoView = function() {};
w.scrollTo = (x, y) => { Object.defineProperty(w, "scrollX", { value: x, configurable: true }); Object.defineProperty(w, "scrollY", { value: y, configurable: true }); };
w.AbortSignal = AbortSignal; w.AbortController = AbortController;
w.fetch = (path, options) => { if (path === "/api/state") localReads++; return fetch(new URL(path, server.url), options); };
w.setTimeout = (fn, ms) => { const timer = setTimeout(() => { timers.delete(timer); fn(); }, ms); timers.add(timer); return timer; };
w.clearTimeout = timer => { clearTimeout(timer); timers.delete(timer); };
w.setInterval = (fn, ms) => { const timer = setInterval(fn, ms); intervals.add(timer); return timer; };
w.clearInterval = timer => { clearInterval(timer); intervals.delete(timer); };
const wait = async (condition, label) => {
  for (let i = 0; i < 150; i++) { if (condition()) return; await sleep(100); }
  throw new Error(`DOM did not settle: ${label}`);
};
const click = selector => { assert.ok($(selector), selector); $(selector).click(); };
async function load(url) {
  if (!modules.has(url.href)) {
    const module = new SourceTextModule(await readFile(url, "utf8"), { context: dom.getInternalVMContext(), identifier: url.href });
    modules.set(url.href, module);
    await module.link((specifier, parent) => load(new URL(specifier, parent.identifier)));
  }
  return modules.get(url.href);
}
async function claim(target) {
  await wait(() => store.explanations.requests.some(r => r.targetId.endsWith(`/${target}`) && r.status === "awaiting-agent"), "admitted explanation");
  const r = store.explanations.requests.find(r => r.targetId.endsWith(`/${target}`));
  return store.explanations.claim({ id: r.id, revision: r.revision });
}
async function complete(c) {
  await store.explanations.report({ id: c.id, revision: c.revision, claimToken: c.claimToken, contentKey: c.contentKey, status: "completed",
    result: { title: `[Fixture] Narrative for ${c.targetId}`, problem: "Fixture problem retains an installed instruction.",
      example: "Imagine keeping one installed skill while removing another.", exampleKind: "illustrative", impact: "Retained instructions remain useful.",
      citations: [c.packet.selected.url] } });
}
try {
  await (await load(new URL("./app.js", import.meta.url))).evaluate();
  await wait(() => $(".selected-progress"), "initial selected read");
  const refresh = store.refresh();
  click('[data-select="microsoft/apm/issue/1"]');
  assert.match($("#decision h2").textContent, /Preserve nested/);
  assert.equal($('.work-row[aria-pressed="true"]').dataset.select, "microsoft/apm/issue/1");
  click('[data-select="microsoft/apm/issue/3"]');
  assert.match($("#decision h2").textContent, /Planned without/);
  assert.equal($('.work-row[aria-pressed="true"]').dataset.select, "microsoft/apm/issue/3");
  assert.match($(".selected-progress").textContent, /Updating this item/);
  assert.equal($("#decision").hasAttribute("aria-busy"), false);
  report.assertions.push("Immediate A/B title and selected row while portfolio and both discussion reads are held");
  held.get(3).resolve();
  await wait(() => $(".explanation-state")?.textContent.includes("Sending explanation request"), "sending phase");
  click('[data-select="microsoft/apm/issue/1"]');
  held.get(1).resolve();
  await wait(() => store.explanations.requests.length === 2, "independent second send while first acknowledgement is held");
  assert.ok(store.explanations.requests.every(r => r.status === "sending"));
  assert.equal(store.state().explanation.busy, false);
  delivery.resolve();
  await wait(() => $(".explanation-state")?.textContent.includes("Waiting for background agent"), "waiting phase");
  assert.ok($("[data-cancel-explanation]"), "Cancellation is not behind disclosure");
  assert.equal(w.document.querySelectorAll("[data-cancel-explanation]").length, 1, "Only selected request can be cancelled here");
  const selectedRequest = store.explanations.requests.find(r => r.targetId === "microsoft/apm/issue/1");
  assert.equal($("[data-cancel-explanation]").dataset.cancelExplanation, selectedRequest.id);
  assert.match($(".explanation-state").textContent, /1 other explanation pending independently/);
  const otherClaim = await claim(3), selectedClaim = await claim(1);
  await wait(() => $(".explanation-state")?.textContent.includes("Generating in background"), "generating phase");
  $('[data-disclosure="evidence"]').open = true;
  $('[data-disclosure="evidence"] summary').focus();
  await complete(otherClaim);
  portfolio.resolve(); await refresh;
  await sleep(2500);
  assert.equal($('.work-row[aria-pressed="true"]').dataset.select, "microsoft/apm/issue/1");
  assert.match($(".explanation-state").textContent, /Generating in background/);
  assert.equal(w.document.activeElement.textContent, "Technical checks and source records");
  assert.equal($('[data-disclosure="evidence"]').open, true);
  await complete(selectedClaim);
  await wait(() => $(".plain-context"), "completed selected narrative");
  report.assertions.push("Independent per-target sends while transport acknowledgement is held; background waiting/generating/completed and selected-only cancel");
  report.assertions.push("Late other-target result and portfolio publication preserve selected narrative, disclosure and keyboard focus");
  assert.match($('#attention-groups [data-view="decisions"]').textContent, /Scope decisions 2 issues/);
  assert.match($('#attention-groups [data-view="permissions"]').textContent, /Workflow permissions 1 PRs/);
  assert.match($('#attention-groups [data-view="reviews"]').textContent, /PR reviews 2 PRs/);
  click('#attention-groups [data-view="reviews"]');
  await wait(() => store.state().view.scope === "reviews", "review scope");
  click('[data-select="microsoft/apm/pr/20"]');
  assert.equal(new URL($("[data-primary]").href).pathname, "/microsoft/apm/pull/20/files");
  await wait(() => store.state().view.selectedId === "microsoft/apm/pr/20" && !store.state().detailLoading, "selected review PR");
  assert.equal(new URL($("[data-primary]").href).pathname, "/microsoft/apm/pull/20/files");
  assert.ok($('[data-primary-effect="approve-workflows"]'), "Permission remains a separate secondary control");
  assert.match($(".related-issues").textContent, /Issue #4.*Partial contribution/);
  assert.match($('[data-queue-disclosure="review-followup"]').textContent, /1 PRs/);
  click('#attention-groups [data-view="permissions"]');
  await wait(() => store.state().view.scope === "permissions" && $("[data-primary]")?.dataset.primaryEffect === "approve-workflows", "permission-specific primary");
  assert.match($("[data-primary]").textContent, /Review workflow approval/);
  assert.equal(store.state().view.selectedId, "microsoft/apm/pr/20");
  click('#attention-groups [data-view="reviews"]');
  await wait(() => store.state().view.scope === "reviews", "review return");
  click('[data-queue-disclosure="review-followup"] [data-view="review-followup"]');
  await wait(() => $("#scope-heading").textContent === "PR follow-up", "draft follow-up");
  assert.match($("#decision h2").textContent, /Draft contribution/);
  assert.match($("[data-primary]").textContent, /Inspect draft/);
  assert.match($('#attention-groups [data-view="reviews"]').textContent, /2 PRs/);
  assert.equal($("#count-unit").textContent, "PRs");
  assert.equal($("#back-to-list").textContent, "Back to PRs");
  click('#attention-groups [data-view="reviews"]');
  await wait(() => store.state().view.scope === "reviews", "actionable reviews again");
  click('[data-select="microsoft/apm/pr/20"]');
  await wait(() => store.state().view.selectedId === "microsoft/apm/pr/20", "return selected PR");
  report.assertions.push("Compact nonadditive issue/PR attention counts, independent review/permission primaries, linked issue context and draft follow-up");
  assert.deepEqual(f.calls.approvals, []);
  assert.equal(f.calls.contexts, 0, "No approval preview or confirmation is exercised by this harness");
  await wait(() => !store.state().detailLoading && store.state().explanation.status === "awaiting-agent", "selected source and explanation acknowledgement settle");
  assert.match($("#update-schedule").textContent, /Auto-update every 4 min.*Last portfolio check/);
  hidden = true; w.document.dispatchEvent(new w.Event("visibilitychange"));
  await sleep(300);
  const reads = localReads, h2 = $("#decision h2");
  await sleep(2200);
  assert.equal(localReads, reads);
  assert.equal($("#decision h2"), h2, "Clock alone does not rebuild decision DOM");
  store.checkReads.clear();
  const pulls = f.calls.pulls;
  hidden = false; w.document.dispatchEvent(new w.Event("visibilitychange"));
  await wait(() => f.calls.pulls > pulls, "return-to-tab checks");
  report.assertions.push("Hidden tab stops polling; returning checks selected PR; local clock does not rebuild DOM");
  click("#back-to-list");
  await wait(() => w.document.activeElement.dataset.select === store.state().view.selectedId, "Back to PRs restores selected row focus");
  assert.equal(w.document.body.classList.contains("detail-open"), false);
  assert.deepEqual(report.exceptions, []);
  report.result = "passed";
} catch (error) {
  report.result = "failed"; report.error = error.stack; throw error;
} finally {
  for (const timer of timers) clearTimeout(timer);
  for (const timer of intervals) clearInterval(timer);
  hidden = true; dom.window.close();
  for (const gate of held.values()) gate.resolve();
  portfolio.resolve(); delivery.resolve(); f.approvalGate.resolve();
  await Promise.all([...store.selectedTasks.values(), ...store.checkTasks.values(), ...store.workflowApprovals.tasks.values()]);
  await store.pending; await store.publishQueue; await store.writeQueue;
  await server.close(); await rm(root, { recursive: true, force: true });
  await writeFile(join(STORE, "responsive-dom-report.json"), JSON.stringify(report, null, 2));
  process.stdout.write(`${report.result}: ${report.assertions.length} nonvisual actual-client interaction groups\n`);
}
