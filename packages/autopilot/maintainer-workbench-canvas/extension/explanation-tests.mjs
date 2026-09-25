import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Explanations, explanationPacket, EXPLANATION_CONTRACT } from "./explanations.mjs";
import { primaryAction } from "./primary-action.mjs";
import { renderDecisionHtml, escape, checkSummary } from "./decision-view.mjs";
import { fixtureStore, now } from "./fixtures.mjs";
import { STORE } from "./config.mjs";
import { startServer } from "./server.mjs";
import { renderHtml } from "./ui.mjs";
import { buildModel } from "./model.mjs";
import { EXPLANATION_MODEL, explanationDispatchContext, explanationTools } from "./explanation-dispatch.mjs";

async function setup(t, options = {}) {
  const base = join(STORE, "test-runs"); await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, "explanation-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = fixtureStore(root), sends = [];
  store.explanations = new Explanations(store, { send: async value => { sends.push(value); return "fixture-message-id"; }, ...options });
  await store.explanations.load();
  return { store, ex: store.explanations, sends };
}
const request = (store, retry = false) => {
  const context = store.state().explanation;
  return store.explanations.request({ targetId: context.targetId, contentKey: context.contentKey, retry }, "fixture-explanations");
};
const exampleResult = packet => ({
  title: "Keep the intended skill files",
  problem: "A fixture cleanup can discard files that are still needed.",
  example: "Imagine retaining a parent package while removing only an unused nested skill.",
  exampleKind: "illustrative", impact: "Required files should remain available after cleanup.",
  tradeoff: "Remove leftovers without deleting retained content.",
  citations: [packet.selected.url],
});
const report = claim => ({ id: claim.id, revision: claim.revision, claimToken: claim.claimToken, contentKey: claim.contentKey,
  status: "completed", result: exampleResult(claim.packet) });
async function claim(store) {
  const r = await request(store);
  return store.explanations.claim({ id: r.id, revision: r.revision });
}
const helpers = {
  link: (label, url) => `<a href="${escape(url)}">${escape(label)}</a>`,
  stateLabel: item => `<span>${escape(item.displayState.label)}</span>`, people: () => "Execution: Not observed",
  evidence: () => "", date: value => value || "Not observed",
};
function ciFixture(store) {
  const pr = store.snapshot.prs.nodes[0];
  const e = { head: pr.headRefOid, observedAt: now(), checks: { complete: true, nodes: [
    { name: "CLA", conclusion: "SUCCESS", status: "COMPLETED" },
    { name: "Advisory", conclusion: "NEUTRAL", status: "COMPLETED" },
  ] }, runs: { complete: true, observedAt: now(), nodes: Array.from({ length: 6 }, (_, i) => ({
    name: `Permission ${i}`, workflow_id: i + 1, event: "pull_request", head_sha: pr.headRefOid, status: "completed",
    conclusion: "action_required", html_url: `https://github.com/microsoft/apm/actions/runs/${100 + i}`, created_at: now(),
  })) } };
  store.snapshot.pulls[20] = { status: "current", observedAt: now(), data: { ...pr, evidence: e } };
  store.view.scope = "delivery"; store.view.selectedId = "microsoft/apm/issue/4";
  return store.snapshot.pulls[20].data;
}

test("canonical primary action: stale precedes CI, failure and permission coexist, preview is not approval", async t => {
  const { store } = await setup(t); const pr = ciFixture(store);
  let item = store.state().items.find(i => i.number === 4);
  assert.equal(item.primaryAction.label, "Review workflow approval for PR #20");
  assert.equal(item.primaryAction.type, "approve-workflows");
  assert.equal(item.primaryAction.url, "https://github.com/microsoft/apm/pull/20");
  assert.match(item.primaryAction.consequence, /Nothing is approved/);
  assert.match(item.primaryAction.status, /Other checks may already have results; review can still proceed/);
  pr.evidence.checks.nodes.push({ name: "Actual failure", conclusion: "FAILURE", status: "COMPLETED" });
  item = store.state().items.find(i => i.number === 4);
  assert.equal(item.primaryAction.label, "Open failed checks for PR #20");
  assert.match(item.primaryAction.status, /also need a GitHub permission/);
  pr.evidence.head = "c".repeat(40);
  item = store.state().items.find(i => i.number === 4);
  assert.equal(item.primaryAction.type, "refresh");
  assert.doesNotMatch(item.primaryAction.status, /asking for.*permission/);
});

