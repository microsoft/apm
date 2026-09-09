"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REPO = "microsoft/apm";
const MAIN_BRANCH = "main";
const WORKFLOW_PATH = ".github/workflows/build-release.yml";
const EVIDENCE_BASENAME = "release-candidate-evidence";
const TRUSTED_REUSE_EVENTS = new Set(["schedule", "repository_dispatch"]);
const CURRENT_RUN_EVENTS = new Set(["push", "schedule", "repository_dispatch"]);
const MAX_CANDIDATE_AGE_DAYS = 30;
const RERUN_ALL_JOBS_HINT = "Use Re-run all jobs to regenerate complete candidate evidence for this attempt";
const CONFIG_HASH_FILES = [
  ".github/workflows/build-release.yml",
  ".github/workflows/release-platform.yml",
  ".github/workflows/release-unit.yml",
  ".github/workflows/release-integration.yml",
  ".github/workflows/docs-build.yml",
  ".github/workflows/pypi-distributions.yml",
  ".github/workflows/ci.yml",
  ".github/actions/pytest-timing/action.yml",
  "scripts/test-integration.sh",
  "scripts/github-token-helper.sh",
  "scripts/package_release.py",
  "scripts/release-candidate.cjs",
  "scripts/release-platforms.json",
];
const REQUIRED_SOURCE_JOB_NAMES = [
  "Candidate Source Checks / Lint",
  "Candidate Source Checks / Windows Compatibility Gate",
  "Candidate Source Checks / Test Architecture Ratchets",
  "Candidate Source Checks / Build & Test Shard 1 (Linux)",
  "Candidate Source Checks / Build & Test Shard 2 (Linux)",
  "Candidate Source Checks / Coverage Combine (Linux)",
  "Candidate Source Checks / APM Self-Check",
  "Candidate Source Checks / Lifecycle Smoke (Linux)",
];
const REQUIRED_SOURCE_JOB_FRAGMENTS = REQUIRED_SOURCE_JOB_NAMES;
const ARTIFACT_DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;

class CandidateUnavailableError extends Error {}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function loadCatalog() {
  return readJson(path.join(__dirname, "release-platforms.json"));
}

function archiveName(binaryName) {
  return `${binaryName}${binaryName === "apm-windows-x86_64" ? ".zip" : ".tar.gz"}`;
}

function assertFullSha(sha, label = "sha") {
  if (!/^[0-9a-f]{40}$/.test(String(sha || ""))) {
    throw new Error(`${label} must be a full lowercase 40-character commit SHA`);
  }
}

function repoFullName(context) {
  if (context?.repo?.owner && context?.repo?.repo) {
    return `${context.repo.owner}/${context.repo.repo}`;
  }
  return context?.payload?.repository?.full_name || process.env.GITHUB_REPOSITORY || "";
}

function repoParts(context) {
  const full = repoFullName(context);
  const [owner, repo] = full.split("/");
  return { owner, repo };
}

function eventName(context) {
  return context?.eventName || context?.event_name || process.env.GITHUB_EVENT_NAME || "";
}

function refName(ref) {
  if (!ref) return "";
  if (ref.startsWith("refs/heads/")) return ref.slice("refs/heads/".length);
  if (ref.startsWith("refs/tags/")) return ref.slice("refs/tags/".length);
  return ref.split("/").pop() || "";
}

function isTagPush(context) {
  return eventName(context) === "push" && String(context?.ref || "").startsWith("refs/tags/v");
}

function isPrereleaseTag(context) {
  return isTagPush(context) && !/^v[0-9]+\.[0-9]+\.[0-9]+$/.test(refName(context.ref));
}

function tagVersion(context) {
  if (!isTagPush(context)) return null;
  return refName(context.ref).replace(/^v/, "");
}

function requiresFullValidation(context) {
  const event = eventName(context);
  return isTagPush(context) || event === "schedule" || event === "repository_dispatch";
}

