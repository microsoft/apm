'use strict';

const { execFileSync } = require('node:child_process');
const { readPolicy, commentReference, evaluateIssue, evidence, requireValue, EvidenceError } = require('./authority.cjs');

function repositoryName(value) {
  requireValue(typeof value === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value),
    'Expected owner/repository');
  return value;
}

function positiveNumber(value) {
  requireValue(/^[1-9]\d*$/.test(String(value)) && Number.isSafeInteger(Number(value)),
    'Expected positive issue or PR number');
  return Number(value);
}

function references(body, repository) {
  const issues = new Set();
  const approvals = new Map();
  let text = (body || '').replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g, '')
    .replace(/`[^`\n]*`/g, '').replace(/^\s*>.*$/gm, '');
  text = text.replace(/\[[^\]\n]*\]\((https?:\/\/[^)\s]+)\)/g, '$1');
  // Consume qualified URLs/refs before bare #N so upstream references never become local.
  text = text.replace(/https?:\/\/[^\s<>"')\]]+/g, value => {
    let url;
    try { url = new URL(value.replace(/[.,;]+$/, '')); } catch { return ' '; }
    const approval = commentReference(url.href, repository);
    if (approval) {
      issues.add(approval.issue);
      if (!approvals.has(approval.issue)) approvals.set(approval.issue, []);
      const normalized = `https://github.com/${repository.toLowerCase()}/issues/${approval.issue}#issuecomment-${approval.comment}`;
      if (!approvals.get(approval.issue).includes(normalized)) approvals.get(approval.issue).push(normalized);
    }
    const parts = url.pathname.split('/');
    if (url.origin === 'https://github.com' && !url.username && !url.password
        && parts.length === 5 && parts.slice(1, 3).join('/').toLowerCase() === repository.toLowerCase()
        && parts[3] === 'issues' && /^[1-9]\d*$/.test(parts[4])) {
      issues.add(positiveNumber(parts[4]));
    }
    return ' ';
  });
  text = text.replace(/\b([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#([1-9]\d*)\b/g, (_, repo, number) => {
    if (repo.toLowerCase() === repository.toLowerCase()) issues.add(positiveNumber(number));
    return ' ';
  });
  // Ignore HTML link captions: Dependabot's upstream #123 captions are not local references.
  text = text.replace(/<a\b[^>]*>[\s\S]*?<\/a>/gi, '').replace(/<!--[\s\S]*?-->/g, '');
  for (const match of text.matchAll(/(^|[\s([:])#([1-9]\d*)\b/g)) {
    issues.add(positiveNumber(match[2]));
  }
  return { issues: [...issues], approvals };
}

function privateReview(pr) {
  const body = pr.body || '';
  return pr.user?.login === 'dependabot[bot]'
    || /<!-- apm-private-tracking -->|confidential (?:security|tracking)|private security|vulnerability alert|GHSA-[a-z0-9-]+/i.test(body);
}

function errorEvidence(error) {
  if (error instanceof EvidenceError) {
    return evidence('error', `${error.message}. Evidence is unknown; obtain human review.`);
  }
  const status = Number(error.status);
  return evidence('error', Number.isInteger(status) && status >= 400 && status <= 599
    ? `GitHub metadata read failed (HTTP ${status}); evidence is unknown.`
    : 'Metadata or policy validation failed; evidence is unknown. Inspect the trusted runner diagnostic.');
}

async function listAll(client, path) {
  const result = [];
  const seen = new Set();
  for (let page = 1; ; page += 1) {
    const values = await client.get(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`);
    requireValue(Array.isArray(values), 'Expected a complete metadata page');
    for (const value of values) {
      requireValue(value && Number.isSafeInteger(value.id) && !seen.has(value.id),
        'Incomplete or repeated metadata pagination');
      seen.add(value.id);
      result.push(value);
    }
    if (values.length < 100) return result;
  }
}

async function trustedPolicy(client, repository, trustedSha) {
  repositoryName(repository);
  let sha = trustedSha;
  if (!sha) {
    const repo = await client.get(`/repos/${repository}`);
    requireValue(typeof repo.default_branch === 'string' && repo.default_branch.length > 0,
      'Default branch unavailable');
    const commit = await client.get(`/repos/${repository}/commits/${encodeURIComponent(repo.default_branch)}`);
    sha = commit.sha;
  }
  requireValue(/^[a-f0-9]{40}$/.test(sha || ''), 'Trusted default-branch SHA unavailable');
  const file = await client.get(`/repos/${repository}/contents/GOVERNANCE.md?ref=${sha}`);
  requireValue(file.type === 'file' && file.encoding === 'base64' && typeof file.content === 'string',
    'Trusted governance file unavailable');
  return { policy: readPolicy(Buffer.from(file.content, 'base64').toString('utf8')), sha };
}

async function issueEvidence({ client, repository, policy, number, approvalUrl, changed = false }) {
  let issue;
  try {
    issue = await client.get(`/repos/${repository}/issues/${positiveNumber(number)}`);
  } catch (error) {
    if (error.status !== 404) throw error;
    return evidence('unavailable-reference', 'Referenced item is unavailable; verify traceability without exposing private details.');
  }
  requireValue(issue && issue.number === number, 'Unexpected issue response');
  if (issue.pull_request) return evidence('not-an-issue', 'A PR reference is not issue traceability.');
  const comments = await listAll(client, `/repos/${repository}/issues/${number}/comments`);
  return evaluateIssue({ policy, repository, issue, comments, approvalUrl, changed });
}

async function evaluatePull({ client, repository, policy, pr, changedIssue }) {
  requireValue(pr && Number.isSafeInteger(pr.number) && /^[a-f0-9]{40}$/.test(pr.head?.sha || ''),
    'Incomplete PR metadata');
  if (privateReview(pr)) {
    return evidence('private-tracking-manual-review',
      'Security/dependency coordination may use private tracking. Confirm privately; do not publish confidential links.',
      { pr: pr.number, issues: [] });
  }
  const links = references(pr.body, repository);
  const issues = [];
  for (const number of links.issues) {
    const urls = links.approvals.get(number) || [];
    if (urls.length > 1) {
      issues.push({ number, ...evidence('needs-evidence', 'Nominate one current approval comment per issue.') });
      continue;
    }
    try {
      issues.push({ number, ...await issueEvidence({ client, repository, policy, number,
        approvalUrl: urls[0], changed: number === changedIssue }) });
    } catch (error) {
      issues.push({ number, ...errorEvidence(error) });
    }
  }
  if (issues.some(issue => issue.state === 'error')) {
    return evidence('error', 'One or more evidence reads failed; no complete assessment is available.', { pr: pr.number, issues });
  }
  const realIssues = issues.filter(issue => !['not-an-issue', 'unavailable-reference'].includes(issue.state));
  if (realIssues.length === 0) {
    return evidence('needs-evidence',
      'No public issue traceability verified. Supply a real scope issue or ask a maintainer to confirm confidential tracking privately.',
      { pr: pr.number, issues });
  }
  const trivial = /<!-- apm-standing-preapproval: (?:typo|broken-link) -->/.test(pr.body || '');
  if (trivial) {
    return evidence('standing-preapproval-manual-review',
      'Issue traceability found. A human must verify this is only a typo/broken-link correction, not code or substantive docs.',
      { pr: pr.number, issues });
  }
  const state = realIssues.some(issue => issue.state === 'withdrawn') ? 'withdrawn'
    : realIssues.every(issue => issue.state === 'record-present') ? 'record-present' : 'needs-evidence';
  return evidence(state,
    'Candidate issue links are metadata, not proof the implementation matches scope. Fresh human confirmation remains required.',
    { pr: pr.number, issues });
}

function ghClient() {
  return {
    async get(path) {
      try {
        return JSON.parse(execFileSync('gh', ['api', '--hostname', 'github.com', '--method', 'GET', path],
          { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
      } catch (cause) {
        const error = new Error('GitHub metadata request failed');
        const match = /\(HTTP (\d{3})\)/.exec(String(cause.stderr || ''));
        if (match) error.status = Number(match[1]);
        throw error;
      }
    },
  };
}

async function main(argv) {
  if (argv.length === 1 && argv[0] === '--help') {
    console.log('Read-only governance evidence (never implementation permission).\n'
      + 'node scripts/governance/eligibility.cjs --repo OWNER/REPO --issue N --approval-url URL\n'
      + 'node scripts/governance/eligibility.cjs --repo OWNER/REPO --pr N\n'
      + 'Reads policy from the trusted default branch. JSON on stdout; read errors exit 1.');
    return;
  }
  const options = {};
  requireValue(argv.length % 2 === 0, 'Expected option/value pairs; use --help');
  for (let i = 0; i < argv.length; i += 2) {
    requireValue(['--repo', '--issue', '--pr', '--approval-url'].includes(argv[i])
      && !Object.hasOwn(options, argv[i]), 'Unknown or repeated option; use --help');
    options[argv[i]] = argv[i + 1];
  }
  const repository = repositoryName(options['--repo']);
  requireValue(Boolean(options['--issue']) !== Boolean(options['--pr']), 'Select one issue or PR');
  const client = ghClient();
  const { policy, sha } = await trustedPolicy(client, repository);
  const result = options['--issue']
    ? await issueEvidence({ client, repository, policy, number: positiveNumber(options['--issue']),
      approvalUrl: options['--approval-url'] })
    : await evaluatePull({ client, repository, policy,
      pr: await client.get(`/repos/${repository}/pulls/${positiveNumber(options['--pr'])}`) });
  console.log(JSON.stringify({ ...result, trusted_sha: sha }));
  if (result.state === 'error') process.exitCode = 1;
}

if (require.main === module) {
  main(process.argv.slice(2)).catch(error => {
    console.error('Governance evidence could not be established. Use --help and verify GitHub read permissions and base policy.');
    console.log(JSON.stringify(errorEvidence(error)));
    process.exitCode = 1;
  });
}

module.exports = { references, privateReview, listAll, trustedPolicy, issueEvidence,
  evaluatePull, errorEvidence, repositoryName, positiveNumber };