test("primary preparation labels name immediate effect and preserve closed/accepted/deferred/run boundaries", async t => {
  const { store } = await setup(t), model = store.state();
  const issue = model.items.find(i => i.number === 1);
  assert.equal(issue.primaryAction.kind, "scope-draft");
  assert.equal(issue.primaryAction.label, "Prepare scope decision");
  assert.match(issue.primaryAction.consequence, /Nothing is accepted, published or started/);
  assert.equal(model.items.find(i => i.number === 2).primaryAction.kind, "horizon");
  assert.equal(model.items.find(i => i.number === 5).primaryAction.type, "navigate");
  assert.equal(model.items.find(i => i.number === 6).primaryAction.label, "Open recorded outcome");
  issue.lifecycle.runs = [{ id: "same", state: "waiting-human", blocker: "Needs input", sessionId: "observed" }];
  assert.equal(primaryAction(issue).runId, "same");
  assert.equal(primaryAction(issue).kind, "resume");
  issue.lifecycle.runs[0].state = "plan-required";
  assert.equal(primaryAction(issue).type, "observe");
});

test("one primary, action before explanation, compact linked PR and no duplicate check links", async t => {
  const { store } = await setup(t); ciFixture(store);
  const state = store.state(), html = renderDecisionHtml(state, helpers);
  assert.equal((html.match(/data-primary="true"/g) || []).length, 1);
  assert.ok((html.match(/href="https:\/\/github.com\/microsoft\/apm\/pull\/20\/checks"/g) || []).length <= 1);
  assert.ok(html.indexOf("Review workflow approval") < html.indexOf("Plain-English context"));
  assert.ok(html.indexOf('aria-label="Related pull requests"') < html.indexOf("Plain-English context"));
  assert.match(html, /Scope agreed/);
  assert.match(checkSummary(state.items.find(i => i.kind === "pr" && i.number === 20)), /1 success, 1 neutral; 6 workflows awaiting permission/);
  assert.doesNotMatch(html, /specific reproducible example has not|no test results exist|cannot.*review/);
  const shell = renderHtml("fixture");
  assert.match(shell, /Attention/); assert.match(shell, /Scope decisions/); assert.doesNotMatch(shell, /Your decisions/);
  assert.match(shell, /Check GitHub for updates/);
  assert.match(shell, /Get the latest issue, pull request, check and roadmap status\. Does not change GitHub or start work\./);
  assert.doesNotMatch(shell, /Refresh evidence/);
  assert.doesNotMatch(await readFile(new URL("./scope.mjs", import.meta.url), "utf8"), /Your decisions/);
  assert.ok(shell.indexOf('class="queue-pane"') < shell.indexOf('id="search"'));
});

