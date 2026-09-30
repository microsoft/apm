'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const authority = require('../../scripts/governance/authority.cjs');
const eligibility = require('../../scripts/governance/eligibility.cjs');
const runner = require('../../scripts/governance/run.cjs');

const repository = 'microsoft/apm';
const root = path.resolve(__dirname, '../..');
const policyText = fs.readFileSync(path.join(root, 'GOVERNANCE.md'), 'utf8');
const policy = authority.readPolicy(policyText);
const sha = 'a'.repeat(40);
const baseSha = 'b'.repeat(40);
const approvalUrl = 'https://github.com/microsoft/apm/issues/2960#issuecomment-501';
const record = `${authority.MARKER}
Decision: approve
Area: project
Scope: A bounded governance change.
Done when: Focused regressions and a reviewable PR.
Out of scope: Merges and other issue approval.
Review contact: @danielmeppiel`;

function comment(overrides = {}) {
  return { id: 501, body: record, user: { login: 'danielmeppiel', type: 'User' },
    created_at: '2026-09-12T01:00:00Z', updated_at: '2026-09-12T01:00:00Z', ...overrides };
}
function issue(overrides = {}) { return { id: 1, number: 2960, state: 'open', ...overrides }; }
function pr(overrides = {}) {
  return { id: 2, number: 3000, state: 'open', body: `Partial work for #2960.\nApproval: ${approvalUrl}`,
    head: { sha, repo: { full_name: 'contributor/apm' } },
    base: { sha: 'c'.repeat(40), repo: { full_name: repository }, ref: 'untrusted-stack' },
    user: { login: 'contributor', type: 'User' }, ...overrides };
}
function evaluate(overrides = {}) {
  return authority.evaluateIssue({ policy, repository, issue: issue(),
    comments: [comment()], approvalUrl, ...overrides });
}
function api(overrides = {}) {
  const calls = [];
  const data = {
    [`/repos/${repository}`]: { default_branch: 'main' },
    [`/repos/${repository}/commits/main`]: { sha: baseSha },
    [`/repos/${repository}/contents/GOVERNANCE.md?ref=${baseSha}`]:
      { type: 'file', encoding: 'base64', content: Buffer.from(policyText).toString('base64') },
    [`/repos/${repository}/issues/2960`]: issue(),
    [`/repos/${repository}/issues/2960/comments?per_page=100&page=1`]: [comment()],
    [`/repos/${repository}/pulls/3000`]: pr(),
    ...overrides,
  };
  return { calls, async get(route) {
    calls.push(route);
    assert.ok(Object.hasOwn(data, route), `Unexpected request ${route}`);
    if (data[route] instanceof Error) throw data[route];
    return typeof data[route] === 'function' ? data[route]() : data[route];
  } };
}
function httpError(status) { return Object.assign(new Error('Confidential response must not be printed'), { status }); }
function environment(client, eventName = 'pull_request_target', payload = { pull_request: pr() }) {
  const writes = [];
  const failures = [];
  const summaries = [];
  const summary = {
    addHeading(value) { summaries.push(value); return this; },
    addRaw(value) { summaries.push(value); return this; },
    async write() { return this; },
  };
  return { writes, failures, summaries,
    github: {
      request: async route => ({ data: await client.get(route.slice(4)) }),
      rest: { checks: { create: async value => { writes.push(value); } } },
    },
    context: { repo: { owner: 'microsoft', repo: 'apm' }, eventName, payload },
    core: { summary, setFailed: value => failures.push(value) },
    trustedSha: baseSha,
  };
}

test('roster comes from the governance table and preserves narrow remit', () => {
  assert.equal(policy.roster.get('danielmeppiel'), 'project');
  assert.equal(policy.roster.get('sergio-sisternes-epam'), 'project');
  assert.equal(policy.roster.get('nadav-y'), 'registry-public-api');
  assert.throws(() => authority.readPolicy(policyText.replace('`registry-public-api` |', '`registry` |')));
  assert.throws(() => authority.readPolicy('No roster'));
  assert.throws(() => authority.readPolicy(policyText + '\n<!-- apm-scope-reset-before:2027-01-01T00:00:00Z -->'));
});

