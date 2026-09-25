import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Github, pullRecord } from "./github.mjs";
import { Store } from "./store.mjs";
import { startServer } from "./server.mjs";
import { buildModel, classify, evidenceFor, relationships, makeBrief, stateAppearance } from "./model.mjs";
import { GH, STORE, REPO, itemId, safeUrl, parseId, sanitize } from "./config.mjs";
import { defaultView as workflowDefaultView, projectView, restoreView, VIEW_SCHEMA } from "./scope.mjs";
const defaultView = () => ({ ...workflowDefaultView(), scope: "roadmap-open" });

const stamp = () => new Date().toISOString();
const head = "a".repeat(40);
const connection = nodes => ({ nodes, totalCount: nodes.length, pageInfo: { hasNextPage: false } });
const item = (number = 3015, kind = "issue", fields = {}) => ({
  number, title: `Source work ${number}`, body: "A source-grounded problem with a concrete report about a behavior that should be corrected.",
  state: "OPEN", url: `${GH}/${kind === "issue" ? "issues" : "pull"}/${number}`,
  repository: { nameWithOwner: REPO }, updatedAt: stamp(), author: { login: "contributor" },
  labels: connection([]), assignees: connection([]), ...fields,
});
const pr = (number = 3057, fields = {}) => item(number, "pr", {
  headRefOid: head, closingIssuesReferences: connection([]), reviewRequests: connection([]),
  autoMergeRequest: null, ...fields,
});
const record = (data, checks = [], runs = [], extra = {}) => ({
  status: "current", observedAt: stamp(), data: { ...data, evidence: {
    head: data.headRefOid, observedAt: stamp(),
    checks: { nodes: checks, complete: true },
    runs: { nodes: runs, complete: true, observedAt: stamp() }, ...extra,
  } },
});
const source = () => ({ status: "current", observedAt: stamp(), count: 1, total: 1, pages: 1 });
const snap = (issues = [item()], prs = [pr()]) => ({
  sources: { issues: source(), prs: source(), roadmap: source() },
  issues: { nodes: issues, complete: true }, prs: { nodes: prs, complete: true },
  roadmap: { project: { title: "APM Roadmap", url: "https://github.com/orgs/microsoft/projects/2304" },
    nodes: issues.map(i => ({ id: `roadmap-${i.number}`, isArchived: false, updatedAt: stamp(),
      fieldValues: connection([{ name: "Now", field: { name: "Horizon" } }]), content: { ...i, __typename: "Issue" } })), complete: true },
  fetchedAt: stamp(), pulls: {},
});
async function directory() {
  const base = join(STORE, "test-runs");
  await mkdir(base, { recursive: true });
  return mkdtemp(join(base, "case-"));
}

test("state appearance distinguishes issue closure reasons, PR closure, draft and unknown", () => {
  assert.deepEqual(stateAppearance({ kind: "issue", state: "CLOSED", stateReason: "COMPLETED" }), { key: "completed", label: "Completed" });
  assert.deepEqual(stateAppearance({ kind: "issue", state: "CLOSED", stateReason: "NOT_PLANNED" }), { key: "not-planned", label: "Not planned" });
  assert.deepEqual(stateAppearance({ kind: "issue", state: "CLOSED" }), { key: "closed", label: "Closed" });
  assert.deepEqual(stateAppearance({ kind: "pr", state: "CLOSED" }), { key: "closed-pr", label: "Closed" });
  assert.deepEqual(stateAppearance({ kind: "pr", state: "MERGED" }), { key: "merged", label: "Merged" });
  assert.deepEqual(stateAppearance({ kind: "pr", state: "OPEN", isDraft: true }), { key: "draft", label: "Draft" });
  assert.deepEqual(stateAppearance({ kind: "issue", state: "OPEN" }), { key: "open", label: "Open" });
  assert.deepEqual(stateAppearance({ kind: "issue" }), { key: "unknown", label: "Unknown" });
});

test("old equal-version issue detail cannot hide fresh metadata or the real primary action", () => {
  const issue = item(), pull = pr(3057, { closingIssuesReferences: connection([issue]) });
  const snapshot = snap([issue], [pull]);
  snapshot.details = { [issue.number]: { status: "current", observedAt: "2000-01-01T00:00:00Z",
    data: { ...issue, title: "Old cached title" } } };
  snapshot.pulls[pull.number] = record(pull, [], [{
    name: "CI", status: "completed", conclusion: "action_required", head_sha: head,
    html_url: `${GH}/actions/runs/123`, updated_at: stamp(),
  }]);
  const observed = buildModel(snapshot).items.find(i => i.id === itemId("issue", issue.number));
  assert.equal(observed.title, issue.title);
  assert.equal(observed.observedAt, snapshot.sources.issues.observedAt);
  assert.equal(observed.metadataFresh, true);
  assert.equal(observed.primaryAction.label, "Review workflow approval for PR #3057");
});