test("selected explanation sends immediate ID/revision relay once; acknowledgement is not inference or execution", async t => {
  const { store, ex, sends } = await setup(t);
  const [a, b] = await Promise.all([request(store), request(store)]);
  assert.equal(a.id, b.id); assert.equal(sends.length, 1);
  assert.equal(sends[0].mode, "immediate");
  assert.match(sends[0].prompt, /Request ID: [\da-f-]+\nRevision: 1/);
  assert.match(sends[0].prompt, /exactly one general-purpose task subagent with mode "background"/);
  assert.equal(EXPLANATION_MODEL, "gpt-6-sol");
  assert.ok(sends[0].prompt.includes(`model: "${EXPLANATION_MODEL}"`));
  assert.match(sends[0].prompt, /Do not inherit the parent\/session default or use Astra/);
  assert.match(sends[0].prompt, /do not silently fall back to another model/);
  assert.match(sends[0].prompt, /Read the complete frozen packet/);
  assert.match(sends[0].prompt, /apm_explanation_report/);
  assert.match(sends[0].prompt, /no filesystem access is needed/);
  assert.doesNotMatch(sends[0].prompt, /controlled fixture|Repair only|ghp_/);
  assert.equal(a.status, "awaiting-agent");
  assert.equal(a.transportMessageId, "fixture-message-id");
  assert.equal(store.bridge.requests.length, 0);
  assert.equal(ex.inspect(a.id).contract.advisoryOnly, true);
});

test("narrative key ignores transient checks but changes for source text, PR head and later human decisions", async t => {
  const { store } = await setup(t);
  ciFixture(store);
  const key = () => explanationPacket(store, store.view.selectedId).contentKey;
  const original = key();
  store.snapshot.fetchedAt = "2099-01-01T00:00:00Z";
  store.snapshot.sources.issues.observedAt = now();
  store.snapshot.lifecycle.issues[4].observedAt = now();
  store.snapshot.pulls[20].observedAt = now();
  assert.equal(key(), original);
  store.snapshot.pulls[20].data.evidence.runs.nodes[0].conclusion = "success";
  assert.equal(key(), original);
  store.snapshot.pulls[20].data.evidence.runs.nodes[0].conclusion = "action_required";
  assert.equal(key(), original);
  store.snapshot.prs.nodes[0].headRefOid = "d".repeat(40);
  store.snapshot.pulls[20].data.headRefOid = "d".repeat(40);
  assert.notEqual(key(), original);
  const next = key();
  store.snapshot.lifecycle.issues[4].latestHumanDiscussion = [{ author: "fixture-maintainer", body: "Scope includes known prompt fields.", url: "https://github.com/microsoft/apm/issues/4#issuecomment-44" }];
  assert.notEqual(key(), next);
  const textBefore = key();
  store.snapshot.issues.nodes[3].body += " Additional source example.";
  assert.notEqual(key(), textBefore);
});

test("agreed human scope outranks stale PR question in bounded packet; actual CTA remains workflow permission", async t => {
  const { store, ex } = await setup(t);
  const pr = ciFixture(store);
  pr.body = "References #4. Open review point: should settings.json be scanned? No scope record.";
  store.snapshot.prs.nodes[0].body = pr.body;
  store.snapshot.lifecycle.issues[4].acceptance.record.Scope = "Discover known hooks in settings.json; inspect only prompt-bearing fields. Exclude transcripts, caches and command strings. Never execute hooks.";
  store.snapshot.lifecycle.issues[4].latestHumanDiscussion = [{ author: "fixture-maintainer", body: "Scope already agreed; include the known prompt fields.", url: "https://github.com/microsoft/apm/issues/4#issuecomment-444" }];
  const c = await claim(store);
  assert.match(c.packet.selected.canonical.record.Scope, /prompt-bearing/);
  assert.match(c.packet.related[0].body, /No scope record/);
  assert.equal(c.packet.related[0].currentChecks, undefined);
  assert.equal(c.packet.version, 2);
  assert.match(EXPLANATION_CONTRACT.instructions, /Never put current CI/);
  assert.match(c.packet.selected.latestHumanDiscussion[0].body, /already agreed/);
  assert.match(c.contract.instructions, /take precedence over stale author prose/);
  assert.match(c.contract.instructions, /do not resurrect a resolved PR question/);
  assert.equal(c.packet.selected.canonical.authorizes_implementation, false);
  const output = report(c);
  output.result.decisionQuestion = { state: "resolved", text: "Scope already includes known prompt fields, not logs. Review whether the contribution implements that agreed boundary." };
  await ex.report(output);
  const state = store.state(), html = renderDecisionHtml(state, helpers);
  assert.match(html, /Agreed direction/); assert.match(html, /Scope agreed/);
  assert.equal(state.items.find(i => i.number === 4).primaryAction.label, "Review workflow approval for PR #20");
  assert.equal(store.bridge.requests.length, 0);
});