test('record presence is never reusable permission', () => {
  const result = evaluate();
  assert.equal(result.state, 'record-present');
  assert.equal(result.authorizes_implementation, false);
  assert.equal(result.approver, 'danielmeppiel');
  assert.equal(result.contact_confirmation_needed, false);
});

test('Sergio needs no lead ratification and other contacts need confirmation', () => {
  const result = evaluate({ comments: [comment({ user: { login: 'sergio-sisternes-epam', type: 'User' } })] });
  assert.equal(result.state, 'record-present');
  assert.equal(result.contact_confirmation_needed, true);
});

test('Nadav public API remit is explicit and cannot be expanded by labels', () => {
  const nadav = comment({ user: { login: 'nadav-y', type: 'User' } });
  assert.equal(evaluate({ comments: [nadav], issue: issue({ labels: ['registry', 'mcp', 'oci'] }) }).state, 'needs-evidence');
  nadav.body = record.replace('Area: project', 'Area: registry-public-api')
    .replace('Review contact: @danielmeppiel', 'Review contact: @nadav-y');
  assert.equal(evaluate({ comments: [nadav] }).state, 'record-present');
  assert.equal(evaluate({ comments: [comment({ body: record.replace('@danielmeppiel', '@nadav-y') })] }).state,
    'needs-evidence');
});

test('bots, code reviews, labels and informal manual comments cannot approve', () => {
  for (const candidate of [
    comment({ user: { login: 'danielmeppiel', type: 'Bot' } }),
    comment({ user: { login: 'outside-user', type: 'User' }, author_association: 'OWNER' }),
    comment({ body: 'LGTM, accepted. APPROVED' }),
  ]) {
    assert.equal(evaluate({ comments: [candidate],
      issue: issue({ labels: ['status/accepted'], milestone: { title: 'Now' } }) }).state, 'needs-evidence');
  }
});

test('strict records reject quotes, duplicate keys, unknown fields and missing criteria', () => {
  for (const body of [
    `Quoted example:\n${record}`, record + '\nDecision: approve',
    record + '\nAuthorization: true', record.replace('Done when: Focused regressions and a reviewable PR.', ''),
    record.replace('Out of scope: Merges and other issue approval.', ''),
    record.replace('Out of scope: Merges and other issue approval.', 'Out of scope:   '),
  ]) assert.equal(authority.parseRecord(body), null);
  assert.ok(authority.parseRecord(record.replace('Merges and other issue approval.', 'None')));
});

test('approval edits and deletion never fall back to an older record', () => {
  assert.equal(evaluate({ comments: [comment({ updated_at: '2026-09-12T02:00:00Z' })] }).state, 'needs-evidence');
  assert.equal(evaluate({ comments: [comment({ id: 500 })] }).state, 'needs-evidence');
  assert.equal(evaluate({ changed: true }).state, 'needs-evidence');
});

test('reset floor works even without the historical reset comment', () => {
  assert.equal(evaluate({ comments: [comment({
    created_at: '2026-09-11T20:00:00Z', updated_at: '2026-09-11T20:00:00Z' })] }).state, 'withdrawn');
  assert.equal(evaluate({ comments: [comment(), comment({ id: 502, body: authority.RESET,
    created_at: '2026-09-12T02:00:00Z', updated_at: '2026-09-12T02:00:00Z' })] }).state, 'withdrawn');
});

test('withdrawals win and edited/malformed human decisions need fresh evidence', () => {
  const withdrawal = comment({ id: 502, body: `${authority.MARKER}\nDecision: withdraw\nArea: project\nReason: Capacity.`,
    created_at: '2026-09-12T02:00:00Z', updated_at: '2026-09-12T02:00:00Z' });
  assert.equal(evaluate({ comments: [comment(), withdrawal] }).state, 'withdrawn');
  assert.equal(evaluate({ comments: [comment(), { ...withdrawal, body: authority.MARKER + '\nMaybe' }] }).state, 'needs-evidence');
  // Deletion is not reconstructible. The remaining record is still NEVER permission.
  assert.equal(evaluate({ comments: [comment()] }).authorizes_implementation, false);
});