function matrixForContext(context, catalog = loadCatalog()) {
  const full = requiresFullValidation(context);
  return {
    matrix: { include: full ? catalog : catalog.filter((row) => row.on_main !== false) },
    fullValidation: full,
  };
}

function digestFile(file) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(file));
  return hash.digest("hex");
}

function configHashes(root = process.cwd()) {
  const hashes = {};
  for (const relative of CONFIG_HASH_FILES) {
    const file = path.join(root, relative);
    if (!fs.existsSync(file)) {
      throw new Error(`Required release config file is missing: ${relative}`);
    }
    hashes[relative] = `sha256:${digestFile(file)}`;
  }
  return hashes;
}

function walkFiles(root) {
  if (!fs.existsSync(root)) return [];
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(full));
    } else if (entry.isFile()) {
      files.push(full);
    }
  }
  return files;
}

function findOneFile(root, filename) {
  const matches = walkFiles(root).filter((file) => path.basename(file) === filename);
  if (matches.length !== 1) {
    throw new Error(`Expected exactly one ${filename} under ${root}, found ${matches.length}`);
  }
  return matches[0];
}

function candidateArtifactName(binaryName, runAttempt) {
  return `candidate-${requireRunAttempt(runAttempt)}-${binaryName}`;
}

function evidenceArtifactName(runAttempt) {
  return `${EVIDENCE_BASENAME}-${requireRunAttempt(runAttempt)}`;
}

function requireRunAttempt(attempt) {
  if (!/^[1-9][0-9]*$/.test(String(attempt || ""))) {
    throw new Error("Workflow run attempt must be present and numeric");
  }
  return Number(attempt);
}

function requireArtifactDigest(digest, label) {
  if (!ARTIFACT_DIGEST_PATTERN.test(String(digest || ""))) {
    throw new Error(`${label} is missing a valid immutable sha256 digest`);
  }
}

function requireFreshArtifact(artifact, label) {
  if (artifact.expired === true) throw new CandidateUnavailableError(`${label} artifact is expired`);
  requireArtifactDigest(artifact.digest, label);
}

function assertChecksumSidecar(sidecarPath, digest, archive) {
  const sidecar = fs.readFileSync(sidecarPath, "utf8").trimEnd();
  if (sidecar !== `${digest}  ${archive}`) {
    throw new Error(`Candidate checksum sidecar mismatch for ${archive}`);
  }
}

function findOneArtifactByName(artifacts, name) {
  const matches = artifacts.filter((artifact) => artifact.name === name);
  if (matches.length !== 1) {
    const ErrorType = matches.length === 0 ? CandidateUnavailableError : Error;
    throw new ErrorType(
      `Expected exactly one workflow artifact named ${name}, found ${matches.length}`,
    );
  }
  return matches[0];
}

function addAttemptHint(error) {
  if (String(error?.message || "").includes(RERUN_ALL_JOBS_HINT)) return error;
  return new Error(`${error.message}. ${RERUN_ALL_JOBS_HINT}.`);
}

async function paginate(github, method, params, collectionName) {
  if (typeof github.paginate === "function") {
    return await github.paginate(method, params);
  }
  const response = await method(params);
  return response?.data?.[collectionName] || [];
}

async function listWorkflowRuns(github, params) {
  return await paginate(
    github,
    github.rest.actions.listWorkflowRuns,
    params,
    "workflow_runs",
  );
}

async function listArtifacts(github, params) {
  return await paginate(
    github,
    github.rest.actions.listWorkflowRunArtifacts,
    params,
    "artifacts",
  );
}

async function listJobs(github, params) {
  const actions = github.rest.actions;
  if (params.attempt_number && actions.listJobsForWorkflowRunAttempt) {
    return await paginate(github, actions.listJobsForWorkflowRunAttempt, params, "jobs");
  }
  const { attempt_number: _attempt, ...withoutAttempt } = params;
  return await paginate(github, actions.listJobsForWorkflowRun, withoutAttempt, "jobs");
}

async function getWorkflowRun(github, params) {
  const response = await github.rest.actions.getWorkflowRun(params);
  return response.data;
}