test("unloaded linked PR remains visible and pending explanation cancellation cannot affect operational work", async t => {
  const { store, ex } = await setup(t);
  store.snapshot.details[1] = { data: { ...store.snapshot.issues.nodes[0],
    timelineReferences: [{ number: 99, url: "https://github.com/microsoft/apm/pull/99" }], discussion: [] }, observedAt: now(), status: "current" };
  assert.match(renderDecisionHtml(store.state(), helpers), /PR #99/);
  assert.match(renderDecisionHtml(store.state(), helpers), /details unavailable/);
  const c = await claim(store);
  await ex.cancel({ id: c.id, revision: c.revision });
  assert.equal(store.bridge.requests.length, 0);
  await assert.rejects(() => ex.report(report(c)), /Stale/);
});

test("CAS claim and report are private, exact and transactional; unsupported authority fields rejected", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  assert.ok(c.claimToken);
  assert.equal(ex.public(ex.get(c.id)).claimToken, undefined);
  assert.equal(store.state().explanation.claimToken, undefined);
  await assert.rejects(() => ex.claim({ id: c.id, revision: c.revision }), /Stale/);
  const original = JSON.stringify(ex.get(c.id));
  await assert.rejects(() => ex.report({ ...report(c), claimToken: "wrong" }), /Stale/);
  await assert.rejects(() => ex.report({ ...report(c), result: { ...exampleResult(c.packet), status: "accepted" } }), /Invalid/);
  assert.equal(JSON.stringify(ex.get(c.id)), original);
  await ex.report(report(c));
  await assert.rejects(() => ex.report(report(c)), /Stale/);
});

test("persisted completed inference rehydrates without resend and hidden token never reaches HTTP", async t => {
  const { store, ex, sends } = await setup(t), c = await claim(store);
  await ex.report(report(c));
  const replacement = new Explanations(store, { send: async () => { throw new Error("Must not resend"); } });
  await replacement.load(); store.explanations = replacement;
  assert.equal(store.state().explanation.status, "completed");
  assert.equal((await request(store)).id, c.id); assert.equal(sends.length, 1);
  assert.equal((await readFile(join(store.directory, "explanations.json"), "utf8")).includes(c.claimToken), false);
});

test("late source report records visible retryable failure, not a stranded claim or stale explanation", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  store.snapshot.issues.nodes[0].body += " Changed scope context.";
  assert.equal(store.state().explanation.status, "generating");
  assert.equal(store.state().explanation.sourceChanged, true);
  assert.equal(store.state().explanation.requestContentKey, c.contentKey);
  const result = await ex.report(report(c));
  assert.equal(result.status, "failed");
  assert.equal(result.result, null);
  const state = store.state().explanation;
  assert.equal(state.status, "failed");
  assert.equal(state.activeCount, 0);
  assert.match(state.error, /No outdated explanation was saved/);
  const next = await request(store, true);
  assert.notEqual(next.id, c.id); assert.equal(ex.get(c.id).status, "failed");
});

test("two selected targets generate concurrently and finish in reverse order without stealing selection", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  await store.setView({ scope: "triage", selectedId: "microsoft/apm/issue/3" });
  assert.equal(store.state().explanation.busy, false);
  const second = await claim(store);
  assert.equal(store.state().explanation.activeCount, 2);
  assert.equal(store.state().explanation.capacityRemaining, 98);
  assert.deepEqual(store.state().explanation.pending.map(r => r.status), ["generating", "generating"]);
  await ex.report(report(second));
  await ex.report(report(c));
  assert.equal(store.state().view.selectedId, "microsoft/apm/issue/3");
  assert.equal(store.state().explanation.status, "completed");
  assert.equal(store.state().explanation.id, second.id);
  assert.equal(ex.requests.length, 2);
});