test('limited-remit withdrawals cannot revoke project-wide evidence', () => {
  const withdrawal = comment({ id: 502,
    user: { login: 'nadav-y', type: 'User' },
    body: `${authority.MARKER}\nDecision: withdraw\nArea: registry-public-api\nReason: Capacity.`,
    created_at: '2026-09-12T02:00:00Z', updated_at: '2026-09-12T02:00:00Z' });
  assert.equal(evaluate({ comments: [comment(), withdrawal] }).state, 'record-present');
  const registryRecord = comment({ body: record.replace('Area: project', 'Area: registry-public-api') });
  assert.equal(evaluate({ comments: [registryRecord, withdrawal] }).state, 'withdrawn');
  const overbroad = { ...withdrawal, body: withdrawal.body.replace('Area: registry-public-api', 'Area: project') };
  assert.equal(evaluate({ comments: [registryRecord, overbroad] }).state, 'record-present');
});

test('malformed metadata is unknown, never an empty successful snapshot', () => {
  assert.throws(() => evaluate({ comments: null }));
  assert.throws(() => evaluate({ comments: [comment({ created_at: null })] }));
  assert.throws(() => evaluate({ comments: [comment({ user: null })] }));
  assert.throws(() => evaluate({ issue: issue({ pull_request: {} }) }));
});

test('comment URLs require exact host, repository, issue and comment identity', () => {
  assert.deepEqual(authority.commentReference(approvalUrl, repository), { issue: 2960, comment: 501 });
  for (const value of [
    'https://github.com.attacker.invalid/microsoft/apm/issues/2960#issuecomment-501',
    'https://github.com/other/apm/issues/2960#issuecomment-501',
    'https://github.com/microsoft/apm/pull/2960#issuecomment-501',
    'https://github.com/microsoft/apm/issues/2960?different=true#issuecomment-501',
  ]) assert.equal(authority.commentReference(value, repository), null);
});

test('ordinary partial, explicit URLs and stacked references need no closing keywords', () => {
  const parsed = eligibility.references(
    `Partial work for #2960; microsoft/apm#2961. https://github.com/microsoft/apm/issues/2962\n${approvalUrl}`, repository);
  assert.deepEqual(parsed.issues.sort(), [2960, 2961, 2962]);
  assert.deepEqual(parsed.approvals.get(2960), [approvalUrl]);
  assert.deepEqual(eligibility.references(`${approvalUrl}\n${approvalUrl}`, repository).approvals.get(2960), [approvalUrl]);
});

test('upstream refs, HTML/Markdown captions, quoted snippets and PR URLs do not become local issues', () => {
  const parsed = eligibility.references([
    'https://github.com/upstream/tool/issues/123',
    'upstream/tool#124',
    '[#125](https://github.com/upstream/tool/issues/125)',
    '<a href="https://redirect.github.com/upstream/tool/issues/126">#126</a>',
    'https://github.com/microsoft/apm/pull/127',
    '`#128`', '> #129', '```text\n#130\n```',
  ].join('\n'), repository);
  assert.deepEqual(parsed.issues, []);
});

test('hidden comments cannot nominate any form of issue or approval evidence', () => {
  const parsed = eligibility.references(
    `<!-- #12 microsoft/apm#13 https://github.com/microsoft/apm/issues/14 ${approvalUrl} -->\nVisible #15.`,
    repository);
  assert.deepEqual(parsed.issues, [15]);
  assert.equal(parsed.approvals.size, 0);
});