function validateTrustedReusableRun(
  run,
  expectedSha,
  { now = Date.now(), maxAgeDays = MAX_CANDIDATE_AGE_DAYS } = {},
) {
  assertFullSha(expectedSha, "expectedSha");
  if (!run) throw new Error("Candidate run was not found");
  requireRunAttempt(run.run_attempt);
  if (run.event === "workflow_dispatch" || run.event === "pull_request") {
    throw new Error(`Candidate run event ${run.event} is not trusted for release promotion`);
  }
  if (!run.path) throw new Error("Candidate run is missing workflow path metadata");
  if (run.path !== WORKFLOW_PATH) {
    throw new Error(`Candidate run workflow path ${run.path} is not ${WORKFLOW_PATH}`);
  }
  if (!TRUSTED_REUSE_EVENTS.has(run.event)) {
    throw new Error(`Candidate run event ${run.event} is not promotable`);
  }
  if (run.head_sha !== expectedSha) {
    throw new Error("Candidate run SHA does not match tag SHA");
  }
  if (!run.head_branch) throw new Error("Candidate run is missing head_branch metadata");
  if (run.head_branch !== MAIN_BRANCH) {
    throw new Error(`Candidate run branch ${run.head_branch} is not ${MAIN_BRANCH}`);
  }
  if (run.repository?.full_name && run.repository.full_name !== REPO) {
    throw new Error(`Candidate run repository ${run.repository.full_name} is not ${REPO}`);
  }
  if (run.status !== "completed" || run.conclusion !== "success") {
    throw new Error("Candidate run did not complete successfully");
  }
  const completedAt = Date.parse(run.updated_at || run.run_started_at || run.created_at || "");
  if (!Number.isFinite(completedAt)) {
    throw new Error("Candidate run is missing timestamp metadata");
  }
  if (now - completedAt > maxAgeDays * 24 * 60 * 60 * 1000) {
    throw new CandidateUnavailableError(`Candidate run is older than ${maxAgeDays} days`);
  }
}

async function candidateFromRun({
  github,
  owner,
  repo,
  run,
  expectedSha,
  catalog,
}) {
  validateTrustedReusableRun(run, expectedSha);
  const artifacts = await listArtifacts(github, {
    owner,
    repo,
    run_id: run.id,
    per_page: 100,
  });
  const attempt = requireRunAttempt(run.run_attempt);
  const evidence = findOneArtifactByName(artifacts, evidenceArtifactName(attempt));
  requireFreshArtifact(evidence, evidence.name);

  const platformArtifacts = [];
  for (const row of catalog) {
    const artifact = findOneArtifactByName(artifacts, candidateArtifactName(row.binary_name, attempt));
    requireFreshArtifact(artifact, artifact.name);
    platformArtifacts.push(artifact);
  }
  return {
    run,
    evidence,
    artifacts: platformArtifacts,
  };
}

async function discoverCandidateRun({ github, context, sha, catalog = loadCatalog(), core }) {
  assertFullSha(sha, "tag SHA");
  const { owner, repo } = repoParts(context);
  const createdSince = new Date(Date.now() - MAX_CANDIDATE_AGE_DAYS * 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 10);
  const runs = await listWorkflowRuns(github, {
    owner,
    repo,
    workflow_id: path.basename(WORKFLOW_PATH),
    branch: MAIN_BRANCH,
    head_sha: sha,
    created: `>=${createdSince}`,
    status: "completed",
    per_page: 100,
  });
  const sameSha = runs
    .filter((run) => run.head_sha === sha)
    .filter((run) => TRUSTED_REUSE_EVENTS.has(run.event))
    .filter((run) => run.conclusion === "success" && run.status === "completed")
    .sort((a, b) => String(b.run_started_at || b.created_at || "").localeCompare(String(a.run_started_at || a.created_at || "")));

  if (sameSha.length === 0) return null;
  try {
    return await candidateFromRun({
      github,
      owner,
      repo,
      run: sameSha[0],
      expectedSha: sha,
      catalog,
    });
  } catch (error) {
    if (!(error instanceof CandidateUnavailableError)) throw error;
    core?.info?.(
      `Prior candidate run ${sameSha[0].id} attempt ${sameSha[0].run_attempt} unavailable: ${error.message}; building a fresh full candidate automatically. No operator action required.`,
    );
    return null;
  }
}