test("source changes before claim rebase the same request and return a fresh packet for the worker", async t => {
  const { store, ex } = await setup(t);
  const requested = await request(store), before = ex.inspect(requested.id);
  store.snapshot.issues.nodes[0].body += " Later human context before dispatch.";
  const state = store.state().explanation;
  assert.equal(state.status, "awaiting-agent");
  assert.equal(state.id, requested.id);
  assert.equal(state.sourceChanged, true);
  const c = await ex.claim({ id: requested.id, revision: requested.revision });
  assert.equal(c.id, requested.id);
  assert.notEqual(c.contentKey, before.contentKey);
  assert.equal(c.contentKey, state.contentKey);
  assert.match(c.packet.selected.body, /Later human context before dispatch/);
  assert.ok(c.sourceRefreshedAt);
  await ex.report(report(c));
  assert.equal(store.state().explanation.status, "completed");
});

test("cancellation and explicit replacement affect only their own target", async t => {
  const { store, ex } = await setup(t), first = await claim(store);
  await store.setView({ scope: "triage", selectedId: "microsoft/apm/issue/3" });
  const second = await claim(store);
  await ex.cancel({ id: first.id, revision: first.revision });
  await assert.rejects(() => ex.report(report(first)), /Stale/);
  assert.equal(ex.get(second.id).status, "generating");
  store.snapshot.issues.nodes.find(i => i.number === 3).body += " Updated source.";
  const replacement = await request(store);
  assert.equal(ex.get(second.id).status, "superseded");
  await assert.rejects(() => ex.report(report(second)), /Stale/);
  assert.equal(replacement.status, "awaiting-agent");
});

test("SDK worker tools claim and populate the canvas without parent writeback or operational effects", async t => {
  const { store, ex } = await setup(t);
  store.snapshot.issues.nodes[0].body = "Large bounded source. ".repeat(1000);
  const r = await request(store);
  const tools = Object.fromEntries(explanationTools(ex).map(tool => [tool.name, tool]));
  assert.deepEqual(Object.keys(tools), ["apm_explanation_get", "apm_explanation_claim", "apm_explanation_report"]);
  const read = JSON.parse(await tools.apm_explanation_get.handler({ id: r.id }));
  assert.equal(read.packet, undefined);
  assert.equal(read.contract.advisoryOnly, true);
  const claimedOutput = await tools.apm_explanation_claim.handler({ id: read.id, revision: read.revision });
  const credentials = JSON.parse(claimedOutput);
  assert.equal(credentials.packet, undefined);
  assert.ok(Buffer.byteLength(claimedOutput) < 5000);
  assert.ok(claimedOutput.indexOf('"claimToken"') < 500);
  let offset = 0, packetText = "", pages = 0;
  do {
    const output = await tools.apm_explanation_get.handler({ id: credentials.id, contentKey: credentials.contentKey, offset });
    assert.ok(Buffer.byteLength(output) < 20000);
    const page = JSON.parse(output);
    assert.equal(page.offset, offset);
    assert.equal(page.contentKey, credentials.contentKey);
    assert.equal(page.claimToken, undefined);
    packetText += page.text; offset = page.nextOffset; pages++;
  } while (offset !== null);
  assert.ok(pages >= 3);
  assert.equal(packetText.length, credentials.packetLength);
  const c = { ...credentials, packet: JSON.parse(packetText) };
  assert.equal(c.packet.selected.id, store.view.selectedId);
  assert.equal(read.claimToken, undefined);
  assert.ok(c.claimToken);
  await assert.rejects(() => tools.apm_explanation_claim.handler({ id: c.id, revision: c.revision }), /Stale/);
  const saved = JSON.parse(await tools.apm_explanation_report.handler(report(c)));
  assert.equal(saved.status, "completed");
  assert.equal(saved.claimToken, undefined);
  assert.equal(store.state().explanation.result.title, saved.result.title);
  assert.equal(store.bridge.requests.length, 0);
});

