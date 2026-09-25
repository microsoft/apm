import { createRequire } from "node:module";
import { Governance, reconcileIssue, TRUSTED_ROOT } from "./governance.mjs";
import { Store } from "./store.mjs";
import { Bridge } from "./bridge.mjs";
import { defaultView } from "./scope.mjs";
const require = createRequire(import.meta.url);
export const authority = require(`${TRUSTED_ROOT}/scripts/governance/authority.cjs`);
export const eligibility = require(`${TRUSTED_ROOT}/scripts/governance/eligibility.cjs`);
export const now = () => new Date().toISOString();
const ago = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const connection = nodes => ({ nodes, totalCount: nodes.length, pageInfo: { hasNextPage: false } });
export const policy = authority.readPolicy(`<!-- apm-scope-roster:start -->
| [Maintainer](https://github.com/fixture-maintainer) | Role | Team | \`project\` |
| [Reviewer](https://github.com/fixture-reviewer) | Role | Team | \`project\` |
<!-- apm-scope-roster:end -->
<!-- apm-scope-reset-before:2026-01-01T00:00:00Z -->`);
export const trustFixture = () => ({ authority, eligibility, policy, sha: "a".repeat(40),
  actor: { login: "fixture-maintainer", type: "User", responsible: true, canWrite: true, observedAt: now() } });
export const scopeFields = { area: "project", scope: "Repair only fixture behavior.", doneWhen: "Fixture checks demonstrate the agreed behavior.",
  outOfScope: "No unrelated redesign or production mutations.", reviewContact: "@fixture-reviewer" };
export function scopeBody(decision = "approve") {
  return `${authority.MARKER}\nDecision: ${decision}\nArea: project\n${decision === "withdraw" ? "Reason: Fixture withdrawal." :
    `Scope: ${scopeFields.scope}\nDone when: ${scopeFields.doneWhen}\nOut of scope: ${scopeFields.outOfScope}\nReview contact: ${scopeFields.reviewContact}`}`;
}
export function comment(id, body, author = "fixture-maintainer", age = 1) {
  const timestamp = ago(age);
  return { id, body, user: { login: author, type: "User" }, created_at: timestamp, updated_at: timestamp,
    html_url: `https://github.com/microsoft/apm/issues/1#issuecomment-${id}` };
}
export function fixtureSnapshot() {
  const stamp = now(), trust = trustFixture();
  const issues = Array.from({ length: 7 }, (_, index) => ({
    number: index + 1, title: ["[Fixture] Preserve nested skill links", "[Fixture] Accepted cleanup scope", "[Fixture] Planned without verified scope",
      "[Fixture] Contributor delivery with partial PR", "[Fixture] Parked proposal with old advice", "[Fixture] Completed historical issue", "[Fixture] Accepted design question"][index],
    body: "A controlled fixture demonstrates the maintainer workflow. It is not a live issue and must never be written to GitHub.",
    state: index === 5 ? "CLOSED" : "OPEN", stateReason: index === 5 ? "COMPLETED" : null,
    url: `https://github.com/microsoft/apm/issues/${index + 1}`, repository: { nameWithOwner: "microsoft/apm" },
    updatedAt: stamp, author: { login: "fixture-contributor" }, assignees: connection([]),
    labels: connection([...(index === 1 || index === 3 || index === 6 ? [{ name: "status/accepted" }] : []),
      ...(index === 4 ? [{ name: "status/deferred" }] : []), { name: "triage/recommended" }]),
  }));
  const records = {};
  for (const issue of issues) {
    const raw = { number: issue.number, updated_at: stamp, state: issue.state.toLowerCase(), labels: issue.labels.nodes };
    const comments = [comment(100 + issue.number, `Existing advice.\n<!-- apm-triage-advisory:v2 target=issue#${issue.number} watermark=fixture -->`, "fixture-adviser", 10)];
    if ([2, 4, 7].includes(issue.number)) comments.push(comment(200 + issue.number, scopeBody(), "fixture-maintainer", 5));
    const timeline = [5, 7].includes(issue.number) ? [{ id: 300 + issue.number, event: "labeled",
      label: { name: issue.number === 5 ? "status/deferred" : "status/needs-design" }, actor: { login: "fixture-maintainer", type: "User" }, created_at: ago(2) }] : [];
    if (issue.number === 7) raw.labels.push({ name: "status/needs-design" });
    records[issue.number] = reconcileIssue({ issue: raw, comments, timeline, trust });
  }
  const prs = [20, 21].map(number => ({
    number, title: `[Fixture] ${number === 20 ? "Partial contribution for issue #4" : "Unlinked contribution"}`,
    body: number === 20 ? "References #4; only a partial fix." : "Admission assessment fixture.",
    state: "OPEN", updatedAt: stamp, url: `https://github.com/microsoft/apm/pull/${number}`, repository: { nameWithOwner: "microsoft/apm" },
    author: { login: "fixture-contributor" }, assignees: connection([]), labels: connection([]), headRefOid: "b".repeat(40),
    reviewRequests: connection([]), closingIssuesReferences: connection([]), autoMergeRequest: null,
  }));
  const source = count => ({ status: "current", observedAt: stamp, total: count, count, pages: 1 });
  return { fetchedAt: stamp, sources: { issues: source(issues.length), prs: source(prs.length), roadmap: source(issues.length) },
    issues: { nodes: issues, complete: true }, prs: { nodes: prs, complete: true }, pulls: {}, details: {},
    roadmap: { project: { title: "APM Roadmap", url: "https://github.com/orgs/microsoft/projects/2304" },
      complete: true, nodes: issues.map(i => ({ id: `fixture-board-${i.number}`, updatedAt: stamp, isArchived: false,
        content: { ...i, __typename: "Issue" }, fieldValues: connection([3, 4].includes(i.number) ? [{ name: "Now", field: { name: "Horizon" } }] : []) })) },
    lifecycle: { status: "current", actor: trust.actor, trustedSha: trust.sha, issues: records, complete: true, observedAt: stamp } };
}
export function fixtureStore(root, { send = async () => {}, fixtureOnly = false } = {}) {
  const snapshot = fixtureSnapshot();
  const validator = new Governance({});
  validator.trust = async () => trustFixture();
  const governance = { prepare: async () => snapshot.lifecycle, trust: validator.trust,
    issue: async number => snapshot.lifecycle.issues[number], validateScope: fields => validator.validateScope(fields),
    validateWithdrawal: fields => validator.validateWithdrawal(fields) };
  const github = {
    portfolio: async () => snapshot,
    selectedIssue: async number => ({ ...snapshot.issues.nodes.find(i => i.number === number), discussion: [], timelineReferences: [], detailCoverage: { comments: true, timeline: true } }),
    pull: async number => snapshot.prs.nodes.find(p => p.number === number),
    selectedDiscussion: async () => ({ discussion: [], complete: true, observedAt: now() }),
  };
  const store = new Store({ root, github, governance });
  store.snapshot = snapshot; store.view = { ...defaultView(), selectedId: "microsoft/apm/issue/1" };
  store.bridge = new Bridge(store, { send, fixtureOnly });
  return store;
}