async function resolveExplicitCandidateRun({ github, context, runId, sha, catalog = loadCatalog() }) {
  const { owner, repo } = repoParts(context);
  if (!/^[1-9][0-9]*$/.test(String(runId || ""))) {
    throw new Error("candidate_run_id must be a numeric workflow run id");
  }
  const run = await getWorkflowRun(github, { owner, repo, run_id: Number(runId) });
  let candidate;
  try {
    candidate = await candidateFromRun({ github, owner, repo, run, expectedSha: sha, catalog });
  } catch (error) {
    if (!(error instanceof CandidateUnavailableError)) throw error;
    throw addAttemptHint(new Error(`Candidate run ${runId} attempt ${run.run_attempt}: ${error.message}`));
  }
  if (!candidate) {
    throw new Error(`Candidate run ${runId} does not contain release-candidate evidence`);
  }
  return candidate;
}

function collectArchiveInventory({ artifactRoot, catalog, sha }) {
  assertFullSha(sha, "candidate SHA");
  const platforms = {};
  for (const row of catalog) {
    const binaryName = row.binary_name;
    const archive = archiveName(binaryName);
    const archivePath = findOneFile(artifactRoot, archive);
    const sidecarPath = findOneFile(artifactRoot, `${archive}.sha256`);
    const metadataPath = findOneFile(artifactRoot, `${binaryName}.json`);
    const metadata = readJson(metadataPath);
    for (const [key, expected] of Object.entries({
      schema_version: 1,
      sha,
      binary_name: binaryName,
      archive,
    })) {
      if (metadata[key] !== expected) {
        throw new Error(`Candidate ${binaryName} metadata ${key} does not match ${expected}`);
      }
    }
    const digest = digestFile(archivePath);
    if (metadata.archive_sha256 !== digest) {
      throw new Error(`Candidate archive digest mismatch for ${archive}`);
    }
    if (typeof metadata.version !== "string" || metadata.version.length === 0) {
      throw new Error(`Candidate ${binaryName} metadata version is missing`);
    }
    if (!/^[0-9a-f]{64}$/.test(String(metadata.executable_sha256 || ""))) {
      throw new Error(`Candidate ${binaryName} executable digest is missing or malformed`);
    }
    assertChecksumSidecar(sidecarPath, digest, archive);
    platforms[binaryName] = {
      runner: row.runner,
      platform: row.platform,
      arch: row.arch || null,
      version: metadata.version,
      executable_sha256: metadata.executable_sha256,
      artifact_id: null,
      artifact_name: null,
      artifact_size: null,
      artifact_digest: null,
      archive,
      archive_sha256: digest,
      sidecar: `${archive}.sha256`,
      metadata: `${binaryName}.json`,
    };
  }
  return platforms;
}

function candidateVersion(platforms, context) {
  const versions = new Set(Object.values(platforms).map((platform) => platform.version));
  if (versions.size !== 1) {
    throw new Error("Candidate platform metadata versions do not match");
  }
  const version = [...versions][0];
  const expectedTagVersion = tagVersion(context);
  if (expectedTagVersion && version !== expectedTagVersion) {
    throw new Error(`Candidate version ${version} does not match tag v${expectedTagVersion}`);
  }
  return version;
}

function attachArtifactMetadata(platforms, catalog, artifacts, runAttempt) {
  const attempt = requireRunAttempt(runAttempt);
  for (const row of catalog) {
    const binaryName = row.binary_name;
    const artifact = findOneArtifactByName(artifacts, candidateArtifactName(binaryName, attempt));
    requireFreshArtifact(artifact, artifact.name);
    platforms[binaryName].artifact_id = artifact.id;
    platforms[binaryName].artifact_name = artifact.name;
    platforms[binaryName].artifact_size = artifact.size_in_bytes || null;
    platforms[binaryName].artifact_digest = artifact.digest;
  }
}