test("SDK packet pagination is bound to the frozen claim key with strict offsets and no private tokens", async t => {
  const { store, ex } = await setup(t), r = await request(store);
  const tools = Object.fromEntries(explanationTools(ex).map(tool => [tool.name, tool]));
  const c = JSON.parse(await tools.apm_explanation_claim.handler({ id: r.id, revision: r.revision }));
  const pageInput = { id: c.id, contentKey: c.contentKey, offset: 0 };
  const before = await tools.apm_explanation_get.handler(pageInput);
  store.snapshot.issues.nodes[0].body += " New live text, not part of the claimed packet.";
  assert.equal(await tools.apm_explanation_get.handler(pageInput), before);
  assert.equal(before.includes(c.claimToken), false);
  for (const input of [{ ...pageInput, contentKey: "0".repeat(64) }, { ...pageInput, offset: -1 },
    { ...pageInput, offset: 0.5 }, { ...pageInput, offset: c.packetLength }, { id: c.id, offset: 0 }]) {
    await assert.rejects(() => tools.apm_explanation_get.handler(input), /Invalid|out of bounds/);
  }
  assert.equal(ex.get(c.id).status, "generating");
  const result = JSON.parse(await tools.apm_explanation_report.handler({ id: c.id, revision: c.revision,
    claimToken: c.claimToken, contentKey: c.contentKey, status: "failed", error: "Fixture worker stopped after reading its bounded packet." }));
  assert.equal(result.status, "failed");
});
test("dispatch hook only routes real request IDs and suppresses duplicate claimed or terminal dispatch", async t => {
  const { store, ex, sends } = await setup(t), r = await request(store);
  const prompt = sends[0].prompt;
  assert.match(explanationDispatchContext(ex, prompt).additionalContext, /mode "background"/);
  assert.ok(explanationDispatchContext(ex, prompt).additionalContext.includes('model: "gpt-6-sol"'));
  assert.equal(explanationDispatchContext(ex, `Quoted example:\n${prompt}`), undefined);
  assert.equal(explanationDispatchContext(ex, prompt.replace(r.id, "00000000-0000-4000-8000-000000000000")), undefined);
  assert.equal(explanationDispatchContext(ex, null), undefined);
  const c = await ex.claim({ id: r.id, revision: r.revision });
  assert.match(explanationDispatchContext(ex, prompt).additionalContext, /already generating.*Do not dispatch/);
  await ex.report(report(c));
  assert.match(explanationDispatchContext(ex, prompt).additionalContext, /already completed.*Do not dispatch/);
});

test("worker error can be reported after a source change without leaving an active claim", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  store.snapshot.issues.nodes[0].body += " Concurrent change.";
  const failure = await ex.report({ ...report(c), status: "failed", result: undefined, error: "Source coverage is insufficient." });
  assert.equal(failure.status, "failed");
  assert.equal(store.state().explanation.status, "failed");
  assert.equal(store.state().explanation.error, "Source coverage is insufficient.");
  assert.equal(ex.requests.filter(r => r.status === "generating").length, 0);
});

test("full active journal rejects new work explicitly without evicting claims; cancellation frees capacity", async t => {
  const { store, ex } = await setup(t), first = await claim(store);
  const template = ex.get(first.id);
  ex.requests = Array.from({ length: 100 }, (_, i) => ({ ...template,
    id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    targetId: `microsoft/apm/issue/${1000 + i}`,
  }));
  const before = JSON.stringify(ex.requests);
  assert.equal(store.state().explanation.busy, true);
  assert.equal(store.state().explanation.capacityRemaining, 0);
  await assert.rejects(() => request(store), /100 unfinished requests/);
  assert.equal(JSON.stringify(ex.requests), before);
  const stopped = ex.requests[1];
  await ex.cancel({ id: stopped.id, revision: stopped.revision });
  const next = await request(store);
  assert.equal(next.status, "awaiting-agent");
  assert.equal(ex.requests.length, 100);
  assert.equal(ex.requests.some(r => r.id === stopped.id), false);
  assert.equal(ex.requests[0].id, "00000000-0000-4000-8000-000000000000");
});

