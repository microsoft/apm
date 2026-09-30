import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import {
    REPO,
    buildSpawnPrompt,
    ghLabelArgs,
    groupOccupancy,
    occupancyActivity,
    occupancyKey,
    occupancyRecord,
    occupancyRowsFromCopilotSessions,
    mergeLiveOccupancy,
    parseItemNumber,
    applyLabelPlan,
    patchLinkedPullRequests,
    planLabelMutation,
    replaceBoardItem,
    resolveSpawn,
    assertSpawnAllowed,
} from "./logic.mjs";
import { readCopilotAppSessions } from "./copilot-sessions.mjs";
import { renderHtml } from "./ui.mjs";

const DEFAULT_POST_BODY_LIMIT_BYTES = 64 * 1024;
const WRITE_ENDPOINTS = new Set(["/label", "/spawn", "/refresh", "/sync-occupancy"]);
const CANVAS_ID = "autopilot-maintainer";

function isPayloadTooLargeError(error) {
    return error && error.code === "PAYLOAD_TOO_LARGE";
}

function readBody(req) {
    const maxBytes = req.maxBytes || DEFAULT_POST_BODY_LIMIT_BYTES;
    return new Promise((resolve, reject) => {
        const chunks = [];
        let size = 0;
        const cleanup = () => {
            req.off("data", onData);
            req.off("end", onEnd);
            req.off("error", onError);
        };
        const onData = (chunk) => {
            size += chunk.length;
            if (size > maxBytes) {
                const error = new Error("payload too large");
                error.code = "PAYLOAD_TOO_LARGE";
                cleanup();
                reject(error);
                return;
            }
            chunks.push(chunk);
        };
        const onError = (error) => {
            cleanup();
            reject(error);
        };
        const onEnd = () => {
            cleanup();
            resolve(Buffer.concat(chunks).toString("utf8"));
        };
        req.on("data", onData);
        req.on("end", onEnd);
        req.on("error", onError);
    });
}

async function readJsonBody(req) {
    req.maxBytes = DEFAULT_POST_BODY_LIMIT_BYTES;
    try {
        const raw = await readBody(req);
        if (!raw) return {};
        return JSON.parse(raw);
    } catch (error) {
        if (isPayloadTooLargeError(error)) {
            return { isPayloadTooLarge: true };
        }
        return { isBodyReadError: true, error };
    }
}

function sendJson(res, status, payload) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
}

function findItem(items, number) {
    return items.find((item) => item.number === number) || null;
}