function normalizeRequiredJobs(jobs) {
  return (jobs || []).map((job) => {
    if (typeof job === "string") return { name: job, conclusion: null, status: null };
    return {
      name: String(job.name || ""),
      conclusion: job.conclusion || null,
      status: job.status || null,
    };
  });
}

function selectRequiredJobs(jobs, catalog) {
  const normalized = normalizeRequiredJobs(jobs);
  const selected = [];
  function select(expectedName) {
    const matches = normalized.filter((job) => job.name === expectedName);
    if (matches.length !== 1) {
      const states = matches
        .map((job) => `${job.name}:${job.status || "unknown"}/${job.conclusion || "none"}`)
        .join(", ");
      throw new Error(`Candidate required job evidence expected exactly one ${expectedName}${states ? ` (${states})` : ""}`);
    }
    const [job] = matches;
    if (job.status !== "completed" || job.conclusion !== "success") {
      throw new Error(
        `Candidate required job ${expectedName} was ${job.status || "unknown"}/${job.conclusion || "none"}`,
      );
    }
    selected.push({ name: job.name, status: "completed", conclusion: "success" });
  }
  for (const jobName of REQUIRED_SOURCE_JOB_NAMES) select(jobName);
  for (const row of catalog) select(`${row.binary_name} / Native Candidate Gate`);
  return selected;
}

function assertRequiredJobs(requiredJobs, catalog) {
  const jobs = normalizeRequiredJobs(requiredJobs);
  if (jobs.length === 0) throw new Error("Candidate manifest contains no required job evidence");
  selectRequiredJobs(jobs, catalog);
}

async function qualify({
  github,
  context,
  core,
  artifactRoot = "candidate-artifacts",
  evidencePath = "release-candidate-evidence.json",
}) {
  const catalog = loadCatalog();
  const { owner, repo } = repoParts(context);
  const runId = Number(context.runId || process.env.GITHUB_RUN_ID);
  const runAttempt = Number(context.runAttempt || process.env.GITHUB_RUN_ATTEMPT || 1);
  const sha = context.sha;
  assertFullSha(sha, "candidate SHA");
  if (repoFullName(context) !== REPO) throw new Error(`Release candidates must be produced in ${REPO}`);
  if (!CURRENT_RUN_EVENTS.has(eventName(context))) {
    throw new Error(`Event ${eventName(context)} cannot produce a release candidate manifest`);
  }

  let platforms;
  try {
    platforms = collectArchiveInventory({ artifactRoot, catalog, sha });
  } catch (error) {
    throw addAttemptHint(error);
  }
  const version = candidateVersion(platforms, context);
  const artifacts = await listArtifacts(github, { owner, repo, run_id: runId, per_page: 100 });
  try {
    attachArtifactMetadata(platforms, catalog, artifacts, runAttempt);
  } catch (error) {
    throw addAttemptHint(error);
  }
  const jobs = await listJobs(github, {
    owner,
    repo,
    run_id: runId,
    attempt_number: runAttempt,
    per_page: 100,
  });
  const requiredJobs = selectRequiredJobs(jobs, catalog);

  const evidence = {
    schema_version: 1,
    repo: REPO,
    workflow_path: WORKFLOW_PATH,
    event: eventName(context),
    ref: context.ref,
    head_branch: String(context.ref || "").startsWith("refs/heads/") ? refName(context.ref) : null,
    head_sha: sha,
    version,
    run_id: runId,
    run_attempt: runAttempt,
    full_validation: true,
    generated_at: new Date().toISOString(),
    config_hashes: configHashes(process.cwd()),
    required_jobs: requiredJobs,
    platforms,
  };
  fs.writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  core?.setOutput?.(
    "candidate_artifact_ids",
    Object.values(platforms).map((platform) => String(platform.artifact_id)).join(","),
  );
  core?.setOutput?.("release_version", version);
  core?.setOutput?.("candidate_sha", sha);
  core?.info?.(`Recorded release candidate evidence for ${sha}`);
  return evidence;
}