test("hostile HTML is escaped, citations are allowlisted and grounded example distinction required", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  const bad = report(c);
  bad.result.citations = ["javascript:alert(1)"];
  await assert.rejects(() => ex.report(bad), /allowlist/);
  bad.result.citations = ["https://github.com/microsoft/apm/issues/999"];
  await assert.rejects(() => ex.report(bad), /allowlist/);
  bad.result.citations = [c.packet.selected.url]; bad.result.exampleKind = "tested";
  await assert.rejects(() => ex.report(bad), /distinction/);
  bad.result.exampleKind = "illustrative"; bad.result.title = '<img src=x onerror="alert(1)">';
  await ex.report(bad);
  const html = renderDecisionHtml(store.state(), helpers);
  assert.match(html, /&lt;img src=x onerror=&quot;/); assert.doesNotMatch(html, /<img /);
});

test("failed send and reload are explicit uncertain, never automatically retried; cancelled results rejected", async t => {
  let sends = 0;
  const { store, ex } = await setup(t, { send: async () => { sends++; throw new Error("transport"); } });
  const r = await request(store);
  assert.equal(r.status, "uncertain"); await request(store); assert.equal(sends, 1);
  const reloaded = new Explanations(store, { send: async () => { sends++; } });
  await reloaded.load(); store.explanations = reloaded;
  await request(store); assert.equal(sends, 1);
  const c = await reloaded.claim({ id: r.id, revision: reloaded.get(r.id).revision });
  await reloaded.cancel({ id: c.id, revision: c.revision });
  await assert.rejects(() => reloaded.report(report(c)), /Stale/);
  assert.equal((await request(store)).status, "cancelled");
  await request(store, true); assert.equal(sends, 2);
  assert.equal(ex.requests.length, 1);
});

test("transport timeout stays uncertain; no operational request or hidden success", async t => {
  const { store } = await setup(t, { send: () => new Promise(() => {}), timeout: 5 });
  assert.equal((await request(store)).status, "uncertain");
  assert.equal(store.bridge.requests.length, 0);
});

test("in-flight restart invalidates old claim, permits explicit reconciliation not automatic generation", async t => {
  const { store } = await setup(t), c = await claim(store);
  const reloaded = new Explanations(store, { send: async () => { assert.fail("automatic resend"); } });
  await reloaded.load(); store.explanations = reloaded;
  assert.equal(store.state().explanation.status, "uncertain");
  await assert.rejects(() => reloaded.report(report(c)), /Stale/);
  assert.equal((await request(store)).status, "uncertain");
  const next = await reloaded.claim({ id: c.id, revision: reloaded.get(c.id).revision });
  assert.notEqual(next.claimToken, c.claimToken);
  await reloaded.report(report(next));
});

test("error report and explicit retry persist; malformed journal and write failure fail closed", async t => {
  const { store, ex } = await setup(t), c = await claim(store);
  await ex.report({ id: c.id, revision: c.revision, claimToken: c.claimToken, contentKey: c.contentKey, status: "failed", error: "Missing source detail; refresh then retry." });
  assert.equal(store.state().explanation.status, "failed");
  assert.equal((await request(store)).id, c.id);
  assert.notEqual((await request(store, true)).id, c.id);
  await writeFile(join(store.directory, "explanations.json"), '{"schema":99}');
  const corrupt = new Explanations(store, { send: async () => assert.fail("send") });
  await corrupt.load(); assert.match(corrupt.error, /invalid/);
  await assert.rejects(() => corrupt.request({ targetId: store.view.selectedId }, "test"), /disabled/);
  store.save = async () => { throw new Error("disk full"); };
  await assert.rejects(() => ex.cancel({ id: ex.requests.at(-1).id, revision: ex.requests.at(-1).revision }), /could not be saved/);
  assert.match(ex.error, /disabled/);
});

