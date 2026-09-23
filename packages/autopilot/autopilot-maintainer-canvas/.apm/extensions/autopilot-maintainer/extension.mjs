import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { joinSession, createCanvas, CanvasError } from "@github/copilot-sdk/extension";
import { REPO, applyTriageAdvice, attachLinkedPrs, classifyIssue, classifyPr } from "./logic.mjs";
import {
    applyOccupancyReport,
    createHandler,
    loadOccupancyFile,
    saveOccupancyFile,
    syncOccupancyFromCopilot,
} from "./server-handler.mjs";

const servers = new Map();
const occupancyFile = join(dirname(fileURLToPath(import.meta.url)), "occupancy.json");
const occupancy = new Map();
loadOccupancyFile(occupancy, occupancyFile);

function saveOccupancy() {
    try {
        saveOccupancyFile(occupancy, occupancyFile);
    } catch {
        // Keep in-memory occupancy if the session file is unwritable.
    }
}

function syncOccupancy() {
    try {
        const result = syncOccupancyFromCopilot(occupancy);
        saveOccupancy();
        return result;
    } catch (error) {
        return { ok: false, error: String(error.message || error), count: occupancy.size };
    }
}
let issues = [];
let prs = [];
let lastUpdated = null;
let lastError = null;
let refreshInFlight = null;
let openInstanceCount = 0;

function ghExec(args) {
    return new Promise((resolve, reject) => {
        execFile("gh", args, { maxBuffer: 1024 * 1024, timeout: 20_000 }, (err, stdout, stderr) => {
            if (err) return reject(new Error(stderr || err.message));
            resolve(stdout);
        });
    });
}

async function attachTriageAdvice(items, kind) {
    const targets = items.filter((item) => item.advisory || item.triageRan);
    const chunkSize = 12;
    for (let offset = 0; offset < targets.length; offset += chunkSize) {
        const chunk = targets.slice(offset, offset + chunkSize);
        const fields = chunk.map((item) => {
            const selector = kind === "pr"
                ? `pullRequest(number: ${item.number})`
                : `issue(number: ${item.number})`;
            return `n${item.number}: ${selector} { comments(last: 40) { nodes { body } } }`;
        }).join("\n");
        const query = `query { repository(owner: "microsoft", name: "apm") {\n${fields}\n} }`;
        try {
            const out = await ghExec(["api", "graphql", "-f", `query=${query}`]);
            const payload = JSON.parse(out);
            const repoNode = payload.data && payload.data.repository ? payload.data.repository : {};
            for (const item of chunk) {
                const comments = (((repoNode[`n${item.number}`] || {}).comments || {}).nodes) || [];
                Object.assign(item, applyTriageAdvice(item, comments));
            }
        } catch {
            // Keep label-only triageRan when comment fetch fails.
        }
    }
}

async function refreshData() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async () => {
        try {
            const issueOut = await ghExec([
                "issue", "list",
                "--repo", REPO,
                "--state", "open",
                "--limit", "100",
                "--json", "number,title,labels,author,url",
            ]);
            let prOut;
            try {
                prOut = await ghExec([
                    "pr", "list",
                    "--repo", REPO,
                    "--state", "open",
                    "--limit", "100",
                    "--json", "number,title,labels,author,url,isDraft,closingIssuesReferences,body",
                ]);
            } catch {
                prOut = await ghExec([
                    "pr", "list",
                    "--repo", REPO,
                    "--state", "open",
                    "--limit", "100",
                    "--json", "number,title,labels,author,url,isDraft,body",
                ]);
            }
            issues = JSON.parse(issueOut).map(classifyIssue);
            prs = JSON.parse(prOut).map(classifyPr);
            await attachTriageAdvice(issues, "issue");
            await attachTriageAdvice(prs, "pr");
            attachLinkedPrs(issues, prs);
            lastUpdated = new Date().toISOString();
            lastError = null;
            return { ok: true, error: null };
        } catch (error) {
            lastError = String(error.message || error);
            return { ok: false, error: lastError };
        }
    })().finally(() => {
        refreshInFlight = null;
        for (const entry of servers.values()) {
            entry.handler?.broadcastState?.();
        }
    });
    return refreshInFlight;
}

const session = await joinSession({
    canvases: [
        createCanvas({
            id: "autopilot-maintainer",
            displayName: "Autopilot maintainer",
            description: "CODEOWNER control surface for microsoft/apm autopilot: live GitHub labels, accept/defer/panel-review, and isolated scheduler/worker spawns.",
            actions: [
                {
                    name: "report_occupancy",
                    description: "Record or clear an autopilot scheduler/worker session so the canvas can list working/idle status and refuse duplicate working spawns.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            target: { type: "string" },
                            busy: { type: "boolean" },
                            activity: { type: "string" },
                            name: { type: "string" },
                            skill: { type: "string" },
                            session_id: { type: "string" },
                        },
                        required: ["target"],
                    },
                    handler: async (ctx) => {
                        try {
                            const result = applyOccupancyReport(occupancy, ctx.input || {});
                            saveOccupancy();
                            for (const entry of servers.values()) {
                                entry.handler?.broadcastState?.();
                            }
                            return result;
                        } catch (error) {
                            throw new CanvasError("occupancy_invalid", String(error.message || error));
                        }
                    },
                },
            ],
            open: async (ctx) => {
                openInstanceCount += 1;
                let entry = servers.get(ctx.instanceId);
                if (!entry) {
                    const handler = createHandler({
                        ghExec,
                        session,
                        occupancy,
                        saveOccupancy,
                        syncOccupancy,
                        repo: REPO,
                        canvasId: "autopilot-maintainer",
                        canvasSessionId: session.sessionId,
                        getIssues: () => issues,
                        getPrs: () => prs,
                        getLastUpdated: () => lastUpdated,
                        getLastError: () => lastError,
                        refreshData,
                    });
                    const server = createServer(handler);
                    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
                    const address = server.address();
                    const port = typeof address === "object" && address ? address.port : 0;
                    entry = { server, handler, url: `http://127.0.0.1:${port}/` };
                    servers.set(ctx.instanceId, entry);
                }
                if (issues.length === 0 && !lastError) {
                    refreshData().catch(() => {});
                }
                syncOccupancy();
                return {
                    title: "Autopilot maintainer",
                    url: entry.url,
                };
            },
            onClose: async (ctx) => {
                openInstanceCount = Math.max(0, openInstanceCount - 1);
                const entry = servers.get(ctx.instanceId);
                if (entry) {
                    servers.delete(ctx.instanceId);
                    await new Promise((resolve) => entry.server.close(() => resolve()));
                }
            },
        }),
    ],
});
