const BASE = "";

async function fetchJson(url, opts) {
  const res = await fetch(BASE + url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
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
  return fetchJson("/start-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number, title }),
  });
}

export async function openSession(number, title) {
  return fetchJson("/open-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number, title }),
  });
}

export async function runPanel(number) {
  return fetchJson("/run-panel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number }),
  });
}

export async function rerunCi(number) {
  return fetchJson("/approve-pipeline", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number }),
  });
}

export async function approvePr(number) {
  return fetchJson("/approve-pr", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number }),
  });
}

export async function approveWorkflowRuns(branch) {
  return fetchJson("/approve-workflow-runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ branch }),
  });
}

export async function mergeWhenReady(number) {
  return fetchJson("/merge-when-ready", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number }),
  });
}

export async function submitComment(type, number, body) {
  return fetchJson("/submit-comment", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, number, body }),
  });
}

export async function refineComment(type, number, draft, title) {
  return fetchJson("/refine-comment", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, number, draft, title }),
  });
}

export async function getDraft(type, number) {
  return fetchJson(`/api/draft?type=${type}&number=${number}`);
}

export async function getPermissions() {
  return fetchJson("/api/permissions");
}
