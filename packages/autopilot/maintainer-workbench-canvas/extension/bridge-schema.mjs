const str = { type: "string", minLength: 1, maxLength: 2000 };
const id = { type: "string", pattern: "^[0-9a-f-]{36}$" };
const revision = { type: "integer", minimum: 1 };
const object = (properties, required) => ({ type: "object", properties, required, additionalProperties: false });
export const GET_REQUEST = object({ id }, ["id"]);
export const CLAIM_REQUEST = object({ id, revision }, ["id", "revision"]);
export const REPORT_REQUEST = object({
  id, revision, claimToken: str, status: { enum: ["awaiting-confirmation", "queued", "running", "plan-required", "partial", "completed", "failed", "blocked", "cancelled"] }, message: str,
  gate: object({ tool: str, reference: str, actor: str, observedAt: str, confirmed: { const: true }, planApproved: { type: "boolean" } }, ["tool", "reference", "actor", "observedAt", "confirmed"]),
  receipts: { type: "array", maxItems: 30, items: object({ step: str, tool: str, reference: str, observedAt: str, outcome: { enum: ["verified", "failed", "uncertain", "noop"] }, url: str,
    resolves: { type: "array", minItems: 1, maxItems: 30, items: str } }, ["step", "tool", "reference", "observedAt", "outcome"]) },
  draft: object({ scope: str, doneWhen: str, outOfScope: str, reviewContact: str, area: str, horizon: str }, ["scope", "doneWhen", "outOfScope", "reviewContact"]),
}, ["id", "revision", "claimToken", "status", "message"]);
export const REPORT_RUNS = object({
  observedAt: str, tool: str, reference: str, complete: { type: "boolean" },
  runs: { type: "array", maxItems: 100, items: object({
    id: str, sessionId: str, targetIds: { type: "array", minItems: 1, maxItems: 10, items: str },
    name: str, kind: { enum: ["triage", "delivery", "review", "recovery"] },
    state: { enum: ["queued", "running", "waiting-human", "plan-required", "blocked", "completed", "cancelled"] },
    actor: str, mandate: str, blocker: str,
  }, ["id", "sessionId", "targetIds", "name", "kind", "state", "actor", "mandate"]) },
}, ["observedAt", "tool", "reference", "complete", "runs"]);
