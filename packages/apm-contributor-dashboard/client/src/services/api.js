const BASE = "";

function getCanvasToken() {
  return (typeof window !== "undefined" && window.__CANVAS_TOKEN__) || "";
}

async function fetchJson(url, opts) {
  const res = await fetch(BASE + url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function postJson(url, body) {
  return fetchJson(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Canvas-Token": getCanvasToken(),
    },
    body: JSON.stringify(body),
  });
}

export async function getIssues() {
  return fetchJson("/api/issues");
}

export async function getPrs() {
  return fetchJson("/api/prs");
}

export async function getIssueDetail(number) {
  return fetchJson(`/api/issue/${number}`);
}

export async function getPrDetail(number) {
  return fetchJson(`/api/pr/${number}`);
}

export async function startSession(number, title) {
  return postJson("/start-session", { number, title });
}

export async function startTriageSession(number, title) {
  return postJson("/start-session", { number, title, model: "claude-opus-4.6" });
}

export async function openSession(number, title) {
  return postJson("/open-session", { number, title });
}

export async function runPanel(number) {
  return postJson("/run-panel", { number });
}

export async function rerunCi(number) {
  return postJson("/approve-pipeline", { number });
}

export async function approvePr(number) {
  return postJson("/approve-pr", { number });
}

export async function approveWorkflowRuns(branch) {
  return postJson("/approve-workflow-runs", { branch });
}

export async function mergeWhenReady(number) {
  return postJson("/merge-when-ready", { number });
}

export async function submitComment(type, number, body) {
  return postJson("/submit-comment", { type, number, body });
}

export async function refineComment(type, number, draft, title) {
  return postJson("/refine-comment", { type, number, draft, title });
}

export async function getDraft(type, number) {
  return fetchJson(`/api/draft?type=${type}&number=${number}`);
}

export async function getPermissions() {
  return fetchJson("/api/permissions");
}

export async function createFollowUpIssues(prNumber, panelReview) {
  return postJson("/create-follow-up-issues", { number: prNumber, panelReview });
}

export async function getTriageData() {
  return fetchJson("/api/triage");
}
