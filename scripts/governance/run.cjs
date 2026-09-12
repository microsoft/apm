'use strict';

const { MARKER, RESET, evidence, requireValue } = require('./authority.cjs');
const { references, listAll, trustedPolicy, evaluatePull, errorEvidence, positiveNumber } = require('./eligibility.cjs');

const CHECK_NAME = 'PR eligibility (advisory only)';

function relevantEvent(eventName, payload) {
  if (eventName !== 'issue_comment') return true;
  if (payload.issue?.pull_request) return false;
  return ['edited', 'deleted'].includes(payload.action)
    || (payload.comment?.body || '').includes(MARKER)
    || (payload.comment?.body || '').includes(RESET);
}

async function targets(client, repository, eventName, payload) {
  const root = `/repos/${repository}`;
  if (eventName === 'pull_request_target') return [positiveNumber(payload.pull_request?.number)];
  if (eventName === 'workflow_dispatch') return [positiveNumber(payload.inputs?.pr_number)];
  if (eventName === 'merge_group') {
    const sha = payload.merge_group?.head_sha;
    requireValue(/^[a-f0-9]{40}$/.test(sha || ''), 'Missing queue SHA');
    const pulls = await listAll(client, `${root}/commits/${sha}/pulls`);
    requireValue(pulls.length > 0, 'Queue association evidence unavailable');
    return pulls.filter(pr => pr.state === 'open').map(pr => positiveNumber(pr.number));
  }
  requireValue(['issues', 'issue_comment', 'push'].includes(eventName), 'Unsupported event');
  const pulls = await listAll(client, `${root}/pulls?state=open&sort=created&direction=asc`);
  if (eventName === 'push') return pulls.map(pr => positiveNumber(pr.number));
  const issue = positiveNumber(payload.issue?.number);
  return pulls.filter(pr => references(pr.body, repository).issues.includes(issue))
    .map(pr => positiveNumber(pr.number));
}

function render(result) {
  const rows = (result.issues || []).map(issue => `- Issue #${issue.number}: ${issue.state}. ${issue.reason}`);
  return [
    '**Advisory evidence only. This check never authorizes implementation or merge.**',
    '',
    `State: \`${result.state}\`. ${result.reason}`,
    ...rows,
    '',
    'A current comment scan cannot detect a deleted withdrawal. Confirm current bounded scope,',
    'implementation-to-scope match and review support with a responsible human.',
    'Use CONTRIBUTING.md and the trusted default-branch GOVERNANCE.md roster.',
    'Private security tracking stays private. No labels, milestones or code reviews substitute for scope approval.',
  ].join('\n');
}

async function run({ github, context, core, trustedSha }) {
  const repository = `${context.repo.owner}/${context.repo.repo}`;
  const client = { get: async path => (await github.request(`GET ${path}`)).data };
  const payload = context.payload;
  if (!relevantEvent(context.eventName, payload)) return [];
  let numbers;
  let policy;
  let assessmentFailed = false;
  const results = [];
  const publish = async (sha, result) => {
    requireValue(/^[a-f0-9]{40}$/.test(sha || ''), 'Advisory target SHA unavailable');
    await github.rest.checks.create({
      ...context.repo, name: CHECK_NAME, head_sha: sha, status: 'completed', conclusion: 'neutral',
      output: { title: result.state, summary: render(result) },
    });
    results.push(result);
  };
  try {
    ({ policy } = await trustedPolicy(client, repository, trustedSha));
    numbers = await targets(client, repository, context.eventName, payload);
    requireValue(context.eventName !== 'merge_group' || numbers.length > 0, 'No open queue associations');
  } catch (error) {
    const result = errorEvidence(error);
    const sha = payload.merge_group?.head_sha || payload.pull_request?.head?.sha;
    if (sha) await publish(sha, result);
    await core.summary.addHeading(CHECK_NAME).addRaw(render(result)).write();
    core.setFailed('Eligibility evidence unavailable. No approval granted. Check API permissions, pagination and trusted base configuration.');
    return results;
  }
  const changedIssue = ['issues', 'issue_comment'].includes(context.eventName)
    && ['edited', 'deleted'].includes(payload.action) ? payload.issue.number : undefined;
  for (const number of [...new Set(numbers)]) {
    let pr;
    try {
      pr = await client.get(`/repos/${repository}/pulls/${number}`);
      requireValue(pr.number === number && pr.base?.repo?.full_name?.toLowerCase() === repository.toLowerCase(),
        'PR is not in target repository');
      if (pr.state !== 'open') continue;
      const result = await evaluatePull({ client, repository, policy, pr, changedIssue });
      // Re-read references and head before publishing: a result for an obsolete body is not current evidence.
      const fresh = await client.get(`/repos/${repository}/pulls/${number}`);
      if (fresh.state !== 'open') continue;
      if (fresh.head?.sha !== pr.head.sha || fresh.body !== pr.body) {
        await publish(fresh.head?.sha, evidence('needs-evidence', 'PR head or references changed during assessment; rerun the advisory.'));
      } else {
        await publish(pr.head.sha, result);
      }
      if (result.state === 'error') {
        assessmentFailed = true;
        core.setFailed('One or more metadata reads failed; evidence is unknown.');
      }
    } catch (error) {
      assessmentFailed = true;
      const result = errorEvidence(error);
      if (pr?.head?.sha) await publish(pr.head.sha, result);
      else await core.summary.addRaw(`PR #${number}: ${result.reason}\n`).write();
      core.setFailed('PR eligibility could not be assessed or published; no approval granted.');
    }
  }
  if (context.eventName === 'merge_group') {
    await publish(payload.merge_group.head_sha, assessmentFailed
      ? evidence('error', 'Associated PR evidence could not be completely read; queue scope evidence is unknown.')
      : evidence('merge-queue-manual-review',
        'Associated PR evidence was refreshed. Commit associations do not prove complete queue membership or human scope approval.'));
  }
  await core.summary.addHeading(CHECK_NAME)
    .addRaw(`Published ${results.length} neutral reports. No implementation or merge authorization.`).write();
  return results;
}

module.exports = { run, targets, relevantEvent, render, CHECK_NAME };