test("HTTP supports only selected advisory request/cancel, not privileged report or claim; full mocked roundtrip", async t => {
  const { store, ex } = await setup(t);
  store.lastAttempt = Date.now();
  const server = await startServer(store); t.after(() => server.close());
  const html = await (await fetch(server.url)).text(), token = /name="canvas-token" content="([^"]+)"/.exec(html)[1];
  const post = (path, value, auth = token) => fetch(`${server.url}${path}`, { method: "POST", headers: {
    "Content-Type": "application/json", "X-Canvas-Token": auth }, body: JSON.stringify(value) });
  let context = store.state().explanation;
  assert.equal((await post("api/explanations/request", {}, "bad")).status, 403);
  for (const path of ["report", "claim", "execute"]) assert.equal((await post(`api/explanations/${path}`, {})).status, 404);
  assert.equal((await post("api/explanations/request", { targetId: "microsoft/apm/issue/3", contentKey: context.contentKey })).status, 409);
  const response = await post("api/explanations/request", { targetId: context.targetId, contentKey: context.contentKey });
  assert.equal(response.status, 200); const r = await response.json();
  const c = await ex.claim({ id: r.id, revision: r.revision }); await ex.report(report(c));
  context = (await (await fetch(`${server.url}api/state`)).json()).explanation;
  assert.equal(context.status, "completed"); assert.equal(context.claimToken, undefined); assert.equal(context.packet, undefined);
  assert.equal(store.bridge.requests.length, 0);
});

test("content and request journals are bounded; missing guides never claim completed inference", async t => {
  const { store, ex } = await setup(t);
  store.snapshot.issues.nodes[0].body = "x".repeat(20000);
  const packet = explanationPacket(store, store.view.selectedId);
  assert.equal(packet.selected.body.length, 14000); assert.equal(packet.selected.coverage.bodyTruncated, true);
  assert.equal(packet.priorEditorialContext, null);
  assert.ok(packet.sources.length <= 64);
  const active = await claim(store);
  await store.setView({ scope: "triage", selectedId: "microsoft/apm/issue/3" });
  for (let i = 0; i < 101; i++) {
    store.snapshot.issues.nodes.find(issue => issue.number === 3).body = `Fixture version ${i}`;
    const c = await claim(store); await ex.report(report(c));
  }
  assert.equal(ex.requests.length, 100);
  assert.equal(ex.get(active.id).status, "generating");
  await ex.report(report(active));
  assert.match(EXPLANATION_CONTRACT.instructions, /No invented reproduction, test, approval or execution claims/);
});

test("selected PR discussion reads stay bounded and errors explicit without corrupting source model", async t => {
  const { store } = await setup(t); ciFixture(store);
  let reads = 0;
  store.github.selectedDiscussion = async () => { reads++; return { discussion: [{ author: "fixture", body: "Latest reply", url: "https://github.com/microsoft/apm/pull/20#issuecomment-42" }], complete: true, observedAt: now() }; };
  await store.readContextDiscussions(store.snapshot, store.view.selectedId);
  await store.readContextDiscussions(store.snapshot, store.view.selectedId); assert.equal(reads, 1);
  assert.match(explanationPacket(store, store.view.selectedId).related[0].discussion[0].body, /Latest reply/);
  store.snapshot.discussions[20].observedAt = "2020-01-01";
  store.github.selectedDiscussion = async () => { throw new Error("GitHub unavailable"); };
  await store.readContextDiscussions(store.snapshot, store.view.selectedId);
  assert.equal(store.snapshot.discussions[20].status, "error");
  assert.equal(store.snapshot.discussions[20].discussion.length, 1);
  assert.equal(buildModel(store.snapshot).items.find(i => i.number === 4).state, "OPEN");
});