test('trusted gh resolution excludes project PATH entries and symlink targets', async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'governance-gh-'));
  try {
    const project = path.join(temp, 'project');
    const trusted = path.join(temp, 'trusted');
    const linkdir = path.join(temp, 'links');
    for (const directory of [project, trusted, linkdir]) fs.mkdirSync(directory);
    const cwd = path.join(project, 'nested');
    fs.mkdirSync(cwd);
    fs.writeFileSync(path.join(project, '.git'), 'gitdir: irrelevant');
    const executable = process.platform === 'win32' ? 'gh.exe' : 'gh';
    const hostile = path.join(project, executable);
    fs.writeFileSync(hostile, 'not a trusted executable', { mode: 0o755 });
    const real = path.join(trusted, executable);
    fs.writeFileSync(real, 'trusted test executable', { mode: 0o755 });
    if (process.platform !== 'win32') fs.symlinkSync(hostile, path.join(linkdir, executable));
    const env = { PATH: [project, linkdir, trusted].join(path.delimiter) };
    assert.equal(eligibility.getGhExecutable({ cwd, env }), fs.realpathSync(real));
    const commands = [];
    const client = eligibility.ghClient({ cwd, env, execute: (file, args, options) => {
      commands.push({ file, args, options });
      return '{"read":true}';
    } });
    assert.deepEqual(await client.get('/metadata'), { read: true });
    assert.equal(commands[0].file, fs.realpathSync(real));
    assert.equal(commands[0].args.at(-1), '/metadata');
    assert.equal(commands[0].options.timeout, 15000);
    assert.throws(() => eligibility.getGhExecutable({ cwd,
      env: { PATH: ['', project, linkdir].join(path.delimiter) } }), /trusted PATH/);
    fs.unlinkSync(path.join(project, '.git'));
    fs.writeFileSync(path.join(project, 'apm.yml'), 'name: test');
    assert.equal(eligibility.getGhExecutable({ cwd, env }), fs.realpathSync(real));
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
});

test('reference limit is checked before issue API fanout', async () => {
  assert.deepEqual(eligibility.LIMITS,
    { references: 25, pages: 10, targets: 100, requests: 500, bytes: 32 * 1024 * 1024 });
  const atLimit = Array.from({ length: eligibility.LIMITS.references }, (_, i) => `#${i + 1}`);
  assert.equal(eligibility.references(atLimit.join(' '), repository).issues.length, atLimit.length);
  const client = api();
  const result = await eligibility.evaluatePull({ client, repository, policy,
    pr: pr({ body: [...atLimit, `#${atLimit.length + 1}`].join(' ') }) });
  assert.equal(result.state, 'error');
  assert.equal(result.authorizes_implementation, false);
  assert.deepEqual(result.issues, []);
  assert.deepEqual(client.calls, []);
});

test('pagination and shared operation budgets stop without partial evidence', async () => {
  let pages = 0;
  const manyPages = { get: async () => {
    const start = pages++ * 100;
    return Array.from({ length: 100 }, (_, i) => ({ id: start + i }));
  } };
  await assert.rejects(eligibility.listAll(manyPages, '/items'), /page limit/);
  assert.equal(pages, eligibility.LIMITS.pages);
  pages = 0;
  const completedPages = { get: async () => {
    const start = pages++ * 100;
    return Array.from({ length: pages === eligibility.LIMITS.pages ? 99 : 100 }, (_, i) => ({ id: start + i }));
  } };
  assert.equal((await eligibility.listAll(completedPages, '/items')).length, eligibility.LIMITS.pages * 100 - 1);
  const original = api({ [`/repos/${repository}/issues/2961`]: issue({ number: 2961 }) });
  const client = eligibility.boundedClient({ get: route => route.startsWith('/burn/')
    ? Promise.resolve(null) : original.get(route) });
  for (let i = 0; i < eligibility.LIMITS.requests - 3; i++) await client.get(`/burn/${i}`);
  const result = await eligibility.evaluatePull({ client, repository, policy,
    pr: pr({ body: `${approvalUrl}\nAlso #2961.` }) });
  assert.equal(result.state, 'error');
  assert.deepEqual(result.issues, []);
  assert.equal(original.calls.length, 3);
});

test('operation-local cache cannot accumulate unbounded response bodies', async () => {
  let calls = 0;
  const client = eligibility.boundedClient({ get: async () => {
    calls++;
    return 'x'.repeat(eligibility.LIMITS.bytes);
  } });
  await assert.rejects(client.get('/large'), /byte limit/);
  await assert.rejects(client.get('/later'), /byte limit/);
  assert.equal(calls, 1);
});