function assertEvidenceIdentity(evidence, { context, runId }) {
  if (evidence.schema_version !== 1) throw new Error("Unsupported candidate evidence schema");
  if (evidence.repo !== REPO) throw new Error("Candidate evidence repo mismatch");
  if (evidence.workflow_path !== WORKFLOW_PATH) throw new Error("Candidate evidence workflow mismatch");
  if (Number(evidence.run_id) !== Number(runId)) throw new Error("Candidate evidence run_id mismatch");
  if (evidence.head_sha !== context.sha) throw new Error("Candidate evidence SHA does not match tag SHA");
  if (evidence.full_validation !== true) throw new Error("Candidate evidence is not full validation");
  const expectedTagVersion = tagVersion(context);
  if (!expectedTagVersion) throw new Error("Release candidate verification requires a v* tag context");
  if (evidence.version !== expectedTagVersion) {
    throw new Error(`Candidate evidence version ${evidence.version} does not match tag v${expectedTagVersion}`);
  }
}

function assertConfigHashes(evidence, root = process.cwd()) {
  const actual = configHashes(root);
  for (const [file, digest] of Object.entries(actual)) {
    if (evidence.config_hashes?.[file] !== digest) {
      throw new Error(`Candidate config hash mismatch for ${file}`);
    }
  }
}

function assertRunMatchesEvidence(run, evidence, context) {
  const sameRun = Number(evidence.run_id) === Number(context.runId || process.env.GITHUB_RUN_ID);
  if (!run) throw new Error("Workflow run metadata was not found");
  requireRunAttempt(run.run_attempt);
  if (!run.path) throw new Error("Workflow run is missing workflow path metadata");
  if (run.path !== WORKFLOW_PATH) throw new Error(`Workflow run path ${run.path} is not ${WORKFLOW_PATH}`);
  if (!run.head_branch) throw new Error("Workflow run is missing head_branch metadata");
  if (run.repository?.full_name && run.repository.full_name !== REPO) {
    throw new Error(`Workflow run repository ${run.repository.full_name} is not ${REPO}`);
  }
  if (run.head_sha !== evidence.head_sha) throw new Error("Workflow run SHA does not match evidence");
  if (run.event !== evidence.event) throw new Error("Workflow run event does not match evidence");
  if (Number(run.run_attempt) !== Number(evidence.run_attempt)) {
    throw new Error("Workflow run attempt does not match evidence");
  }
  if (sameRun) {
    if (!CURRENT_RUN_EVENTS.has(evidence.event)) {
      throw new Error(`Current run event ${evidence.event} cannot publish a candidate`);
    }
    if (!["in_progress", "completed"].includes(run.status)) {
      throw new Error(`Current workflow run status ${run.status} cannot publish a candidate`);
    }
    if (evidence.event !== eventName(context) || evidence.ref !== context.ref) {
      throw new Error("Current run evidence does not match this workflow invocation");
    }
    return;
  }
  if (evidence.ref !== `refs/heads/${MAIN_BRANCH}` || evidence.head_branch !== MAIN_BRANCH) {
    throw new Error(`Reusable candidate evidence must come from ${MAIN_BRANCH}`);
  }
  validateTrustedReusableRun(run, evidence.head_sha);
}

