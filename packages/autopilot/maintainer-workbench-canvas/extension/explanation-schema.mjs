const id = { type: "string", pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$" };
const revision = { type: "integer", minimum: 1 };
const string = maxLength => ({ type: "string", minLength: 1, maxLength });
const object = (properties, required = Object.keys(properties)) => ({ type: "object", properties, required, additionalProperties: false });
export const GET_EXPLANATION = object({ id });
export const CLAIM_EXPLANATION = object({ id, revision });
export const REPORT_EXPLANATION = object({
  id, revision, claimToken: id, contentKey: { type: "string", pattern: "^[0-9a-f]{64}$" },
  status: { enum: ["completed", "failed"] }, error: string(500),
  result: object({
    title: string(100), problem: string(480), example: string(480), exampleKind: { enum: ["reported", "illustrative"] },
    impact: string(360), tradeoff: string(360),
    decisionQuestion: object({ text: string(320), state: { enum: ["resolved", "unresolved", "unclear"] } }),
    citations: { type: "array", items: string(1000), minItems: 1, maxItems: 6, uniqueItems: true },
  }, ["title", "problem", "example", "exampleKind", "impact", "citations"]),
}, ["id", "revision", "claimToken", "contentKey", "status"]);