test("a genuinely newer issue revision still wins without borrowing another source's freshness", () => {
  const issue = item(3015, "issue", { updatedAt: "2026-09-24T00:00:00Z" });
  const snapshot = snap([issue], []);
  const observedAt = "2026-09-24T00:02:00Z";
  snapshot.details = { [issue.number]: { status: "current", observedAt,
    data: { ...issue, state: "CLOSED", updatedAt: "2026-09-24T00:01:00Z" } } };
  const observed = buildModel(snapshot, Date.parse("2026-09-25T00:00:00Z")).items.find(i => i.id === itemId("issue", issue.number));
  assert.equal(observed.state, "CLOSED");
  assert.equal(observed.observedAt, observedAt);
  assert.equal(observed.metadataFresh, false);
});

test("placement meta describes project membership separately from lifecycle and Horizon", async () => {
  const source = await readFile(new URL("./decision-view.mjs", import.meta.url), "utf8");
  const placement = source.split("\n").find(line => line.trimStart().startsWith("const board ="));
  assert.ok(placement);
  assert.ok(placement.includes('"Archived in" : "In"} APM project'));
  assert.ok(placement.includes('"Not in the loaded APM project"'));
  assert.ok(placement.includes('"Project membership unknown"'));
  assert.doesNotMatch(placement, /Roadmap|lifecycle|horizon/);
  assert.ok(source.includes('<span class="horizon-badge">Horizon ${escape(item.horizon)}</span>'));
  assert.ok(source.includes('<span>${escape(item.lifecycle.stage)}</span>'));
});

test("light and dark open-state fallbacks meet text contrast on base and selected surfaces", async () => {
  const css = await readFile(new URL("./style.css", import.meta.url), "utf8");
  const colors = name => [...css.matchAll(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, "g"))].map(match => match[1]);
  const luminance = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
    .reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
  const foregrounds = colors("state-open");
  assert.equal(foregrounds.length, 3);
  for (const surface of ["fallback-bg", "fallback-selected"]) {
    const backgrounds = colors(surface);
    assert.equal(backgrounds.length, foregrounds.length);
    foregrounds.forEach((foreground, index) => {
      const fg = luminance(foreground), bg = luminance(backgrounds[index]);
      const ratio = (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05);
      assert.ok(ratio >= 4.5, `${foreground} on ${backgrounds[index]}: ${ratio}`);
    });
  }
});

test("default Roadmap universe excludes closed, archived, nonmember issues and all PRs", () => {
  const s = snap([item(1), item(2, "issue", { state: "CLOSED" }), item(3), item(4)], [
    pr(10), pr(11, { state: "MERGED" }),
  ]);
  s.roadmap.nodes[2].isArchived = true;
  s.roadmap.nodes = s.roadmap.nodes.filter(n => n.content.number !== 4);
  const m = buildModel(s), projection = projectView(m.items, defaultView());
  assert.deepEqual(projection.visible.map(i => i.number), [1]);
  assert.equal(projection.horizonCounts.All, 1);
  assert.equal(Object.values(projection.queueCounts).reduce((a, b) => a + b, 0), 1);
  assert.equal(m.items.length, 6);
  assert.deepEqual(projectView(m.items, { ...defaultView(), scope: "roadmap-history" }).visible.map(i => i.number).sort(), [2, 3]);
  assert.equal(projectView(m.items, { ...defaultView(), scope: "repository-open" }).visible.length, 3);
  assert.equal(projectView(m.items, { ...defaultView(), scope: "repository-prs" }).visible.length, 2);
});

test("missing or partial board data never infers membership or silently means zero work", () => {
  const s = snap([item(1), item(2)], []);
  s.roadmap.nodes = [];
  s.roadmap.complete = false;
  s.sources.roadmap = { status: "error", error: "Unavailable" };
  let m = buildModel(s);
  assert.equal(projectView(m.items, defaultView()).universeTotal, 0);
  assert.equal(m.sources.roadmap.status, "error");
  assert.equal(m.items[0].boardKnown, false);
  s.roadmap.nodes = [{ id: "known", isArchived: false, content: { ...item(1), __typename: "Issue" }, fieldValues: connection([]) }];
  m = buildModel(s);
  assert.equal(projectView(m.items, defaultView()).universeTotal, 1);
  assert.equal(projectView(m.items, defaultView()).horizonCounts.Unset, 1);
  assert.equal(m.coverage.projectComplete, false);
  s.roadmap.nodes[0].isArchived = undefined;
  assert.equal(projectView(buildModel(s).items, defaultView()).universeTotal, 0);
});