function assertArtifactMetadata(evidence, catalog, artifacts) {
  const expectedPlatformNames = catalog.map((row) => row.binary_name).sort();
  const actualPlatformNames = Object.keys(evidence.platforms || {}).sort();
  if (JSON.stringify(actualPlatformNames) !== JSON.stringify(expectedPlatformNames)) {
    throw new Error("Candidate platform inventory is not exactly the supported release platforms");
  }
  const names = new Set();
  for (const row of catalog) {
    const platform = evidence.platforms?.[row.binary_name];
    if (!platform) throw new Error(`Candidate evidence missing platform ${row.binary_name}`);
    const expectedArtifactName = candidateArtifactName(row.binary_name, evidence.run_attempt);
    if (platform.artifact_name !== expectedArtifactName) {
      throw new Error(`Candidate artifact name for ${row.binary_name} is not ${expectedArtifactName}`);
    }
    const artifact = artifacts.find((item) => Number(item.id) === Number(platform.artifact_id));
    if (!artifact) throw new Error(`Workflow artifact ${platform.artifact_id} not found for ${row.binary_name}`);
    if (artifact.expired === true) throw new Error(`Workflow artifact ${artifact.name} is expired`);
    if (artifact.name !== platform.artifact_name) {
      throw new Error(`Workflow artifact name mismatch for ${row.binary_name}`);
    }
    if (platform.artifact_size != null && Number(artifact.size_in_bytes) !== Number(platform.artifact_size)) {
      throw new Error(`Workflow artifact size mismatch for ${row.binary_name}`);
    }
    requireArtifactDigest(artifact.digest, `Workflow artifact ${artifact.name}`);
    requireArtifactDigest(platform.artifact_digest, `Candidate evidence artifact ${artifact.name}`);
    if (artifact.digest !== platform.artifact_digest) {
      throw new Error(`Workflow artifact digest mismatch for ${row.binary_name}`);
    }
    names.add(platform.archive);
  }
  const expectedArchives = new Set(catalog.map((row) => archiveName(row.binary_name)));
  if (names.size !== expectedArchives.size || [...names].some((name) => !expectedArchives.has(name))) {
    throw new Error("Candidate archive inventory is not exactly the supported release platforms");
  }
}

function verifyLocalArchiveBytes({ evidence, catalog, artifactRoot, outputRoot }) {
  fs.mkdirSync(outputRoot, { recursive: true });
  const expectedReleaseFiles = new Set(
    catalog.flatMap((row) => {
      const archive = archiveName(row.binary_name);
      return [archive, `${archive}.sha256`, `${row.binary_name}.json`];
    }),
  );
  const unexpectedReleaseFiles = walkFiles(artifactRoot)
    .map((file) => path.basename(file))
    .filter((name) => /^apm-/.test(name))
    .filter((name) => /\.(tar\.gz|zip|sha256|json)$/.test(name))
    .filter((name) => !expectedReleaseFiles.has(name));
  if (unexpectedReleaseFiles.length) {
    throw new Error(`Candidate contains unexpected release files: ${unexpectedReleaseFiles.join(", ")}`);
  }
  const localInventory = collectArchiveInventory({
    artifactRoot,
    catalog,
    sha: evidence.head_sha,
  });
  const localVersion = candidateVersion(localInventory, { eventName: "push", ref: `refs/tags/v${evidence.version}` });
  if (localVersion !== evidence.version) {
    throw new Error("Candidate local archive versions do not match evidence");
  }
  for (const row of catalog) {
    const binaryName = row.binary_name;
    const platform = evidence.platforms[binaryName];
    const archive = platform.archive;
    if (localInventory[binaryName].archive_sha256 !== platform.archive_sha256) {
      throw new Error(`Candidate archive digest mismatch for ${archive}`);
    }
    if (localInventory[binaryName].version !== platform.version) {
      throw new Error(`Candidate version mismatch for ${archive}`);
    }
    if (localInventory[binaryName].executable_sha256 !== platform.executable_sha256) {
      throw new Error(`Candidate executable digest mismatch for ${archive}`);
    }
    const archivePath = findOneFile(artifactRoot, archive);
    const sidecarPath = findOneFile(artifactRoot, `${archive}.sha256`);
    const digest = digestFile(archivePath);
    if (platform.archive_sha256 !== digest) {
      throw new Error(`Candidate archive digest mismatch for ${archive}`);
    }
    assertChecksumSidecar(sidecarPath, digest, archive);
    fs.copyFileSync(archivePath, path.join(outputRoot, archive));
    fs.copyFileSync(sidecarPath, path.join(outputRoot, `${archive}.sha256`));
  }
}