test('event target limit is enforced before any assessment fanout', async () => {
  const values = Array.from({ length: eligibility.LIMITS.targets + 1 },
    (_, id) => pr({ id, number: id + 1 }));
  const route = `/repos/${repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page=`;
  const client = api({ [route + 1]: values.slice(0, 100), [route + 2]: values.slice(100) });
  await assert.rejects(runner.targets(client, repository, 'push', {}), /target limit/);
  assert.equal(client.calls.length, 2);
  const exactly = api({ [route + 1]: values.slice(0, eligibility.LIMITS.targets), [route + 2]: [] });
  assert.equal((await runner.targets(exactly, repository, 'push', {})).length, eligibility.LIMITS.targets);
  const currentSize = Array.from({ length: 62 },
    (_, id) => pr({ id, number: id + 1, body: id < 2 ? '#2960' : '#100' }));
  const normal = api({ [`/repos/${repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page=1`]: currentSize });
  assert.deepEqual(await runner.targets(normal, repository, 'issues', { issue: issue() }), [1, 2]);
  assert.equal(normal.calls.length, 1);
});

test('a full 62-PR policy refresh fits the budget and caches only operation-local evidence', async () => {
  for (const sharedIssue of [true, false]) {
    const responses = {};
    const pulls = Array.from({ length: 62 }, (_, i) => {
      const number = sharedIssue ? 2960 : i + 1;
      const commentId = number + 500;
      responses[`/repos/${repository}/issues/${number}`] = issue({ number });
      responses[`/repos/${repository}/issues/${number}/comments?per_page=100&page=1`] = [comment({ id: commentId })];
      const pull = pr({ id: i + 1, number: i + 3000,
        body: `Refs #${number}. https://github.com/${repository}/issues/${number}#issuecomment-${commentId}` });
      responses[`/repos/${repository}/pulls/${pull.number}`] = pull;
      return pull;
    });
    responses[`/repos/${repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page=1`] = pulls;
    const client = api(responses);
    const env = environment(client, 'push', {});
    await runner.run(env);
    assert.equal(env.writes.length, 62);
    assert.deepEqual(env.failures, []);
    assert.ok(env.writes.every(value => value.conclusion === 'neutral'));
    assert.equal(client.calls.length, 2 + 62 * 2 + (sharedIssue ? 2 : 62 * 2));
  }
});

test('manual rechecks use default-branch repository dispatch, not branch-selected workflows', async () => {
  const client = api();
  assert.deepEqual(await runner.targets(client, repository, 'repository_dispatch',
    { action: 'pr-eligibility-recheck', client_payload: { pr_number: 3000 } }), [3000]);
  await assert.rejects(runner.targets(client, repository, 'workflow_dispatch',
    { inputs: { pr_number: 3000 } }), /Unsupported event/);
  for (const value of ['3000; ignored', [3000], {}]) {
    await assert.rejects(runner.targets(client, repository, 'repository_dispatch',
      { action: 'pr-eligibility-recheck', client_payload: { pr_number: value } }));
  }
  assert.deepEqual(client.calls, []);
});

test('default-branch workflow_run consumes only queue metadata, never artifacts or head code', async () => {
  const queueSha = 'd'.repeat(40);
  const signal = {
    id: 90, workflow_id: 91, repository: { full_name: repository }, head_repository: { full_name: repository },
    name: 'PR eligibility queue signal', event: 'merge_group', status: 'completed',
    head_sha: queueSha,
  };
  const workflow = { id: 91, name: signal.name, path: '.github/workflows/pr-eligibility-queue.yml' };
  const responses = {
    [`/repos/${repository}/actions/runs/90`]: signal,
    [`/repos/${repository}/actions/workflows/91`]: workflow,
    [`/repos/${repository}/commits/${queueSha}/pulls?per_page=100&page=1`]: [pr()],
  };
  const client = api(responses);
  const payload = { workflow_run: { id: 90, head_sha: 'e'.repeat(40),
    artifacts_url: 'https://untrusted.invalid/artifact', head_branch: 'untrusted-code' } };
  const env = environment(client, 'workflow_run', payload);
  await runner.run(env);
  assert.deepEqual(env.writes.map(value => value.head_sha), [sha, queueSha]);
  assert.ok(env.writes.every(value => value.conclusion === 'neutral'));
  assert.deepEqual(env.failures, []);
  for (const bad of [
    { [`/repos/${repository}/actions/runs/90`]: { ...signal, event: 'pull_request' } },
    { [`/repos/${repository}/actions/runs/90`]: { ...signal, head_repository: { full_name: 'untrusted/fork' } } },
    { [`/repos/${repository}/actions/runs/90`]: httpError(403) },
    { [`/repos/${repository}/actions/workflows/91`]: { ...workflow, path: '.github/workflows/other.yml' } },
  ]) {
    const rejected = environment(api({ ...responses, ...bad }), 'workflow_run', payload);
    await runner.run(rejected);
    assert.equal(rejected.failures.length, 1);
    assert.deepEqual(rejected.writes, []);
  }
});