test("Horizon and queue totals use the same issue universe and active facet filters", () => {
  const s = snap([item(1), item(2), item(3)], [pr(20, { body: "Partial work for #1" }), pr(21, { body: "Related to #1" })]);
  s.roadmap.nodes[1].fieldValues = connection([{ name: "Next", field: { name: "Horizon" } }]);
  s.roadmap.nodes[2].fieldValues = connection([]);
  const m = buildModel(s);
  let p = projectView(m.items, { ...defaultView(), horizon: "Now" });
  assert.equal(p.universeTotal, 3);
  assert.deepEqual(p.horizonCounts, { All: 3, Now: 1, Next: 1, Later: 0, Unset: 1 });
  assert.equal(p.filteredTotal, 1);
  assert.equal(Object.values(p.queueCounts).reduce((a, b) => a + b, 0), p.filteredTotal);
  assert.equal(p.visible[0].related.length, 2);
  p = projectView(m.items, { ...defaultView(), search: "work 2", person: "contributor" });
  assert.equal(p.horizonCounts.All, 1);
  assert.equal(p.horizonCounts.Next, 1);
  assert.equal(p.filteredTotal, 1);
});

test("schema-1 view migrates to decisions without showing an unverified selection", () => {
  const m = buildModel(snap([item(3015)], [pr(3057)]));
  const legacy = { schema: 1, data: { kind: "work", horizon: "All", search: "old", selectedId: itemId("issue", 3015) } };
  const good = restoreView(legacy, m.items);
  assert.equal(good.view.scope, "decisions");
  assert.equal(good.view.search, "");
  assert.equal(good.view.selectedId, null);
  assert.equal(good.migrated, true);
  assert.match(good.notice, /View updated/);
  const oldPr = restoreView({ ...legacy, data: { ...legacy.data, selectedId: itemId("pr", 3057) } }, m.items);
  assert.equal(oldPr.view.selectedId, null);
  assert.match(oldPr.notice, /No matching/);
  assert.equal(restoreView(legacy, []).view.selectedId, null);
});

test("persisted view migration and filter changes never leave a hidden selected PR or history item", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const seed = new Store({ root });
  await seed.save("snapshot.json", snap([item(3015), item(2, "issue", { state: "CLOSED" })], [pr()]));
  await writeFile(join(seed.directory, "view.json"), JSON.stringify({ schema: 1, data: { kind: "work", horizon: "All", selectedId: itemId("pr", 3057) } }));
  const store = await new Store({ root }).load();
  assert.equal(store.view.selectedId, null);
  assert.match(store.state().viewNotice, /No matching/);
  assert.equal(JSON.parse(await readFile(join(store.directory, "view.json"), "utf8")).schema, VIEW_SCHEMA);
  await store.setView({ scope: "roadmap-history" });
  assert.equal(store.view.selectedId, itemId("issue", 2));
  await store.setView({ scope: "roadmap-open", search: "no matching issue" });
  assert.equal(store.view.selectedId, null);
  assert.equal(store.state().brief, null);
  assert.match(store.state().viewNotice, /No matching/);
  assert.equal(store.state().projection.filteredTotal, 0);
  await assert.rejects(() => store.select(itemId("pr", 3057)), /outside the active scope/);
});

test("current-schema explicit history selection restores without another migration", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const seed = new Store({ root });
  seed.snapshot = snap([item(1, "issue", { state: "CLOSED" })], []);
  await seed.save("snapshot.json", seed.snapshot);
  await seed.setView({ scope: "roadmap-history", selectedId: itemId("issue", 1) });
  const restored = await new Store({ root }).load();
  assert.equal(restored.view.scope, "roadmap-history");
  assert.equal(restored.view.selectedId, itemId("issue", 1));
  assert.equal(restored.state().viewNotice, null);
});

