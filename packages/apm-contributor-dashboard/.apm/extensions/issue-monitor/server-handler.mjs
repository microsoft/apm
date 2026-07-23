// Extracted HTTP request handler with dependency injection for testability.
// Dependencies are passed via the `deps` object so tests can substitute mocks.
//
// Usage in extension.mjs:
//   import { createHandler } from "./server-handler.mjs";
//   const handler = createHandler({ ghExec, session, startedSessions, ... });
//   const server = createServer(handler);

import { readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";
import { randomBytes } from "node:crypto";
import { parsePanelReview, extractFollowUpItems } from "./logic.mjs";

// Sanitize user-controlled strings before interpolation into session prompts.
// Strips backticks and angle brackets to prevent prompt injection via
// adversary-controlled GitHub issue/PR titles.
function sanitizeForPrompt(str) {
    if (typeof str !== "string") return "";
    return str.replace(/[`<>]/g, "").slice(0, 200);
}

// Validate that a model identifier is safe for interpolation inside a double-quoted string.
// Accepts alphanumeric chars, dots, dashes, underscores, forward slashes, and colons --
// covering IDs like "claude-opus-4.6", "org/model:tag", "gpt-5.4-mini".
// Rejects anything containing whitespace, quotes, or other shell-significant characters.
// Returns empty string for any value that does not match, suppressing the model clause.
function sanitizeModel(str) {
    if (typeof str !== "string" || !str) return "";
    return /^[\w.\-/:]{1,100}$/.test(str) ? str : "";
}

// Validate that a value is a well-formed UUID before embedding it in a prompt.
// Session IDs from the Copilot app are always UUIDs; anything else is rejected
// to prevent prompt injection via a malicious or corrupted persisted session ID.
function isValidSessionId(id) {
    return typeof id === "string" &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
}

const MIME_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
};

function isRelativePathUnsafe(relPath) {
    return relPath === ".." || relPath.startsWith(`..${sep}`) || isAbsolute(relPath);
}

function isPathWithinDist(root, candidatePath) {
    try {
        const canonicalRoot = realpathSync(root);
        try {
            const canonicalCandidate = realpathSync(candidatePath);
            return !isRelativePathUnsafe(relative(canonicalRoot, canonicalCandidate));
        } catch (error) {
            if (error.code !== "ENOENT" && error.code !== "ENOTDIR") {
                return null;
            }
            try {
                const parent = dirname(candidatePath);
                const canonicalParent = realpathSync(parent);
                return !isRelativePathUnsafe(relative(canonicalRoot, canonicalParent));
            } catch {
                return null;
            }
        }
    } catch {
        return null;
    }
}

// Write endpoints that perform state-changing operations
const WRITE_ENDPOINTS = new Set([
    "/start-session", "/open-session", "/run-panel", "/approve-pipeline",
    "/approve-pr", "/approve-workflow-runs", "/merge-when-ready",
    "/submit-comment", "/refine-comment", "/create-follow-up-issues",
]);

function serveStatic(res, filePath) {
    try {
        const data = readFileSync(filePath);
        const ext = filePath.slice(filePath.lastIndexOf("."));
        const mime = MIME_TYPES[ext] || "application/octet-stream";
        const cache = ext === ".html" ? "no-cache" : "public, max-age=31536000, immutable";
        res.writeHead(200, { "Content-Type": mime, "Cache-Control": cache });
        res.end(data);
    } catch {
        res.writeHead(404);
        res.end("Not found");
    }
}

function readBody(req) {
    return new Promise((resolve) => {
        let body = "";
        req.on("data", (chunk) => { body += chunk; });
        req.on("end", () => resolve(body));
    });
}

/**
 * Create an HTTP request handler with injected dependencies.
 *
 * @param {object} deps
 * @param {function} deps.ghExec - async (args: string[]) => string
 * @param {object}  deps.session - { send(payload) }
 * @param {Set}     deps.startedSessions - Set<number>
 * @param {Map}     deps.sessionIds - Map<number, string> issue number -> project_session_id
 * @param {function} deps.saveSessions - () => void
 * @param {function} deps.getIssueData - () => array
 * @param {function} deps.getPrData - () => array
 * @param {function} deps.getLastUpdated - () => string|null
 * @param {function} deps.getLastError - () => string|null
 * @param {string}  deps.repo - e.g. "microsoft/apm"
 * @param {string}  deps.distDir - absolute path to dist/ folder
 */
export function createHandler(deps) {
    const { ghExec, session, startedSessions, sessionIds = new Map(), saveSessions, getIssueData, getPrData, getLastUpdated, getLastError, repo, distDir } = deps;

    // CSRF token -- generated once per server lifetime, embedded in index.html
    const csrfToken = deps.csrfToken || randomBytes(32).toString("hex");

    // In-memory draft store for agent-refined comment text (keyed by "issue-123" / "pr-456")
    const drafts = new Map();

    const handler = async function handler(req, res) {
        // CSRF protection for write endpoints
        const csrfPath = req.url.split("?")[0];
        if (req.method === "POST" && WRITE_ENDPOINTS.has(csrfPath)) {
            const origin = req.headers.origin || "";
            const host = req.headers.host || "";
            // Reject cross-origin requests (only allow localhost)
            if (origin && !origin.match(/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/)) {
                res.writeHead(403, { "Content-Type": "application/json" });
                res.end(JSON.stringify({ ok: false, error: "Forbidden: cross-origin request" }));
                return;
            }
            // Validate CSRF token
            const token = req.headers["x-canvas-token"];
            if (token !== csrfToken) {
                res.writeHead(403, { "Content-Type": "application/json" });
                res.end(JSON.stringify({ ok: false, error: "Forbidden: invalid or missing CSRF token" }));
                return;
            }
        }

        // POST /start-session
        if (req.method === "POST" && req.url === "/start-session") {
            const raw = await readBody(req);
            try {
                const { number, title, model } = JSON.parse(raw);
                startedSessions.add(number);
                saveSessions();
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
                const safeTitle = sanitizeForPrompt(title);
                const safeModel = sanitizeModel(model);
                const modelClause = safeModel ? ` Use model "${safeModel}".` : "";
                setTimeout(() => {
                    session.send({
                        prompt: `Open a new session for issue #${number} ("Title: ${safeTitle}") in ${repo}. Use the open_issue_session tool with repo_full_name "${repo}", issue_number ${number}, issue_title "#${number} ${safeTitle}", and kickoff_mode "plan".${modelClause} The session should plan the implementation of this issue. After the session is created, immediately call the register_session canvas action with the new session's project_session_id and issue_number ${number} so the dashboard can navigate directly next time.`,
                    });
                }, 0);
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /open-session
        if (req.method === "POST" && req.url === "/open-session") {
            const raw = await readBody(req);
            try {
                const { number, title } = JSON.parse(raw);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
                const safeTitle = sanitizeForPrompt(title);
                setTimeout(() => {
                    const knownId = sessionIds.get(number);
                    if (knownId && isValidSessionId(knownId)) {
                        // Direct navigate -- no session lookup needed, single tool call
                        session.send({ prompt: `Call navigate_to with id="${knownId}". No other response needed.` });
                    } else {
                        // Fallback: search and navigate, then register for next time
                        session.send({
                            prompt: `Navigate to the existing session for issue #${number} ("${safeTitle}") in ${repo}. Use the list_sessions_and_chats tool to find a session linked to issue #${number}, then use navigate_to with its project_session_id to open it. After navigating, call the register_session canvas action with that project_session_id and issue_number ${number}.`,
                        });
                    }
                }, 0);
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // GET /api/issues
        if (req.url === "/api/issues") {
            res.setHeader("Content-Type", "application/json");
            const enriched = getIssueData().map(i => ({ ...i, hasSession: startedSessions.has(i.number) }));
            res.end(JSON.stringify({ issues: enriched, lastUpdated: getLastUpdated(), error: getLastError() }));
            return;
        }

        // GET /api/prs
        if (req.url === "/api/prs") {
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ prs: getPrData(), lastUpdated: getLastUpdated(), error: getLastError() }));
            return;
        }

        // GET /api/issue/:n
        const issueMatch = req.url.match(/^\/api\/issue\/(\d+)$/);
        if (issueMatch) {
            const num = issueMatch[1];
            res.setHeader("Content-Type", "application/json");
            try {
                const out = await ghExec([
                    "issue", "view", num,
                    "--repo", repo,
                    "--json", "number,title,body,author,labels,state,createdAt,updatedAt,comments",
                ]);
                const data = JSON.parse(out);
                const rawComments = Array.isArray(data.comments) ? data.comments : [];
                const commentList = rawComments.map(c => ({
                    id: c.url || c.id || "",
                    author: c.author?.login || "unknown",
                    body: c.body || "",
                    createdAt: c.createdAt || "",
                    url: c.url || "",
                    isBot: /\[bot\]/.test(c.author?.login || ""),
                    isTriagePanel: (c.body || "").includes("```json triage-decision"),
                }));
                res.end(JSON.stringify({
                    number: data.number,
                    title: data.title,
                    body: data.body,
                    author: data.author?.login || "unknown",
                    labels: (data.labels || []).map(l => l.name),
                    state: data.state,
                    createdAt: data.createdAt,
                    updatedAt: data.updatedAt,
                    comments: rawComments.length,
                    commentList,
                }));
            } catch (e) {
                res.end(JSON.stringify({ error: String(e.message || e) }));
            }
            return;
        }

        // GET /api/pr/:n
        const prMatch = req.url.match(/^\/api\/pr\/(\d+)$/);
        if (prMatch) {
            const num = prMatch[1];
            res.setHeader("Content-Type", "application/json");
            try {
                const out = await ghExec([
                    "pr", "view", num,
                    "--repo", repo,
                    "--json", "number,title,body,author,labels,state,isDraft,reviewDecision,headRefName,createdAt,updatedAt,comments,reviews,statusCheckRollup",
                ]);
                const data = JSON.parse(out);
                const rawComments = Array.isArray(data.comments) ? data.comments : [];
                const rawReviews = Array.isArray(data.reviews) ? data.reviews : [];
                const activity = [];
                for (const c of rawComments) {
                    activity.push({ kind: "comment", author: c.author?.login || "unknown", body: c.body || "", createdAt: c.createdAt, url: c.url || "" });
                }
                for (const r of rawReviews) {
                    if (!r.body && r.state === "COMMENTED") continue;
                    activity.push({ kind: "review", author: r.author?.login || "unknown", body: r.body || "", state: r.state || "", createdAt: r.submittedAt || r.createdAt, url: r.url || "" });
                }
                activity.sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""));
                const panelReview = parsePanelReview(rawComments);
                const branch = data.headRefName || "";
                let workflowRuns = [];
                if (branch) {
                    try {
                        const runsOut = await ghExec(["run", "list", "--repo", repo, "--branch", branch, "--limit", "20", "--json", "databaseId,name,status,conclusion,headSha,createdAt,updatedAt,url,event"]);
                        workflowRuns = JSON.parse(runsOut);
                    } catch (_) { /* ignore */ }
                }
                const rawChecks = Array.isArray(data.statusCheckRollup) ? data.statusCheckRollup : [];
                const checks = rawChecks.map(c => ({ name: c.name || c.context || "unknown", status: c.status || "", conclusion: c.conclusion || "", url: c.detailsUrl || c.targetUrl || "" }));
                res.end(JSON.stringify({
                    number: data.number, title: data.title, body: data.body,
                    author: data.author?.login || "unknown",
                    labels: (data.labels || []).map(l => l.name),
                    state: data.state, isDraft: data.isDraft,
                    reviewDecision: data.reviewDecision || "", branch,
                    createdAt: data.createdAt, updatedAt: data.updatedAt,
                    commentCount: rawComments.length,
                    activity, panelReview, checks, workflowRuns,
                }));
            } catch (e) {
                res.end(JSON.stringify({ error: String(e.message || e) }));
            }
            return;
        }

        // POST /run-panel
        if (req.method === "POST" && req.url === "/run-panel") {
            const raw = await readBody(req);
            try {
                const { number } = JSON.parse(raw);
                try {
                    const branchOut = await ghExec(["pr", "view", String(number), "--repo", repo, "--json", "headRefName"]);
                    const branch = JSON.parse(branchOut).headRefName;
                    if (branch) {
                        const runsOut = await ghExec(["run", "list", "--repo", repo, "--branch", branch, "--limit", "10", "--json", "databaseId,conclusion"]);
                        const runs = JSON.parse(runsOut);
                        for (const run of runs.filter(r => r.conclusion === "action_required")) {
                            try { await ghExec(["api", `repos/${repo}/actions/runs/${run.databaseId}/approve`, "-X", "POST"]); } catch (_) {}
                        }
                    }
                } catch (_) {}
                await ghExec(["pr", "edit", String(number), "--repo", repo, "--add-label", "panel-review"]);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /approve-pipeline
        if (req.method === "POST" && req.url === "/approve-pipeline") {
            const raw = await readBody(req);
            try {
                const { number } = JSON.parse(raw);
                const checksOut = await ghExec(["pr", "checks", String(number), "--repo", repo, "--json", "name,state,link"]);
                const checks = JSON.parse(checksOut);
                const failed = checks.filter(c => c.state === "FAILURE" || c.state === "ERROR");
                const runIds = new Set();
                for (const c of failed) {
                    const m = (c.link || "").match(/\/runs\/(\d+)/);
                    if (m) runIds.add(m[1]);
                }
                let reran = 0;
                for (const runId of runIds) {
                    try { await ghExec(["run", "rerun", runId, "--repo", repo, "--failed"]); reran++; } catch (_) {}
                }
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true, reran }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /approve-pr
        if (req.method === "POST" && req.url === "/approve-pr") {
            const raw = await readBody(req);
            try {
                const { number } = JSON.parse(raw);
                await ghExec(["pr", "review", String(number), "--repo", repo, "--approve"]);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /approve-workflow-runs
        if (req.method === "POST" && req.url === "/approve-workflow-runs") {
            const raw = await readBody(req);
            try {
                const { branch } = JSON.parse(raw);
                const runsOut = await ghExec(["run", "list", "--repo", repo, "--branch", branch, "--limit", "10", "--json", "databaseId,conclusion"]);
                const runs = JSON.parse(runsOut);
                const pending = runs.filter(r => r.conclusion === "action_required");
                let approved = 0;
                for (const run of pending) {
                    try { await ghExec(["api", `repos/${repo}/actions/runs/${run.databaseId}/approve`, "-X", "POST"]); approved++; } catch (_) {}
                }
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true, approved }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /merge-when-ready
        if (req.method === "POST" && req.url === "/merge-when-ready") {
            const raw = await readBody(req);
            try {
                const { number } = JSON.parse(raw);
                await ghExec(["pr", "merge", String(number), "--repo", repo, "--auto", "--squash"]);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
            return;
        }

        // POST /submit-comment -- post a comment to an issue or PR via gh CLI
        if (req.method === "POST" && req.url === "/submit-comment") {
            const raw = await readBody(req);
            try {
                const { type, number, body } = JSON.parse(raw);
                const cmd = type === "pr" ? "pr" : "issue";
                await ghExec([cmd, "comment", String(number), "--repo", repo, "--body", body]);
                drafts.delete(`${type}-${number}`);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e.message || e) }));
            }
            return;
        }

        // POST /refine-comment -- send a draft to the chat session for agent refinement
        if (req.method === "POST" && req.url === "/refine-comment") {
            const raw = await readBody(req);
            try {
                const { type, number, draft, title } = JSON.parse(raw);
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
                const label = type === "pr" ? "PR" : "Issue";
                const safeTitle = sanitizeForPrompt(title);
                const safeDraft = sanitizeForPrompt(draft);
                setTimeout(() => {
                    session.send({
                        prompt: `The user is drafting a comment for ${label} #${number} ("${safeTitle}") in ${repo}. Please help refine this draft. When you have a final version, use invoke_canvas_action with instanceId "apm-dashboard", actionName "update-draft", and input { "type": "${type}", "number": ${number}, "text": "<your refined text>" } to push it back to the composer.\n\nCurrent draft:\n\n${safeDraft}`,
                    });
                }, 0);
            } catch (e) {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: false, error: String(e.message || e) }));
            }
            return;
        }

        // GET /api/permissions -- return the user's GitHub permissions on the repo (cached)
        if (req.url === "/api/permissions") {
            if (!handler._permissionsCache) {
                try {
                    const raw = await ghExec(["api", `repos/${repo}`, "--jq", ".permissions"]);
                    handler._permissionsCache = JSON.parse(raw);
                } catch {
                    handler._permissionsCache = { pull: true, triage: false, push: false, maintain: false, admin: false };
                }
            }
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify(handler._permissionsCache));
            return;
        }

        // POST /create-follow-up-issues -- create GitHub issues from panel review deferred/recommended items
        if (req.method === "POST" && req.url === "/create-follow-up-issues") {
            const raw = await readBody(req);
            res.setHeader("Content-Type", "application/json");
            try {
                const { number, panelReview } = JSON.parse(raw);
                const followUps = extractFollowUpItems(panelReview, number);
                if (followUps.length === 0) {
                    res.end(JSON.stringify({ ok: true, created: [], message: "No follow-up items found in the panel review" }));
                    return;
                }
                const created = [];
                for (const item of followUps) {
                    try {
                        const args = ["issue", "create", "--repo", repo, "--title", item.title, "--body", item.body];
                        for (const label of item.labels) {
                            args.push("--label", label);
                        }
                        const out = await ghExec(args);
                        const urlMatch = (out || "").match(/https:\/\/github\.com\/[^\s]+/);
                        created.push({ title: item.title, url: urlMatch ? urlMatch[0] : "" });
                    } catch (e) {
                        created.push({ title: item.title, error: String(e.message || e) });
                    }
                }
                res.end(JSON.stringify({ ok: true, created }));
            } catch (e) {
                res.end(JSON.stringify({ ok: false, error: String(e.message || e) }));
            }
            return;
        }

        // GET /api/triage -- fetch issues with triage-decision comments (lazy, cached)
        // Performance: single GraphQL query fetches all open issues + their recent comments
        // in one round trip (vs the old N+1 approach of one gh-issue-view call per issue).
        if (req.url === "/api/triage") {
            res.setHeader("Content-Type", "application/json");
            const TRIAGE_TTL_MS = 5 * 60 * 1000;
            const cache = handler._triageCache;
            if (cache && (Date.now() - cache.fetchedAt) < TRIAGE_TTL_MS) {
                const items = cache.items.map(i => ({ ...i, hasSession: startedSessions.has(i.number) }));
                res.end(JSON.stringify({ items, lastUpdated: cache.lastUpdated, total: items.length }));
                return;
            }
            try {
                const [owner, repoName] = repo.split("/");
                // Paginate: GitHub GraphQL returns max 100 issues per page.
                // Two pages covers up to 200 open issues which is more than enough.
                const gql = `
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, states: [OPEN], after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
      body
      labels(first: 10) { nodes { name } }
      comments(last: 20) {
        nodes {
          body
          createdAt
          author { login avatarUrl }
          isMinimized
        }
      }
      }
    }
  }
}`.trim();
                const triageItems = [];
                let cursor = null;
                let pages = 0;
                while (pages < 2) {
                    const args = ["api", "graphql", "-f", `query=${gql}`, "-F", `owner=${owner}`, "-F", `repo=${repoName}`];
                    if (cursor) args.push("-F", `cursor=${cursor}`);
                    const gqlOut = await ghExec(args);
                    const gqlData = JSON.parse(gqlOut);
                    const issuesPage = gqlData?.data?.repository?.issues;
                    if (!issuesPage) break;
                    for (const issue of issuesPage.nodes) {
                        const comments = issue.comments?.nodes || [];
                        let triageComment = null;
                        for (const c of comments) {
                            const body = c.body || "";
                            const m = body.match(/```json\s+triage-decision\s*\n([\s\S]*?)\n```/);
                            if (!m) continue;
                            try {
                                const td = JSON.parse(m[1]);
                                triageComment = { comment: c, td };
                                break;
                            } catch (_) { /* malformed JSON */ }
                        }
                        if (!triageComment) continue;
                        const { comment: c, td } = triageComment;
                        const nonTriageComments = comments
                            .filter(x => x !== c && !x.isMinimized)
                            .map(x => ({ author: x.author?.login || "ghost", createdAt: x.createdAt, body: x.body || "" }));
                        triageItems.push({
                            number: issue.number,
                            title: (issue.title || "").slice(0, 90),
                            url: issue.url || "",
                            issueBody: issue.body || "",
                            labels: (issue.labels?.nodes || []).map(l => l.name),
                            triageAuthor: c.author?.login || "unknown",
                            triageCreatedAt: c.createdAt || "",
                            commentBody: c.body,
                            commentMarkdown: td.comment_markdown || "",
                            nonTriageComments,
                            decision: td.decision || "",
                            decisionDetail: td.decision_detail || "",
                            theme: td.theme || "",
                            areas: Array.isArray(td.areas) ? td.areas : [],
                            type: td.type || "",
                            status: td.status || "",
                            priority: td.priority || "",
                            milestone: td.milestone || "",
                            nextAction: td.next_action || "",
                            preservedLabels: Array.isArray(td.preserved_labels) ? td.preserved_labels : [],
                        });
                    }
                    if (!issuesPage.pageInfo.hasNextPage) break;
                    cursor = issuesPage.pageInfo.endCursor;
                    pages++;
                }
                const lastUpdated = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
                handler._triageCache = { items: triageItems, fetchedAt: Date.now(), lastUpdated };
                const enriched = triageItems.map(i => ({ ...i, hasSession: startedSessions.has(i.number) }));
                res.end(JSON.stringify({ items: enriched, lastUpdated, total: enriched.length }));
            } catch (e) {
                res.end(JSON.stringify({ items: [], error: String(e.message || e) }));
            }
            return;
        }

        // GET /api/draft -- poll for agent-updated draft text
        if (req.url?.startsWith("/api/draft")) {
            const url = new URL(req.url, "http://localhost");
            const key = `${url.searchParams.get("type")}-${url.searchParams.get("number")}`;
            const text = drafts.get(key) || "";
            if (text) drafts.delete(key);
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ text }));
            return;
        }

        // Static file serving from dist/
        const urlPath = decodeURIComponent(req.url.split("?")[0]);
        if (urlPath === "/" || urlPath === "/index.html") {
            // Inject CSRF token into HTML so the client can send it with write requests
            try {
                let html = readFileSync(join(distDir, "index.html"), "utf-8");
                const tokenScript = `<script>window.__CANVAS_TOKEN__="${csrfToken}";</script>`;
                html = html.replace("</head>", `${tokenScript}</head>`);
                res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-cache" });
                res.end(html);
            } catch {
                res.writeHead(404);
                res.end("Not found");
            }
        } else if (urlPath.startsWith("/assets/")) {
            const root = resolve(distDir);
            const resolved = resolve(root, normalize(urlPath.slice(1)));
            const rel = relative(root, resolved);
            if (isRelativePathUnsafe(rel)) {
                res.writeHead(403); res.end("Forbidden"); return;
            }

            const isSafePath = isPathWithinDist(root, resolved);
            if (isSafePath === false) {
                res.writeHead(403); res.end("Forbidden"); return;
            }
            serveStatic(res, resolved);
        } else {
            serveStatic(res, join(distDir, "index.html"));
        }
    };

    handler.setDraft = (type, number, text) => { drafts.set(`${type}-${number}`, text); };
    handler.csrfToken = csrfToken;

    return handler;
}