test('real API objects distinguish issues from arbitrary nonexistent numbers and PRs', async () => {
  const client = api({
    [`/repos/${repository}/issues/12`]: issue({ number: 12, pull_request: {} }),
    [`/repos/${repository}/issues/13`]: httpError(404),
  });
  const result = await eligibility.evaluatePull({ client, repository, policy, pr: pr({ body: 'References #12 and #13.' }) });
  assert.equal(result.state, 'needs-evidence');
  assert.deepEqual(result.issues.map(value => value.state), ['not-an-issue', 'unavailable-reference']);
});

test('comment 404, 403 and unexpected pages are errors, not no-issue success', async () => {
  for (const response of [httpError(404), httpError(403), { incomplete: true }]) {
    const client = api({ [`/repos/${repository}/issues/2960/comments?per_page=100&page=1`]: response });
    const result = await eligibility.evaluatePull({ client, repository, policy, pr: pr() });
    assert.equal(result.state, 'error');
    assert.equal(JSON.stringify(result).includes('Confidential response'), false);
  }
});

test('pagination passes a full first page and rejects repeated pages', async () => {
  const first = Array.from({ length: 100 }, (_, id) => ({ id }));
  const client = api({ '/items?per_page=100&page=1': first, '/items?per_page=100&page=2': [{ id: 100 }] });
  assert.equal((await eligibility.listAll(client, '/items')).length, 101);
  const broken = api({ '/items?per_page=100&page=1': first, '/items?per_page=100&page=2': first });
  await assert.rejects(eligibility.listAll(broken, '/items'));
  const denied = api({ '/items?per_page=100&page=1': first, '/items?per_page=100&page=2': httpError(403) });
  await assert.rejects(eligibility.listAll(denied, '/items'));
});

test('fork and stacked PRs read default-branch policy, never the head or feature base', async () => {
  const client = api();
  const trusted = await eligibility.trustedPolicy(client, repository);
  assert.equal(trusted.sha, baseSha);
  const result = await eligibility.evaluatePull({ client, repository, policy: trusted.policy, pr: pr() });
  assert.equal(result.state, 'record-present');
  assert.ok(client.calls.every(route => !route.includes('untrusted-stack') && !route.includes('contributor')));
});

test('private security and dependency submissions are never asked for public confidential tracking', async () => {
  for (const candidate of [
    pr({ body: '<!-- apm-private-tracking --> private details', number: 2893 }),
    pr({ body: 'Bumps a library', number: 2920, user: { login: 'dependabot[bot]', type: 'Bot' } }),
    pr({ body: 'Fix GHSA-abc1-def2-ghi3', number: 2921 }),
  ]) {
    const client = api();
    const result = await eligibility.evaluatePull({ client, repository, policy, pr: candidate });
    assert.equal(result.state, 'private-tracking-manual-review');
    assert.equal(result.authorizes_implementation, false);
    assert.deepEqual(client.calls, []);
    assert.equal(runner.render(result).includes('private details'), false);
  }
});

test('standing preapproval still needs an issue and a human assessment of triviality', async () => {
  for (const kind of ['typo', 'broken-link']) {
    const body = `<!-- apm-standing-preapproval: ${kind} -->`;
    const missing = await eligibility.evaluatePull({ client: api(), repository, policy, pr: pr({ body }) });
    assert.equal(missing.state, 'needs-evidence');
    const linked = await eligibility.evaluatePull({ client: api(), repository, policy, pr: pr({ body: body + '\n#2960' }) });
    assert.equal(linked.state, 'standing-preapproval-manual-review');
    assert.equal(linked.authorizes_implementation, false);
  }
});

