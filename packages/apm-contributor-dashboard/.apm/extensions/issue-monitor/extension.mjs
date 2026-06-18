// Extension: issue-monitor
// Live dashboard monitoring APM issue triage inbox and session status
// Fetches issues in real-time from GitHub via `gh` CLI

import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { joinSession, createCanvas } from "@github/copilot-sdk/extension";
import { classifyIssue, classifyPrStatus, escapeHtml, matchPrsToIssues, classifyPrForTable, classifyPipeline, classifyPanel, parsePanelCounts, parsePanelReview } from "./logic.mjs";
import { createHandler } from "./server-handler.mjs";

const REPO = "microsoft/apm";
const POLL_INTERVAL_MS = 30_000;
const MAX_ISSUES = 100;
const MAX_CONCURRENT_GH = 8;

const servers = new Map();

let issueData = [];
let prData = [];
let lastUpdated = null;
let lastError = null;
let pollTimer = null;
let openInstanceCount = 0;
let lastPrFingerprint = null;

// Persist started sessions to disk so they survive extension reloads
const __extensionDir = dirname(fileURLToPath(import.meta.url));
const SESSION_FILE = join(__extensionDir, ".sessions.json");

function loadSessions() {
    try {
        const data = JSON.parse(readFileSync(SESSION_FILE, "utf-8"));
        return new Set(Array.isArray(data) ? data : []);
    } catch { return new Set(); }
}

function saveSessions() {
    try {
        mkdirSync(dirname(SESSION_FILE), { recursive: true });
        writeFileSync(SESSION_FILE, JSON.stringify([...startedSessions]));
    } catch {}
}

const startedSessions = loadSessions();

// -- Concurrency semaphore for gh CLI calls --

function createSemaphore(max) {
    let active = 0;
    const queue = [];
    return function acquire() {
        return new Promise((resolve) => {
            const run = () => { active++; resolve(() => { active--; if (queue.length > 0) queue.shift()(); }); };
            if (active < max) run();
            else queue.push(run);
        });
    };
}

const acquireSlot = createSemaphore(MAX_CONCURRENT_GH);

// -- GitHub fetching via gh CLI --

function ghExec(args) {
    return new Promise((resolve, reject) => {
        execFile("gh", args, { maxBuffer: 1024 * 1024, timeout: 15_000 }, (err, stdout, stderr) => {
            if (err) return reject(new Error(stderr || err.message));
            resolve(stdout);
        });
    });
}

async function fetchIssues() {
    try {
        const out = await ghExec([
            "issue", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", String(MAX_ISSUES),
            "--json", "number,title,labels,author,url",
        ]);
        const raw = JSON.parse(out);
        // Preserve session/status overrides from agent actions
        const overrides = new Map(issueData.map((i) => [i.number, { status: i.status, session: i.session }]));
        issueData = raw.map((r) => {
            const issue = classifyIssue(r);
            const prev = overrides.get(issue.number);
            if (prev && prev.status !== "available") {
                issue.status = prev.status;
                if (prev.session) issue.session = prev.session;
            }
            return issue;
        });
        // Fetch open PRs and match to issues by #number references
        await fetchAndMatchPRs();
        lastUpdated = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        lastError = null;
    } catch (e) {
        lastError = String(e.message || e);
    }
}

async function fetchAndMatchPRs() {
    try {
        const prOut = await ghExec([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", "100",
            "--json", "number,title,url,body,state,isDraft,reviewDecision,statusCheckRollup,author,labels,headRefName",
        ]);
        const prs = JSON.parse(prOut);

        // Change-detection: skip expensive per-PR fetches when PR list is stable
        const fingerprint = prs.map(p => `${p.number}:${p.headRefName}`).sort().join("|");
        const listChanged = fingerprint !== lastPrFingerprint;
        lastPrFingerprint = fingerprint;

        // Fetch workflow runs for each PR branch with semaphore-limited concurrency
        const branchRunPromises = prs.map(async (pr) => {
            if (!pr.headRefName) return;
            // Skip per-PR fetches if the list has not changed (use cached data)
            if (!listChanged) {
                const cached = prData.find(p => p.number === pr.number);
                if (cached) { pr.workflowRuns = cached._rawWorkflowRuns || []; return; }
            }
            const release = await acquireSlot();
            try {
                const runsOut = await ghExec([
                    "run", "list",
                    "--repo", REPO,
                    "--branch", pr.headRefName,
                    "--limit", "10",
                    "--json", "databaseId,name,status,conclusion,event",
                ]);
                pr.workflowRuns = JSON.parse(runsOut);
            } catch (_) {
                pr.workflowRuns = [];
            } finally {
                release();
            }
        });
        await Promise.all(branchRunPromises);
        // Fetch panel review comments for all PRs with semaphore
        const panelPromises = prs.map(async (pr) => {
            if (!listChanged) {
                const cached = prData.find(p => p.number === pr.number);
                if (cached && cached.panelCounts) { pr.panelCounts = cached.panelCounts; return; }
            }
            const release = await acquireSlot();
            try {
                const cOut = await ghExec([
                    "pr", "view", String(pr.number),
                    "--repo", REPO,
                    "--json", "comments",
                ]);
                const parsed = JSON.parse(cOut);
                pr.panelCounts = parsePanelCounts(parsed.comments || []);
            } catch (_) {
                pr.panelCounts = null;
            } finally {
                release();
            }
        });
        await Promise.all(panelPromises);
        matchPrsToIssues(issueData, prs);
        // Store raw workflow runs for change-detection cache
        for (const pr of prs) { pr._rawWorkflowRuns = pr.workflowRuns || []; }
        prData = prs.map(pr => classifyPrForTable(pr));
    } catch (_) {
        // PR fetch is best-effort; ignore failures
    }
}