test("rehydrated open input with a now-closed issue reconciles before open or after refresh", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const closed = snap([item(3015, "issue", { state: "CLOSED", stateReason: "COMPLETED" }), item(2928)], []);
  const store = new Store({ root, github: { portfolio: async () => closed } });
  store.view = defaultView();
  store.snapshot = closed;
  let state = await store.setView({ selectedId: itemId("issue", 3015) });
  assert.equal(state.view.selectedId, itemId("issue", 2928));
  assert.match(state.viewNotice, /Issue #3015 is outside/);
  assert.equal(state.brief.id, itemId("issue", 2928));
  store.snapshot = snap([item(3015), item(2928)], []);
  await store.setView({ selectedId: itemId("issue", 3015) });
  state = await store.refresh();
  assert.equal(state.view.selectedId, itemId("issue", 2928));
  assert.match(state.viewNotice, /Issue #3015 is outside/);
  assert.ok(state.projection.visibleIds.includes(state.view.selectedId));
  assert.equal(state.projection.visibleIds.includes(itemId("issue", 3015)), false);
});

test("labels, open PRs and Horizon never imply execution or authorization", () => {
  const s = snap([item(3015, "issue", { labels: connection([{ name: "status/accepted" }]) })]);
  const m = buildModel(s);
  for (const i of m.items) {
    assert.equal(i.execution.verified, false);
    assert.equal(i.authority.verified, false);
    assert.equal(i.queue, "preparing");
  }
  assert.equal(m.counts.active, 0);
  assert.equal(m.counts.authorized, 0);
});

test("paused verified agent has one queue, never active and needs-human together", () => {
  const i = { kind: "issue", state: "OPEN", labels: [], execution: { verified: true, state: "waiting-human" }, authority: { verified: true } };
  assert.equal(classify(i).queue, "needs-human");
  assert.equal(classify({ ...i, execution: { verified: true, state: "active" } }).queue, "active");
  assert.equal(classify({ ...i, execution: { verified: true, state: "waiting-capacity" } }).queue, "authorized");
});

test("nonclosing, partial, multi-PR references survive without closing an open issue", () => {
  const a = pr(3070, { body: "Partial fix for #2991. Related to #3015." });
  const b = pr(3057, { closingIssuesReferences: connection([{ number: 3015, repository: { nameWithOwner: REPO } }]) });
  const c = pr(3058, { state: "MERGED", body: "References #2991; this is partial." });
  const s = snap([item(2991), item(3015)], [a, b, c]);
  const m = buildModel(s);
  const issue = m.items.find(i => i.number === 2991);
  assert.equal(issue.state, "OPEN");
  assert.equal(issue.queue, "preparing");
  assert.equal(issue.related.length, 2);
  assert.equal(projectView(m.items, defaultView()).visible.length, 2);
  assert.equal(new Set(m.workIds).size, m.workIds.length);
  assert.equal(Object.values(m.counts).reduce((a, b) => a + b, 0), m.workIds.length);
  assert.equal(m.items.filter(i => i.kind === "pr").length, 3);
});

test("foreign repository refs do not become local links; mentions do not prove closing scope", () => {
  const links = relationships([pr(30, { body: "Fixes #10; octo/other#20; https://github.com/other/repo/issues/21; note #11." })]);
  assert.deepEqual(links.map(l => l.issue).sort((a, b) => a - b), [10, 11]);
  assert.equal(links.find(l => l.issue === 10).kind, "reference");
  assert.equal(links.find(l => l.issue === 11).kind, "mentioned");
});

test("action_required is not test failure; zero jobs does not imply approval", () => {
  const raw = pr();
  const r = record(raw, [], [{ head_sha: head, conclusion: "action_required", status: "completed" }]);
  const e = evidenceFor(raw, r);
  assert.equal(e.status, "action-required");
  assert.equal(e.failed, 0);
  assert.equal(evidenceFor(raw, record(raw)).status, "unknown");
});

test("real Windows failure supersedes historical gate and has no invented causal diagnosis", () => {
  const raw = pr();
  const r = record(raw, [{ name: "Windows Compatibility Gate", status: "COMPLETED", conclusion: "FAILURE" }]);
  const e = evidenceFor(raw, r);
  assert.equal(e.status, "failed");
  assert.equal(e.checks[0].platform, "Windows (job name)");
  assert.match(e.summary, /cause has not been diagnosed/);
});

test("final-head, freshness, partial and error evidence are explicit", () => {
  const raw = pr();
  const old = record(raw, [{ name: "test", conclusion: "SUCCESS", status: "COMPLETED" }]);
  assert.equal(evidenceFor({ ...raw, headRefOid: "b".repeat(40) }, old).status, "outdated-head");
  old.status = "stale";
  old.data.evidence.runs.complete = false;
  old.data.evidence.runs.error = "Read unavailable";
  const e = evidenceFor(raw, old);
  assert.equal(e.fresh, false);
  assert.equal(e.complete, false);
  assert.equal(e.error, "Read unavailable");
  const m = buildModel({ ...snap(), sources: { issues: { ...source(), observedAt: "2020-01-01T00:00:00Z" } } });
  assert.equal(m.sources.issues.status, "stale");
});

test("stale old-head record cannot overwrite a newer list head; history is separate", () => {
  const oldRaw = pr();
  const oldRecord = record(oldRaw, [{ name: "Windows", status: "COMPLETED", conclusion: "FAILURE" }]);
  oldRecord.status = "stale";
  const next = pr(3057, { headRefOid: "b".repeat(40) });
  const s = snap([], [next]);
  s.pulls[3057] = oldRecord;
  let i = buildModel(s).items[0];
  assert.equal(i.head, next.headRefOid);
  assert.equal(i.evidence.status, "outdated-head");
  assert.equal(i.queue, "preparing");
  const newRecord = record(next, [], [{ head_sha: next.headRefOid, status: "completed", conclusion: "action_required" }]);
  s.pulls[3057] = pullRecord(newRecord.data, oldRecord);
  i = buildModel(s).items[0];
  assert.equal(i.evidence.status, "action-required");
  assert.equal(i.history.length, 1);
  assert.equal(i.history[0].head, head);
  assert.equal(i.history[0].outcomes[0].conclusion, "FAILURE");
});

test("a newer run on the same head supersedes an older action-required run", () => {
  const raw = pr();
  const runs = [
    { workflow_id: 1, event: "pull_request", head_sha: head, status: "completed", conclusion: "action_required", created_at: "2026-01-01T00:00:00Z" },
    { workflow_id: 1, event: "pull_request", head_sha: head, status: "completed", conclusion: "success", created_at: "2026-01-02T00:00:00Z" },
  ];
  const e = evidenceFor(raw, record(raw, [{ name: "test", status: "COMPLETED", conclusion: "SUCCESS" }], runs));
  assert.equal(e.actionRequired, false);
  assert.equal(e.runs.length, 1);
  assert.equal(e.status, "observed-success");
});

test("neutral advisory and green checks never independently meet acceptance", () => {
  const raw = pr(3057, { autoMergeRequest: { enabledAt: stamp() } });
  const s = snap([], [raw]);
  s.pulls[3057] = record(raw, [{ name: "PR eligibility (advisory only)", status: "COMPLETED", conclusion: "NEUTRAL" }]);
  let i = buildModel(s).items[0];
  assert.equal(i.evidence.status, "mixed");
  assert.equal(i.queue, "preparing");
  assert.equal(i.autoMerge, "ON");
  s.pulls[3057] = record(raw, [{ name: "tests", status: "COMPLETED", conclusion: "SUCCESS" }]);
  i = buildModel(s).items[0];
  assert.equal(i.queue, "preparing");
});

test("success, neutral and skipped describe only observed outcomes and suggest review without reapproval", () => {
  const raw = pr(3057, { closingIssuesReferences: connection([{ number: 3015, repository: { nameWithOwner: REPO } }]) });
  const checks = ["SUCCESS", "NEUTRAL", "NEUTRAL", "SKIPPED"].map((conclusion, index) => ({ name: `Check ${index}`, status: "COMPLETED", conclusion }));
  const s = snap([item()], [raw]);
  s.pulls[3057] = record(raw, checks, [{ head_sha: head, name: "CI", status: "completed", conclusion: "success" }]);
  const m = buildModel(s);
  const p = m.items.find(i => i.kind === "pr");
  assert.equal(p.evidence.status, "mixed");
  assert.equal(p.evidence.summary, "Checks completed with no reported failures; 2 neutral and 1 skipped. Required-check coverage and acceptance are not verified by this prototype.");
  assert.equal(evidenceFor(raw, record(raw, [...checks].reverse())).summary, p.evidence.summary);
  assert.doesNotMatch(p.evidence.summary, /cancelled|passed/);
  assert.equal(p.queue, "preparing");
  assert.equal(p.authority.verified, false);
  for (const i of m.items) {
    const brief = makeBrief(i, m, s);
    assert.equal(brief.recommendation.title, "Open PR #3057 for your review");
    assert.match(brief.recommendation.tradeoff, /Do not repeat scope approval/);
    assert.match(brief.recommendation.tradeoff, /do not establish.*merge readiness/);
  }
});

test("cancelled checks and workflows are not described as completed without failures or passing", () => {
  const raw = pr();
  for (const workflowCancellation of [false, true]) {
    const s = snap([], [raw]);
    const checks = [{ name: "Tests", status: "COMPLETED", conclusion: workflowCancellation ? "SUCCESS" : "CANCELLED" }];
    const runs = workflowCancellation ? [{ name: "CI", head_sha: head, status: "completed", conclusion: "cancelled" }] : [];
    s.pulls[3057] = record(raw, checks, runs);
    const m = buildModel(s), p = m.items[0];
    assert.equal(p.evidence.status, "mixed");
    assert.match(p.evidence.summary, workflowCancellation ? /1 cancelled workflow run/ : /1 cancelled check/);
    assert.match(p.evidence.summary, /Cancellation is not a passing outcome/);
    assert.doesNotMatch(p.evidence.summary, /no reported failures|neutral|skipped/);
    assert.notEqual(makeBrief(p, m, s).recommendation.title, "Review completed checks and remaining requirements");
  }
});

test("failed checks and workflows remain separate from cancellation and benign extras", () => {
  const raw = pr();
  for (const workflowFailure of [false, true]) {
    const s = snap([], [raw]);
    const checks = [
      { name: "Windows", status: "COMPLETED", conclusion: workflowFailure ? "SUCCESS" : "FAILURE" },
      { name: "Other job", status: "COMPLETED", conclusion: "CANCELLED" },
    ];
    const runs = workflowFailure ? [{ name: "CI", head_sha: head, status: "completed", conclusion: "failure" }] : [];
    s.pulls[3057] = record(raw, checks, runs);
    const m = buildModel(s), p = m.items[0];
    assert.equal(p.evidence.status, "failed");
    assert.match(p.evidence.summary, /reported failure/);
    assert.match(p.evidence.summary, /1 cancelled check/);
    assert.match(p.evidence.summary, /cause has not been diagnosed/);
    assert.doesNotMatch(p.evidence.summary, /no reported failures/);
    assert.equal(makeBrief(p, m, s).recommendation.title, "Open failed checks for PR #3057");
  }
});

test("stale or partial benign check evidence does not trigger the completed-check recommendation", () => {
  const raw = pr();
  for (const partial of [false, true]) {
    const s = snap([], [raw]);
    s.pulls[3057] = record(raw, [{ name: "Tests", status: "COMPLETED", conclusion: "NEUTRAL" }]);
    if (partial) s.pulls[3057].data.evidence.checks.complete = false;
    else s.pulls[3057].status = "stale";
    const m = buildModel(s);
    assert.notEqual(makeBrief(m.items[0], m, s).recommendation.title, "Review completed checks and remaining requirements");
    assert.equal(m.items[0].queue, "preparing");
  }
});

test("closed without verified delivery is not shipped; merged PR does not close another entity", () => {
  const m = buildModel(snap([item(3015, "issue", { state: "CLOSED", stateReason: "NOT_PLANNED" })], [pr(3061, { state: "MERGED" })]));
  assert.equal(m.items.find(i => i.number === 3015).queue, "deferred");
  assert.equal(m.items.find(i => i.number === 3061).queue, "completed");
});

test("Roadmap Horizon, archived state and missing fields stay distinct", () => {
  const raw = item();
  const s = snap([raw], []);
  s.roadmap.nodes = [{ id: "board-id", updatedAt: stamp(), isArchived: true,
    fieldValues: connection([{ name: "Now", field: { name: "Horizon" } }]),
    content: { ...raw, __typename: "Issue" } }];
  const i = buildModel(s).items[0];
  assert.equal(i.horizon, "Now");
  assert.equal(i.placement.archived, true);
  assert.equal(i.queue, "preparing");
});

test("brief distinguishes optional advisory, scope observation and execution; source labels are readable", () => {
  const s = snap();
  s.details = { 3015: { observedAt: stamp(), data: { ...item(), discussion: [
    { body: "<!-- apm-scope:v1 --> Decision: approve", author: "maintainer", url: `${GH}/issues/3015#issuecomment-1` },
    { body: "Optional advisory recommendation", author: "reviewer", url: `${GH}/issues/3015#issuecomment-2` },
  ] } } };
  const m = buildModel(s), b = makeBrief(m.items[0], m, s);
  assert.match(b.problem, /downloaded source/);
  assert.equal(b.scope.length, 1);
  assert.equal(b.advice.length, 1);
  assert.match(b.scope[0].label, /not authority-validated/);
  assert.match(b.wouldNotDo, /Reading or selecting never changes GitHub/);
  assert.match(b.wouldDo, /separate exact account, commit and run preview plus explicit confirmation/);
  assert.match(b.wouldNotDo, /Other agent and repository operations retain their existing parent gates/);
  assert.ok(b.sources.every(s => safeUrl(s.url)));
});

test("safe URLs, token redaction and stable domain IDs reject hostile inputs", () => {
  for (const u of ["javascript:alert(1)", "https://github.com.evil.test/microsoft/apm/issues/1", "https://x@github.com/microsoft/apm/issues/1", "http://github.com/microsoft/apm/issues/1", "https://github.com/other/private/issues/1"]) assert.equal(safeUrl(u), null);
  assert.throws(() => parseId("../../etc/passwd"), /Invalid/);
  assert.equal(parseId("microsoft/apm/pr/3057").number, 3057);
  assert.equal(sanitize({ body: "ghp_abcdefghijklmnopqrstuvwxyz012345" }).body, "[redacted credential]");
});

test("GraphQL pagination reads all pages with constant read-only queries", async () => {
  const calls = [];
  const gh = new Github(async args => {
    calls.push(args);
    return { data: { repository: { issues: {
      nodes: [item(calls.length)], totalCount: 2,
      pageInfo: { hasNextPage: calls.length === 1, endCursor: calls.length === 1 ? "page2" : null },
    } } } };
  });
  const result = await gh.connection("issues", d => d.repository.issues);
  assert.equal(result.nodes.length, 2);
  assert.equal(result.complete, true);
  assert.equal(result.pages, 2);
  assert.ok(calls.every(args => args.includes("graphql") && args.some(a => a.startsWith("query=query")) && !args.some(a => /mutation\s*[{(]/.test(a))));
  await assert.rejects(() => gh.query("mutation"), /Unsupported/);
  await assert.rejects(() => gh.rest("dispatch", 1), /Unsupported/);
  await assert.rejects(() => gh.rest("runs", "x; touch bad"), /Unsupported/);
});

test("API failures retain stale previous sources, never empty success queues", async () => {
  const gh = new Github(async () => { throw new Error("API unavailable"); });
  const previous = snap([item()], []);
  const updated = await gh.portfolio(previous);
  assert.equal(updated.issues.nodes.length, 1);
  assert.equal(updated.sources.issues.status, "stale");
  assert.equal(updated.sources.roadmap.status, "stale");
  const empty = await gh.portfolio({});
  assert.equal(empty.sources.issues.status, "error");
  assert.equal(buildModel(empty).items.length, 0);
});

test("pagination safety cap is partial, never silently complete", async () => {
  let page = 0;
  const gh = new Github(async () => ({
    data: { repository: { issues: {
      nodes: [item(++page)], totalCount: 200,
      pageInfo: { hasNextPage: true, endCursor: `cursor${page}` },
    } } },
  }));
  const result = await gh.connection("issues", d => d.repository.issues);
  assert.equal(result.complete, false);
  assert.equal(result.nodes.length, 100);
  assert.equal(result.total, 200);
});

test("refresh coalesces concurrent callers and enforces the local read budget", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  let calls = 0;
  const store = new Store({ root, github: { portfolio: async () => { calls++; await new Promise(r => setTimeout(r, 10)); return snap(); } } });
  await Promise.all([store.refresh(), store.refresh()]);
  await store.refresh();
  assert.equal(calls, 1);
  assert.equal(store.state().refreshing, false);
});

test("concurrent selections run alongside refresh and preserve each stable brief ID", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  let active = 0;
  let maximum = 0;
  const read = async value => {
    active++; maximum = Math.max(maximum, active);
    await new Promise(r => setTimeout(r, 5));
    active--;
    return value;
  };
  const store = new Store({ root, github: {
    selectedIssue: number => read({ ...item(number), timelineReferences: [] }),
    portfolio: previous => read(previous),
  } });
  store.snapshot = snap([item(3015), item(2991)], []);
  store.view = defaultView();
  await Promise.all([store.select(itemId("issue", 3015), { wait: true }), store.select(itemId("issue", 2991), { wait: true }), store.refresh()]);
  assert.ok(maximum > 1 && maximum <= 3);
  for (const number of [3015, 2991]) {
    const brief = JSON.parse(await readFile(join(store.directory, `briefs/issue-${number}.json`), "utf8"));
    assert.equal(brief.data.id, itemId("issue", number));
  }
  assert.equal(store.state().view.selectedId, itemId("issue", 2991));
});

test("head change during evidence read rejects old completion", async () => {
  let queryCount = 0;
  const gh = new Github(async args => {
    if (args.includes("GET")) return { workflow_runs: [], total_count: 0 };
    queryCount++;
    const raw = pr(3057, { headRefOid: queryCount === 1 ? head : "b".repeat(40),
      commits: { nodes: [{ commit: { oid: queryCount === 1 ? head : "b".repeat(40), statusCheckRollup: { contexts: connection([]) } } }] } });
    return { data: { repository: { pullRequest: raw } } };
  });
  await assert.rejects(() => gh.pull(3057), /head changed/);
});

test("HTTP loads real controls, rejects mutation paths and invalid input, persists selection", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = new Store({ root, github: { selectedIssue: async () => ({ ...item(), discussion: [], timelineReferences: [] }), portfolio: async s => s } });
  store.snapshot = snap([item()], []);
  const server = await startServer(store);
  t.after(() => server.close());
  const html = await (await fetch(server.url)).text();
  assert.match(html, /Roadmap planning/);
  assert.match(html, /Selected work item/);
  assert.match(html, /id="workflow-nav"/);
  assert.doesNotMatch(html, /id="scope"/);
  assert.doesNotMatch(html, /id="kind"/);
  assert.match(html, /script type="module" src="\/app.js"/);
  const token = /name="canvas-token" content="([^"]+)"/.exec(html)[1];
  const post = (path, input, header = token) => fetch(new URL(path, server.url), { method: "POST", headers: { "Content-Type": "application/json", "X-Canvas-Token": header }, body: JSON.stringify(input) });
  for (const path of ["/api/dispatch", "/api/resume", "/api/merge", "/api/approve", "/api/exec"]) assert.equal((await post(path, {})).status, 404);
  assert.equal((await post("/api/select", { id: "bad" })).status, 400);
  assert.equal((await post("/api/refresh", { command: "anything" })).status, 400);
  assert.equal((await post("/api/refresh", {}, "wrong")).status, 403);
  assert.equal((await post("/api/view", { repo: "other/repo" })).status, 400);
  assert.equal((await post("/api/view", { kind: "work" })).status, 400);
  assert.equal((await post("/api/view", { scope: "unknown" })).status, 400);
  const history = await (await post("/api/view", { scope: "roadmap-history" })).json();
  assert.equal(history.projection.filteredTotal, 0);
  assert.equal(history.view.selectedId, null);
  assert.equal(history.brief, null);
  assert.equal((await post("/api/select", { id: itemId("issue", 3015) })).status, 400);
  const open = await (await post("/api/view", { scope: "roadmap-open" })).json();
  assert.equal(open.projection.filteredTotal, 1);
  assert.equal(open.view.selectedId, itemId("issue", 3015));
  assert.equal((await post("/api/view", { horizon: "Now" })).status, 200);
  assert.equal((await post("/api/select", { id: itemId("issue", 3015) })).status, 202);
  assert.equal((await post("/api/refresh", {})).status, 202);
  assert.equal((await fetch(new URL("app.js", server.url))).status, 200);
  assert.equal((await fetch(new URL("style.css", server.url))).status, 200);
  assert.equal((await fetch(new URL("scope.mjs", server.url))).status, 200);
  assert.equal((await fetch(new URL("model.mjs", server.url))).status, 404);
  assert.equal((await fetch(new URL("api/state", server.url), { headers: { Origin: "https://evil.test" } })).status, 403);
  await store.pending;
  await store.waitSelected(itemId("issue", 3015));
  const loaded = await new Store({ root }).load();
  assert.equal(loaded.view.selectedId, itemId("issue", 3015));
  assert.equal(loaded.view.horizon, "Now");
  assert.ok((await readFile(join(store.directory, "briefs/issue-3015.json"), "utf8")).includes("source-grounded") || loaded.snapshot.details[3015]);
});

