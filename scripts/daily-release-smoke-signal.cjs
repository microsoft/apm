const MARKER = '<!-- apm-daily-release-smoke -->';
const LABEL = 'ci/daily-smoke';
const TITLE = '[ci] Daily release smoke is failing';
const BAD_CONCLUSIONS = new Set(['failure', 'cancelled', 'timed_out', 'action_required']);

async function ensureLabel({ github, owner, repo }) {
  try {
    await github.rest.issues.getLabel({ owner, repo, name: LABEL });
  } catch (error) {
    if (error.status !== 404) {
      throw error;
    }
    await github.rest.issues.createLabel({
      owner,
      repo,
      name: LABEL,
      color: 'D93F0B',
      description: 'Daily release smoke validation signal',
    });
  }
}

async function findTrackingIssue({ github, owner, repo }) {
  const issues = await github.paginate(github.rest.issues.listForRepo, {
    owner,
    repo,
    state: 'open',
    labels: LABEL,
    per_page: 100,
  });
  return issues.find((issue) => !issue.pull_request && (issue.body || '').includes(MARKER));
}

function stripAnsi(value) {
  return value.replace(/\u001b\[[0-9;]*m/g, '');
}

function extractFailedTests(logText) {
  const tests = new Set();
  const cleanLog = stripAnsi(logText);
  const patterns = [
    /\b(?:FAILED|ERROR)\s+(tests\/[^\s]+(?:::[^\s]+)*)/g,
    /\b(tests\/[^\s]+(?:::[^\s]+)*)\s+(?:FAILED|ERROR)\b/g,
  ];
  for (const pattern of patterns) {
    for (const match of cleanLog.matchAll(pattern)) {
      tests.add(match[1].replace(/[,\]]$/, ''));
      if (tests.size >= 30) {
        return [...tests];
      }
    }
  }
  return [...tests];
}

async function collectFailedTests({ github, core, owner, repo, failedJobs }) {
  const tests = new Set();
  for (const job of failedJobs.slice(0, 10)) {
    try {
      const response = await github.rest.actions.downloadJobLogsForWorkflowRun({
        owner,
        repo,
        job_id: job.id,
      });
      const logText = typeof response.data === 'string'
        ? response.data
        : Buffer.from(response.data).toString('utf8');
      for (const testName of extractFailedTests(logText)) {
        tests.add(testName);
        if (tests.size >= 30) {
          return [...tests];
        }
      }
    } catch (error) {
      core.warning('Could not inspect logs for job ' + job.name + ': ' + error.message);
    }
  }
  return [...tests];
}

function failureBody({ context, runUrl, failedJobs, failedTests }) {
  const failedJobLines = failedJobs.map((job) => {
    const name = job.html_url ? '[' + job.name + '](' + job.html_url + ')' : job.name;
    return '- ' + name + ' - ' + job.conclusion;
  });
  const failedTestLines = failedTests.length
    ? failedTests.map((testName) => '- `' + testName + '`')
    : ['- Not extractable from failed job logs.'];
  return [
    MARKER,
    'The scheduled release smoke validation is currently failing.',
    '',
    '### Current failing run',
    '',
    '- Run: [' + context.runNumber + '](' + runUrl + ')',
    '- Head SHA: `' + context.sha + '`',
    '- Workflow: ' + context.workflow,
    '',
    '### Failed jobs',
    '',
    ...failedJobLines,
    '',
    '### Failing tests',
    '',
    ...failedTestLines,
    '',
    'This advisory issue is updated by the daily scheduled CI/CD Pipeline run. It does not gate PR merges.',
  ].join('\n');
}

async function listFailedJobs({ github, context, owner, repo }) {
  const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
    owner,
    repo,
    run_id: context.runId,
    per_page: 100,
  });
  return jobs
    .filter((job) => job.name !== 'Daily Release Smoke Signal')
    .filter((job) => BAD_CONCLUSIONS.has(job.conclusion));
}

async function closeRecoveredIssue({ github, core, owner, repo, context, runUrl, trackingIssue }) {
  if (!trackingIssue) {
    core.info('Daily release smoke recovered; no open tracking issue exists.');
    return;
  }
  const comment = [
    'Recovered in scheduled run [' + context.runNumber + '](' + runUrl + ').',
    '',
    'Head SHA: `' + context.sha + '`',
  ].join('\n');
  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: trackingIssue.number,
    body: comment,
  });
  await github.rest.issues.update({
    owner,
    repo,
    issue_number: trackingIssue.number,
    state: 'closed',
    state_reason: 'completed',
  });
  core.info('Closed recovered daily smoke issue #' + trackingIssue.number + '.');
}

async function upsertFailureIssue({
  github,
  core,
  owner,
  repo,
  context,
  runUrl,
  trackingIssue,
  failedJobs,
  failedTests,
}) {
  const body = failureBody({ context, runUrl, failedJobs, failedTests });
  if (trackingIssue) {
    await github.rest.issues.update({
      owner,
      repo,
      issue_number: trackingIssue.number,
      body,
    });
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: trackingIssue.number,
      body: 'Still failing in scheduled run [' + context.runNumber + '](' + runUrl + ') at `' + context.sha + '`.',
    });
    core.info('Updated daily smoke issue #' + trackingIssue.number + '.');
    return;
  }
  const issue = await github.rest.issues.create({
    owner,
    repo,
    title: TITLE,
    body,
    labels: [LABEL],
  });
  core.info('Created daily smoke issue #' + issue.data.number + ': ' + issue.data.html_url);
}

async function main({ github, context, core }) {
  const owner = context.repo.owner;
  const repo = context.repo.repo;
  const serverUrl = context.serverUrl || process.env.GITHUB_SERVER_URL || 'https://github.com';
  const runUrl = serverUrl + '/' + owner + '/' + repo + '/actions/runs/' + context.runId;

  await ensureLabel({ github, owner, repo });
  const failedJobs = await listFailedJobs({ github, context, owner, repo });
  const trackingIssue = await findTrackingIssue({ github, owner, repo });

  if (failedJobs.length === 0) {
    await closeRecoveredIssue({ github, core, owner, repo, context, runUrl, trackingIssue });
    return;
  }

  const failedTests = await collectFailedTests({ github, core, owner, repo, failedJobs });
  await upsertFailureIssue({
    github,
    core,
    owner,
    repo,
    context,
    runUrl,
    trackingIssue,
    failedJobs,
    failedTests,
  });
}

module.exports = { main };
