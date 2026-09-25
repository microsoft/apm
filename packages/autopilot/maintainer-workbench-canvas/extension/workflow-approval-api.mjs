import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { REPO } from "./config.mjs";

const exec = promisify(execFile);
const integer = value => Number.isSafeInteger(value) && value > 0;
const prefix = `repos/${REPO}`;

export class ApprovalApiError extends Error {
  constructor(message, { status = null, ambiguous = false } = {}) {
    super(message);
    this.status = status;
    this.ambiguous = ambiguous;
  }
}

async function ghRequest(method, path) {
  let stdout;
  try {
    ({ stdout } = await exec("gh", ["api", "--hostname", "github.com", "--method", method,
      "--include", path, "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"], {
      timeout: 30000, maxBuffer: 12 * 1024 * 1024,
      env: { ...process.env, GH_PROMPT_DISABLED: "1", GH_PAGER: "cat" },
    }));
  } catch (error) {
    const match = String(error.stderr || "").match(/\(HTTP (\d{3})\)/);
    const status = match ? Number(match[1]) : null;
    const ambiguous = method === "POST" && (!status || status >= 500);
    throw new ApprovalApiError(ambiguous
      ? "GitHub did not confirm the approval outcome. Check the run on GitHub before any new approval."
      : `GitHub ${method === "POST" ? "approval" : "read"} failed${status ? ` (HTTP ${status})` : " (timeout or account access)"}. ${
        status === 403 ? "Check repository access and the token's Actions write permission." : "Check GitHub access and try the read again."}`,
    { status, ambiguous });
  }
  const status = Number(stdout.match(/^HTTP\/[\d.]+\s+(\d{3})/m)?.[1]);
  const separator = /\r?\n\r?\n/.exec(stdout);
  if (!status || !separator) throw new ApprovalApiError("GitHub returned an unrecognized response; no success is assumed.", { ambiguous: method === "POST" });
  const body = stdout.slice(separator.index + separator[0].length).trim();
  let data = null;
  try { data = body ? JSON.parse(body) : null; }
  catch { throw new ApprovalApiError("GitHub returned an unreadable response; check the run before retrying.", { status, ambiguous: method === "POST" }); }
  return { status, data };
}

export class WorkflowApprovalApi {
  constructor(request = ghRequest) { this.request = request; }

  async get(path) {
    const response = await this.request("GET", path);
    if (response.status !== 200 || !response.data) throw new ApprovalApiError("GitHub read did not return complete data.", { status: response.status });
    return response.data;
  }

  async identity(number) {
    if (!integer(number)) throw new Error("Invalid PR number.");
    const [actor, repository, pr] = await Promise.all([
      this.get("user"), this.get(prefix), this.get(`${prefix}/pulls/${number}`),
    ]);
    return { actor, repository, pr };
  }

  async pages(path, field = null) {
    const nodes = [];
    for (let page = 1; page <= 10; page++) {
      const result = await this.get(`${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`);
      const batch = field ? result[field] : result;
      if (!Array.isArray(batch)) throw new Error("GitHub returned incomplete approval evidence.");
      nodes.push(...batch);
      if (batch.length < 100 || field && Number.isSafeInteger(result.total_count) && nodes.length >= result.total_count) {
        if (field && (!Number.isSafeInteger(result.total_count) || nodes.length < result.total_count)) {
          throw new Error("Workflow pagination was incomplete. Open GitHub to inspect approval.");
        }
        return nodes;
      }
    }
    throw new Error("Approval evidence exceeded the bounded read. Open GitHub to inspect it.");
  }

  async context(number) {
    const identity = await this.identity(number);
    const { pr } = identity;
    if (!/^[a-f0-9]{40}$/.test(pr?.head?.sha || "") ||
        !/^[A-Za-z0-9-]+$/.test(pr.head.repo?.owner?.login || "") ||
        typeof pr.head.ref !== "string" || !pr.head.ref.length || pr.head.ref.length > 255) {
      throw new Error("PR head identity is unavailable. Open GitHub to inspect approval.");
    }
    const head = encodeURIComponent(`${pr.head.repo.owner.login}:${pr.head.ref}`);
    const [runs, matchingPulls] = await Promise.all([
      this.pages(`${prefix}/actions/runs?head_sha=${pr.head.sha}`, "workflow_runs"),
      this.pages(`${prefix}/pulls?state=open&head=${head}`),
    ]);
    return { ...identity, runs, matchingPulls };
  }

  async run(id) {
    if (!integer(id)) throw new Error("Invalid workflow run ID.");
    return this.get(`${prefix}/actions/runs/${id}`);
  }

  async approve(id) {
    if (!integer(id)) throw new Error("Invalid workflow run ID.");
    const response = await this.request("POST", `${prefix}/actions/runs/${id}/approve`);
    // GitHub's official OpenAPI contract returns 201, often with no body.
    if (response.status !== 201) throw new ApprovalApiError("GitHub did not return a confirmed approval receipt. Inspect the run before retrying.",
      { status: response.status, ambiguous: true });
    return { status: response.status };
  }
}