test("corrupt local cache and failed selected reads are explicit and keep last-known data", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = new Store({ root, github: { selectedIssue: async () => { throw new Error("Discussion access unavailable"); } } });
  await mkdir(store.directory, { recursive: true });
  await writeFile(join(store.directory, "snapshot.json"), "invalid");
  await store.load();
  assert.match(store.error, /could not be loaded/);
  store.snapshot = snap();
  store.view = defaultView();
  const state = await store.select(itemId("issue", 3015), { wait: true });
  assert.match(state.detailError, /Discussion access unavailable/);
  assert.equal(state.items.find(i => i.number === 3015).state, "OPEN");
});

test("partially paginated discussion stays partial and cached selection persists a fresh brief", async t => {
  const root = await directory();
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = new Store({ root, github: { selectedIssue: async () => ({ ...item(), timelineReferences: [], detailCoverage: { comments: false, timeline: true } }) } });
  store.snapshot = snap();
  store.view = defaultView();
  const selected = await store.select(itemId("issue", 3015), { wait: true });
  assert.equal(selected.detailStatus, "partial");
  store.snapshot.details[3015].status = "current";
  const cached = await store.select(itemId("issue", 3015), { wait: true });
  const persisted = JSON.parse(await readFile(join(store.directory, "briefs/issue-3015.json"), "utf8"));
  assert.equal(persisted.data.recommendation.title, cached.brief.recommendation.title);
  assert.equal(persisted.data.id, itemId("issue", 3015));
});

test("extension only relays to parent and adapters expose no privileged execution", async () => {
  for (const file of ["extension.mjs", "server.mjs", "github.mjs", "store.mjs"]) {
    const source = await readFile(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /\bsession\.rpc|\bmutation\s*[{(]|workflow_dispatch|\/rerun|execSync|shell:\s*true|onPermissionRequest|systemMessage/);
  }
  const source = await readFile(new URL("extension.mjs", import.meta.url), "utf8");
  assert.deepEqual([...source.matchAll(/name: "(get_state|refresh|select)"/g)].map(m => m[1]), ["get_state", "refresh", "select"]);
  assert.doesNotMatch(source, /name: "canvas\./);
});