function startPolling() {
    if (pollTimer) return;
    // Skip immediate fetch if data already loaded (initial fetch done in open handler)
    if (issueData.length === 0) fetchIssues();
    pollTimer = setInterval(fetchIssues, POLL_INTERVAL_MS);
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// -- HTTP server --

const DIST_DIR = join(__extensionDir, "dist");

async function startServer(instanceId) {
    const handler = createHandler({
        ghExec,
        session,
        startedSessions,
        saveSessions,
        getIssueData: () => issueData,
        getPrData: () => prData,
        getLastUpdated: () => lastUpdated,
        getLastError: () => lastError,
        repo: REPO,
        distDir: DIST_DIR,
    });
    const server = createServer(handler);
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    return { server, handler, url: `http://127.0.0.1:${port}/` };
}

// -- Session + Canvas --

const session = await joinSession({
    canvases: [
        createCanvas({
            id: "issue-monitor",
            displayName: "APM Contributor Dashboard",
            description: "Live dashboard tracking APM issue triage status, session progress, and PR state for active bug fixes. Auto-fetches issues from GitHub every 30 seconds.",
            actions: [
                {
                    name: "update_issues",
                    description: "Push the current issue tracking data to the dashboard. Each issue needs: number, title, type, priority, author, status, url, and optionally session.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            issues: {
                                type: "array",
                                items: {
                                    type: "object",
                                    properties: {
                                        number: { type: "number" },
                                        title: { type: "string" },
                                        type: { type: "string", enum: ["bug", "feature", "chore"] },
                                        priority: { type: "string", enum: ["P0", "P1", "P2", "P3"] },
                                        author: { type: "string" },
                                        status: { type: "string", enum: ["available", "planning", "implementing", "in-review", "merged"] },
                                        url: { type: "string" },
                                        session: { type: "string" },
                                    },
                                    required: ["number", "title", "type", "priority", "author", "status", "url"],
                                },
                            },
                        },
                        required: ["issues"],
                    },
                    handler: async (ctx) => {
                        issueData = ctx.input.issues;
                        lastUpdated = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
                        return { updated: issueData.length, timestamp: lastUpdated };
                    },
                },
                {
                    name: "update_status",
                    description: "Update the status of a single tracked issue by number.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            number: { type: "number" },
                            status: { type: "string", enum: ["available", "planning", "implementing", "in-review", "merged"] },
                            session: { type: "string" },
                        },
                        required: ["number", "status"],
                    },
                    handler: async (ctx) => {
                        const issue = issueData.find((i) => i.number === ctx.input.number);
                        if (!issue) return { error: "Issue not found" };
                        issue.status = ctx.input.status;
                        if (ctx.input.session) issue.session = ctx.input.session;
                        lastUpdated = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
                        return { updated: issue.number, status: issue.status, timestamp: lastUpdated };
                    },
                },
                {
                    name: "mark_sessions",
                    description: "Mark issue numbers that have active sessions. Buttons will show 'Open Session' instead of 'Start Session' for these issues.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            issue_numbers: {
                                type: "array",
                                items: { type: "number" },
                                description: "List of issue numbers that have linked sessions",
                            },
                        },
                        required: ["issue_numbers"],
                    },
                    handler: async (ctx) => {
                        const nums = ctx.input.issue_numbers || [];
                        nums.forEach(n => startedSessions.add(n));
                        saveSessions();
                        return { marked: nums, total: startedSessions.size };
                    },
                },
                {
                    name: "update-draft",
                    description: "Push refined comment text back to the dashboard's comment composer for a specific issue or PR. Use after the user clicks 'Refine with Copilot' and you have prepared an improved draft.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            type: { type: "string", enum: ["issue", "pr"], description: "Whether this is an issue or PR comment" },
                            number: { type: "number", description: "The issue or PR number" },
                            text: { type: "string", description: "The refined comment text to push to the composer" },
                        },
                        required: ["type", "number", "text"],
                    },
                    handler: async (ctx) => {
                        const { type, number, text } = ctx.input;
                        // Push to all server instances
                        for (const entry of servers.values()) {
                            entry.handler?.setDraft?.(type, number, text);
                        }
                        return { ok: true, type, number, length: text.length };
                    },
                },
            ],
            open: async (ctx) => {
                // Start server immediately; fetch data in background so
                // the canvas opens fast and shows a loading state.
                openInstanceCount++;
                startPolling();
                let entry = servers.get(ctx.instanceId);
                if (!entry) {
                    entry = await startServer(ctx.instanceId);
                    servers.set(ctx.instanceId, entry);
                }
                return { title: "APM Contributor Dashboard", url: entry.url };
            },
            onClose: async (ctx) => {
                openInstanceCount = Math.max(0, openInstanceCount - 1);
                if (openInstanceCount === 0) stopPolling();
                const entry = servers.get(ctx.instanceId);
                if (entry) {
                    servers.delete(ctx.instanceId);
                    await new Promise((resolve) => entry.server.close(() => resolve()));
                }
            },
        }),
    ],
});