test('always-neutral publication uses the fork head SHA and exposes no write operation except checks', async () => {
  const env = environment(api());
  const result = await runner.run(env);
  assert.equal(result[0].state, 'record-present');
  assert.equal(env.writes.length, 1);
  assert.equal(env.writes[0].head_sha, sha);
  assert.equal(env.writes[0].conclusion, 'neutral');
  assert.deepEqual(env.failures, []);
});

test('issue decision edit/delete refreshes only referencing PRs, not unrelated panels', async () => {
  for (const action of ['edited', 'deleted']) {
    const client = api({
      [`/repos/${repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page=1`]:
        [pr(), pr({ id: 3, number: 3001, body: 'Unrelated #11' })],
    });
    const env = environment(client, 'issue_comment', { action, issue: issue(), comment: comment() });
    const result = await runner.run(env);
    assert.equal(result[0].state, 'needs-evidence');
    assert.equal(env.writes.length, 1);
    assert.ok(client.calls.every(route => !route.endsWith('/pulls/3001')));
  }
  assert.equal(runner.relevantEvent('issue_comment', { action: 'created', issue: issue(),
    comment: { body: 'Thanks!' } }), false);
});

test('references changed during evaluation never publish old record presence on a new head', async () => {
  let reads = 0;
  const client = api({ [`/repos/${repository}/pulls/3000`]: () => ++reads === 1 ? pr() : pr({ body: '' }) });
  const env = environment(client);
  await runner.run(env);
  assert.equal(env.writes[0].output.title, 'needs-evidence');
});

test('merge queue reports associated PR evidence and a neutral queue-specific limitation', async () => {
  const queueSha = 'd'.repeat(40);
  const client = api({ [`/repos/${repository}/commits/${queueSha}/pulls?per_page=100&page=1`]: [pr()] });
  const env = environment(client, 'merge_group', { merge_group: { head_sha: queueSha } });
  await runner.run(env);
  assert.deepEqual(env.writes.map(value => value.head_sha), [sha, queueSha]);
  assert.ok(env.writes.every(value => value.conclusion === 'neutral'));
  assert.equal(env.writes[1].output.title, 'merge-queue-manual-review');
});

test('missing merge queue associations and fork-token denial produce explicit unknown/error', async () => {
  const queueSha = 'd'.repeat(40);
  for (const value of [[], httpError(403)]) {
    const client = api({ [`/repos/${repository}/commits/${queueSha}/pulls?per_page=100&page=1`]: value });
    const env = environment(client, 'merge_group', { merge_group: { head_sha: queueSha } });
    await runner.run(env);
    assert.equal(env.writes[0].conclusion, 'neutral');
    assert.equal(env.writes[0].output.title, 'error');
    assert.equal(env.failures.length, 1);
  }
});

test('merge queue preserves associated comment failures including later-page 404', async () => {
  const queueSha = 'd'.repeat(40);
  const commentsRoute = `/repos/${repository}/issues/2960/comments?per_page=100&page=`;
  for (const status of [403, 404]) {
    for (const page of [1, 2]) {
      const responses = {
        [`/repos/${repository}/commits/${queueSha}/pulls?per_page=100&page=1`]: [pr()],
        [commentsRoute + page]: httpError(status),
      };
      if (page === 2) responses[commentsRoute + 1] = Array.from({ length: 100 }, (_, id) => comment({ id: 1000 + id }));
      const env = environment(api(responses), 'merge_group', { merge_group: { head_sha: queueSha } });
      await runner.run(env);
      assert.deepEqual(env.writes.map(value => value.output.title), ['error', 'error']);
      assert.ok(env.writes.every(value => value.conclusion === 'neutral'));
      assert.equal(env.failures.length, 1);
    }
  }
});

test('check publication denial is an explicit failed run, not a successful advisory', async () => {
  const env = environment(api());
  env.github.rest.checks.create = async () => { throw httpError(403); };
  await assert.rejects(runner.run(env));
  assert.equal(env.writes.length, 0);
});
