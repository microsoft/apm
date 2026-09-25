import { joinSession, createCanvas, CanvasError } from "@github/copilot-sdk/extension";
import { Store } from "./store.mjs";
import { startServer } from "./server.mjs";
import { Bridge } from "./bridge.mjs";
import { executionContract } from "./actions.mjs";
import { GET_REQUEST, CLAIM_REQUEST, REPORT_REQUEST, REPORT_RUNS } from "./bridge-schema.mjs";
import { Explanations } from "./explanations.mjs";
import { GET_EXPLANATION, CLAIM_EXPLANATION, REPORT_EXPLANATION } from "./explanation-schema.mjs";
import { WorkflowApprovals } from "./workflow-approvals.mjs";
import { explanationDispatchContext, explanationTools } from "./explanation-dispatch.mjs";

const store = new Store();
let session;
const bridge = new Bridge(store, { send: async options => {
  if (!session) throw new Error("Parent connection is not ready.");
  return await session.send(options);
} });
store.bridge = bridge;
await bridge.load();
store.workflowApprovals = new WorkflowApprovals(store);
await store.workflowApprovals.load();
await store.load();
const explanations = new Explanations(store, { send: async options => {
  if (!session) throw new Error("Parent connection is not ready.");
  return await session.send(options);
} });
await explanations.load();
store.explanations = explanations;
const servers = new Map();
const empty = { type: "object", properties: {}, additionalProperties: false };
const idSchema = { type: "string", pattern: "^microsoft/apm/(issue|pr)/[1-9][0-9]{0,8}$" };
const wrap = fn => async ctx => {
  try { return await fn(ctx); }
  catch (error) { throw new CanvasError("maintainer_read_failed", error.message); }
};

session = await joinSession({
  tools: explanationTools(explanations),
  hooks: { onUserPromptSubmitted: input => explanationDispatchContext(explanations, input.prompt) },
  canvases: [createCanvas({
    id: "maintainer-control-plane",
    displayName: "APM maintainer workbench",
    description: "APM maintainer decisions, triage, planning and delivery with explicit workflow permission previews and confirmed parent-tool requests.",
    inputSchema: {
      type: "object", properties: {
        repo: { const: "microsoft/apm" },
        selectedId: idSchema,
      }, additionalProperties: false,
    },
    actions: [
      { name: "get_explanations", description: "Read bounded explanation request IDs, targets, revisions and states for recovery. No private claim tokens or operational authority.", inputSchema: empty,
        handler: () => ({ error: explanations.error, requests: explanations.requests.map(r => ({ id: r.id, targetId: r.targetId, revision: r.revision, status: r.status, contentKey: r.contentKey })) }) },
      { name: "get_explanation", description: "Read an advisory explanation request, bounded untrusted sources and source-precedence contract. IDs appear in get_state.explanation.", inputSchema: GET_EXPLANATION, handler: wrap(ctx => explanations.inspect(ctx.input.id)) },
      { name: "claim_explanation", description: "Claim read-only inference with revision CAS; read the fresh returned packet before inference. Private report token grants no operational authority.", inputSchema: CLAIM_EXPLANATION, handler: wrap(ctx => explanations.claim(ctx.input)) },
      { name: "report_explanation", description: "Save grounded advisory prose with allowlisted citations. Cannot set lifecycle, buttons, authority or execution.", inputSchema: REPORT_EXPLANATION, handler: wrap(ctx => explanations.report(ctx.input)) },
      { name: "get_request", description: "Read exact requested effects and parent execution contract; not authority.", inputSchema: GET_REQUEST,
        handler: wrap(ctx => { const r = bridge.get(ctx.input.id); return { ...bridge.public(r), executionContract: executionContract(r) }; }) },
      { name: "claim_request", description: "Claim a pending request by revision; returns parent-only report token. No execution permission.", inputSchema: CLAIM_REQUEST, handler: wrap(ctx => bridge.claim(ctx.input)) },
      { name: "report_request", description: "Report actual host gates, tool/readback receipts and partial outcomes. Never called by iframe.", inputSchema: REPORT_REQUEST, handler: wrap(ctx => bridge.report(ctx.input)) },
      { name: "report_runs", description: "Report observed mapped host sessions, not inferred names or zero agents.", inputSchema: REPORT_RUNS, handler: wrap(ctx => bridge.reportRuns(ctx.input)) },
      { name: "get_state", description: "Read current view and start a bounded live GitHub refresh when due.", inputSchema: empty, handler: () => { store.ensureFresh(); return store.state(); } },
      { name: "refresh", description: "Refresh read-only GitHub evidence, bounded to one request per 30 seconds.", inputSchema: empty, handler: wrap(() => store.refresh()) },
      { name: "select", description: "Select a loaded stable work item and read its evidence; no execution.", inputSchema: {
        type: "object", properties: { id: idSchema }, required: ["id"], additionalProperties: false,
      }, handler: wrap(ctx => store.select(ctx.input.id)) },
    ],
    open: wrap(async ctx => {
      if (ctx.input?.selectedId) await store.setView({ selectedId: ctx.input.selectedId });
      let entry = servers.get(ctx.instanceId);
      if (!entry) {
        entry = await startServer(store, { instanceId: ctx.instanceId });
        servers.set(ctx.instanceId, entry);
      }
      store.ensureFresh();
      return { title: "APM maintainer workbench", url: entry.url, status: "Ready for confirmed requests" };
    }),
    onClose: async ctx => {
      const entry = servers.get(ctx.instanceId);
      if (entry) { servers.delete(ctx.instanceId); await entry.close(); }
    },
  })],
});
