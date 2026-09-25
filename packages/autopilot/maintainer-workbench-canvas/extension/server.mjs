import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { renderHtml } from "./ui.mjs";

const assets = new Map([
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/style.css", ["style.css", "text/css; charset=utf-8"]],
  ["/scope.mjs", ["scope.mjs", "text/javascript; charset=utf-8"]],
  ["/actions.mjs", ["actions.mjs", "text/javascript; charset=utf-8"]],
  ["/decision-view.mjs", ["decision-view.mjs", "text/javascript; charset=utf-8"]],
  ["/async-view.mjs", ["async-view.mjs", "text/javascript; charset=utf-8"]],
]);

export async function startServer(store, { instanceId = "standalone" } = {}) {
  const token = randomBytes(32).toString("hex");
  const server = createServer(async (req, res) => {
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'");
    const send = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(body));
    };
    try {
      const origin = `http://127.0.0.1:${server.address().port}`;
      if (req.headers.host !== new URL(origin).host || (req.headers.origin && req.headers.origin !== origin)) return send(403, { error: "Only same-origin loopback requests are accepted." });
      const path = new URL(req.url, origin).pathname;
      if (req.method === "GET" && path === "/") {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        return res.end(renderHtml(token));
      }
      if (req.method === "GET" && assets.has(path)) {
        const [file, type] = assets.get(path);
        const content = await readFile(new URL(file, import.meta.url));
        res.writeHead(200, { "Content-Type": type });
        return res.end(content);
      }
      if (req.method === "GET" && path === "/api/state") {
        store.ensureFresh();
        store.ensureSelectedChecks();
        return send(200, store.state());
      }
      if (req.method !== "POST" || !["/api/refresh", "/api/select", "/api/view", "/api/checks", "/api/explanations/request", "/api/explanations/cancel", "/api/requests/preview", "/api/requests/confirm", "/api/requests/cancel",
        "/api/workflow-approvals/preview", "/api/workflow-approvals/confirm", "/api/workflow-approvals/observe"].includes(path)) return send(404, { error: "No such route." });
      const supplied = Buffer.from(String(req.headers["x-canvas-token"] || ""));
      if (supplied.length !== token.length || !timingSafeEqual(supplied, Buffer.from(token))) return send(403, { error: "Invalid local request token." });
      if (req.headers["content-type"] !== "application/json") return send(415, { error: "Expected application/json." });
      let text = "";
      for await (const part of req) {
        text += part;
        if (Buffer.byteLength(text) > 16384) return send(413, { error: "Request is too large." });
      }
      let input;
      try { input = JSON.parse(text); } catch { return send(400, { error: "Invalid JSON." }); }
      if (!input || typeof input !== "object" || Array.isArray(input)) return send(400, { error: "Expected an input object." });
      if (path.startsWith("/api/workflow-approvals/")) {
        if (!store.workflowApprovals) return send(503, { error: "Workflow approval is unavailable." });
        const method = path.split("/").at(-1);
        const fields = { preview: ["targetId"], confirm: ["id", "revision", "confirmed"], observe: ["id"] }[method];
        if (Object.keys(input).length !== fields.length || fields.some(key => !Object.hasOwn(input, key))) return send(400, { error: "Invalid workflow approval input." });
        const selectedId = store.view.selectedId;
        const viewRevision = store.viewRevision;
        const record = await store.workflowApprovals[method](input);
        if (method === "confirm") await store.followApproval(record, selectedId, viewRevision);
        return send(method === "confirm" ? 202 : 200, record);
      }
      if (path.startsWith("/api/explanations/")) {
        if (!store.explanations) return send(503, { error: "Explanation connection unavailable." });
        return send(200, await store.explanations[path.endsWith("/cancel") ? "cancel" : "request"](input, instanceId));
      }
      if (path.startsWith("/api/requests/")) {
        if (!store.bridge) return send(503, { error: "Parent action transport is not attached." });
        const operation = path.split("/").at(-1);
        const result = await store.bridge[operation](input, instanceId);
        return send(200, result);
      }
      if (path === "/api/refresh") {
        if (Object.keys(input).length) return send(400, { error: "Refresh takes no arguments." });
        // Long reads run in this provider; the client observes progress through state.
        store.refresh().catch(() => { store.error = "Refresh failed. Previous evidence remains available."; });
        return send(202, store.state());
      }
      if (path === "/api/select") {
        if (Object.keys(input).some(key => !["id", "intent"].includes(key)) || typeof input.id !== "string") return send(400, { error: "Select requires a stable work-item ID." });
        if (!store.acceptViewIntent(instanceId, input.intent)) return send(202, store.state());
        return send(202, await store.select(input.id));
      }
      if (path === "/api/checks") {
        if (Object.keys(input).length !== 1 || typeof input.id !== "string" || input.id !== store.state().view.selectedId) return send(409, { error: "Stale selected checks request." });
        store.ensureSelectedChecks({ force: true });
        return send(202, store.state());
      }
      const { intent, ...view } = input;
      if (Object.hasOwn(view, "followingApprovalId")) return send(400, { error: "View input cannot set workflow follow-up." });
      if (!store.acceptViewIntent(instanceId, intent)) return send(200, store.state());
      return send(200, await store.setView(view));
    } catch (error) {
      const conflict = /Stale|Conflict|Existing|already|Cancellation|reconcil/.test(error.message);
      const inputError = /Invalid|Unsupported|input|Filter|not in the loaded|outside the active|View|requires|required|needs|Closed|No fresh|Scope|Implementation/.test(error.message);
      send(conflict ? 409 : inputError ? 400 : 500, { error: error.message || "Local operation failed. Inspect the request journal before any retry." });
    }
  });
  server.requestTimeout = 120000;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    server, url: `http://127.0.0.1:${server.address().port}/`,
    close: () => new Promise((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeIdleConnections();
    }),
  };
}