export function createHandler(deps) {
    const csrfToken = deps.csrfToken || randomBytes(32).toString("hex");
    const occupancy = deps.occupancy || new Map();
    const repo = deps.repo || REPO;
    const canvasId = deps.canvasId || CANVAS_ID;
    const sseClients = new Set();

    function occupancySnapshot() {
        return [...occupancy.entries()].map(([target, rec]) => ({
            target,
            ...rec,
        }));
    }

    function statePayload() {
        return {
            ok: true,
            repo,
            issues: deps.getIssues(),
            prs: deps.getPrs(),
            occupancy: occupancySnapshot(),
            sessions: groupOccupancy(occupancySnapshot()),
            canvasSessionId: deps.canvasSessionId || deps.session?.sessionId || "",
            lastUpdated: deps.getLastUpdated(),
            lastError: deps.getLastError(),
        };
    }

    function broadcastState() {
        const payload = `data: ${JSON.stringify(statePayload())}\n\n`;
        for (const client of sseClients) {
            try {
                client.write(payload);
            } catch {
                sseClients.delete(client);
            }
        }
    }

    const handler = async function handler(req, res) {
        const urlPath = (req.url || "/").split("?")[0];

        if (req.method === "POST" && WRITE_ENDPOINTS.has(urlPath)) {
            const origin = req.headers.origin || "";
            if (origin && !/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin)) {
                sendJson(res, 403, { ok: false, error: "Forbidden: cross-origin request" });
                return;
            }
            if (req.headers["x-canvas-token"] !== csrfToken) {
                sendJson(res, 403, { ok: false, error: "Forbidden: invalid or missing CSRF token" });
                return;
            }
        }

        if (req.method === "GET" && (urlPath === "/" || urlPath === "/index.html")) {
            res.writeHead(200, {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store",
            });
            res.end(renderHtml({ csrfToken, repo }));
            return;
        }

        if (req.method === "GET" && urlPath === "/api/state") {
            sendJson(res, 200, statePayload());
            return;
        }

        if (req.method === "GET" && urlPath === "/events") {
            res.writeHead(200, {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                Connection: "keep-alive",
            });
            res.write(`data: ${JSON.stringify(statePayload())}\n\n`);
            sseClients.add(res);
            req.on("close", () => {
                sseClients.delete(res);
            });
            return;
        }

        if (req.method === "POST" && urlPath === "/refresh") {
            try {
                const result = await deps.refreshData();
                broadcastState();
                sendJson(res, result?.ok === false ? 502 : 200, {
                    ok: result?.ok !== false,
                    error: result?.error || null,
                });
            } catch (error) {
                sendJson(res, 500, { ok: false, error: String(error.message || error) });
            }
            return;
        }

        if (req.method === "POST" && urlPath === "/sync-occupancy") {
            try {
                const result = await (deps.syncOccupancy
                    ? deps.syncOccupancy()
                    : syncOccupancyFromCopilot(occupancy, deps));
                deps.saveOccupancy?.();
                broadcastState();
                sendJson(res, result?.ok === false ? 502 : 200, {
                    ok: result?.ok !== false,
                    count: result?.count ?? occupancy.size,
                    error: result?.error || null,
                });
            } catch (error) {
                sendJson(res, 500, { ok: false, error: String(error.message || error) });
            }
            return;
        }

        if (req.method === "POST" && urlPath === "/label") {
            const body = await readJsonBody(req);
            if (body.isPayloadTooLarge) {
                sendJson(res, 413, { ok: false, error: "payload too large" });
                return;
            }
            if (body.isBodyReadError) {
                sendJson(res, 400, { ok: false, error: "invalid body" });
                return;
            }
            try {
                const number = parseItemNumber(body.number);
                const kind = body.kind;
                const items = kind === "pr" ? deps.getPrs() : deps.getIssues();
                const item = findItem(items, number);
                const plan = planLabelMutation({
                    kind,
                    number,
                    action: body.action,
                    currentLabels: item?.labels || body.currentLabels || [],
                    confirmClearAccepted: body.confirmClearAccepted === true,
                    repo,
                });
                if (plan.add.length === 0 && plan.remove.length === 0) {
                    sendJson(res, 200, { ok: true, unchanged: true });
                    return;
                }
                const args = ghLabelArgs(plan);
                await deps.ghExec(args);
                const patched = applyLabelPlan(item, plan);
                if (patched && kind === "pr") {
                    replaceBoardItem(deps.getPrs(), patched);
                    patchLinkedPullRequests(deps.getIssues(), patched);
                } else if (patched) {
                    replaceBoardItem(deps.getIssues(), patched);
                }
                broadcastState();
                sendJson(res, 200, {
                    ok: true,
                    add: plan.add,
                    remove: plan.remove,
                    item: patched,
                });
            } catch (error) {
                sendJson(res, 400, { ok: false, error: String(error.message || error) });
            }
            return;
        }

        if (req.method === "POST" && urlPath === "/spawn") {
            const body = await readJsonBody(req);
            if (body.isPayloadTooLarge) {
                sendJson(res, 413, { ok: false, error: "payload too large" });
                return;
            }
            if (body.isBodyReadError) {
                sendJson(res, 400, { ok: false, error: "invalid body" });
                return;
            }
            try {
                const resolved = resolveSpawn(body.action, body.number);
                assertSpawnAllowed(resolved, occupancy, { confirm: body.confirm === true });
                occupancy.set(occupancyKey(resolved.target), {
                    busy: true,
                    activity: "pending",
                    name: resolved.sessionName,
                    skill: resolved.skill,
                    pending: true,
                });
                deps.saveOccupancy?.();
                const prompt = buildSpawnPrompt(resolved, canvasId);
                deps.session.send({ prompt });
                broadcastState();
                sendJson(res, 200, {
                    ok: true,
                    target: resolved.target,
                    name: resolved.sessionName,
                    skill: resolved.skill,
                    pending: true,
                });
            } catch (error) {
                sendJson(res, 409, { ok: false, error: String(error.message || error) });
            }
            return;
        }

        sendJson(res, 404, { ok: false, error: "not found" });
    };

    handler.csrfToken = csrfToken;
    handler.broadcastState = broadcastState;
    return handler;
}

export function syncOccupancyFromCopilot(occupancy, deps = {}) {
    const reader = deps.readCopilotAppSessions || readCopilotAppSessions;
    let liveSessions;
    try {
        liveSessions = reader(deps.copilotDbPath);
    } catch (error) {
        return {
            ok: false,
            error: String((error && error.message) || error),
            count: occupancy.size,
        };
    }
    const live = occupancyRowsFromCopilotSessions(liveSessions);
    mergeLiveOccupancy(occupancy, live);
    return { ok: true, count: occupancy.size };
}

export function applyOccupancyReport(occupancy, input) {
    const target = occupancyKey(input.target);
    if (!target) {
        throw new Error("occupancy target required");
    }
    if (input.busy === false) {
        occupancy.delete(target);
        return { target, busy: false };
    }
    const activity = occupancyActivity({
        activity: input.activity,
        busy: true,
        pending: false,
    });
    occupancy.set(target, occupancyRecord({
        busy: true,
        activity,
        name: String(input.name || ""),
        skill: String(input.skill || ""),
        sessionId: input.session_id || input.sessionId || null,
        creatorSessionId: input.creator_session_id || input.creatorSessionId || null,
        pending: false,
    }));
    return occupancy.get(target);
}

export function serializeOccupancy(occupancy) {
    return [...occupancy.entries()].map(([target, rec]) => ({
        target,
        ...occupancyRecord(rec),
    }));
}

export function hydrateOccupancy(occupancy, rows) {
    occupancy.clear();
    for (const row of rows || []) {
        const target = occupancyKey(row?.target);
        if (!target) continue;
        occupancy.set(target, occupancyRecord(row));
    }
    return occupancy;
}

export function loadOccupancyFile(occupancy, filePath) {
    try {
        const rows = JSON.parse(readFileSync(filePath, "utf8"));
        hydrateOccupancy(occupancy, rows);
        return true;
    } catch {
        return false;
    }
}

export function saveOccupancyFile(occupancy, filePath) {
    mkdirSync(dirname(filePath), { recursive: true });
    writeFileSync(filePath, JSON.stringify(serializeOccupancy(occupancy)), "utf8");
}