function resolveEvidencePath(evidencePath) {
  if (fs.existsSync(evidencePath) && fs.statSync(evidencePath).isDirectory()) {
    return findOneFile(evidencePath, "release-candidate-evidence.json");
  }
  return evidencePath;
}

async function verify({
  github,
  context,
  core,
  runId = process.env.CANDIDATE_RUN_ID || context.runId || process.env.GITHUB_RUN_ID,
  artifactRoot = "candidate-artifacts",
  evidencePath = "candidate-evidence",
  outputRoot = "release-assets",
}) {
  const catalog = loadCatalog();
  const { owner, repo } = repoParts(context);
  const evidence = readJson(resolveEvidencePath(evidencePath));
  assertEvidenceIdentity(evidence, { context, runId });
  assertConfigHashes(evidence, process.cwd());
  assertRequiredJobs(evidence.required_jobs, catalog);

  const run = await getWorkflowRun(github, { owner, repo, run_id: Number(runId) });
  assertRunMatchesEvidence(run, evidence, context);
  const jobs = await listJobs(github, {
    owner,
    repo,
    run_id: Number(runId),
    attempt_number: Number(evidence.run_attempt),
    per_page: 100,
  });
  selectRequiredJobs(jobs, catalog);
  const artifacts = await listArtifacts(github, {
    owner,
    repo,
    run_id: Number(runId),
    per_page: 100,
  });
  assertArtifactMetadata(evidence, catalog, artifacts);
  verifyLocalArchiveBytes({ evidence, catalog, artifactRoot, outputRoot });
  core?.info?.(`Verified release candidate ${runId} for ${context.sha}`);
  return evidence;
}

async function plan({ github, context, core }) {
  const catalog = loadCatalog();
  const { matrix, fullValidation } = matrixForContext(context, catalog);
  let candidateRunId = "";
  let candidateArtifactIds = "";
  let candidateEvidenceArtifactId = "";
  let decision = `fresh ${fullValidation ? "full qualification" : "platform validation"}; ${eventName(context)} uses the current run`;
  if (isTagPush(context)) {
    const candidate = await discoverCandidateRun({ github, context, sha: context.sha, catalog, core });
    decision = "fresh full qualification; no reusable candidate is available for this SHA";
    if (candidate) {
      candidateRunId = String(candidate.run.id);
      candidateArtifactIds = candidate.artifacts.map((artifact) => String(artifact.id)).join(",");
      candidateEvidenceArtifactId = String(candidate.evidence.id);
      decision = `reuse; trusted exact-SHA candidate from source run ${candidate.run.id} attempt ${candidate.run.run_attempt} SHA ${candidate.run.head_sha}`;
    }
  }
  core?.info?.(`Release plan: ${decision}.`);
  core.setOutput("matrix", JSON.stringify(matrix));
  core.setOutput("full_validation", String(fullValidation));
  core.setOutput("candidate_run_id", candidateRunId);
  core.setOutput("candidate_artifact_ids", candidateArtifactIds);
  core.setOutput("candidate_evidence_artifact_id", candidateEvidenceArtifactId);
  core.setOutput("is_prerelease", String(isPrereleaseTag(context)));
}

module.exports = {
  CONFIG_HASH_FILES,
  REQUIRED_SOURCE_JOB_NAMES,
  REQUIRED_SOURCE_JOB_FRAGMENTS,
  TRUSTED_REUSE_EVENTS,
  WORKFLOW_PATH,
  archiveName,
  assertRequiredJobs,
  candidateFromRun,
  candidateVersion,
  collectArchiveInventory,
  configHashes,
  discoverCandidateRun,
  evidenceArtifactName,
  matrixForContext,
  plan,
  qualify,
  resolveEvidencePath,
  resolveExplicitCandidateRun,
  selectRequiredJobs,
  candidateArtifactName,
  validateTrustedReusableRun,
  verifyLocalArchiveBytes,
  verify,
};
