import { fileURLToPath } from "node:url";
import { isAbsolute } from "node:path";

export const REPO = "microsoft/apm";
export const GH = `https://github.com/${REPO}`;
export const STORE = process.env.APM_MAINTAINER_DATA_DIR || fileURLToPath(new URL("./.local/", import.meta.url));
if (!isAbsolute(STORE)) throw new Error("APM_MAINTAINER_DATA_DIR must be an absolute path.");
export const TTL = 5 * 60 * 1000;
export const AUTO_REFRESH = 4 * 60 * 1000;
export const HISTORY_TTL = 30 * 60 * 1000;
export const MIN_REFRESH = 30 * 1000;
export const CONCURRENCY = 4;
export const MAX_PAGES = 100;
export { HORIZONS } from "./scope.mjs";
export const QUEUES = [
  ["needs-human", "Needs your decision"],
  ["active", "Agents working"],
  ["authorized", "Authorized and queued"],
  ["waiting", "Waiting on someone else"],
  ["review", "Ready for human review"],
  ["preparing", "Preparing / unknown"],
  ["completed", "Verified completed"],
  ["deferred", "Deferred / closed"],
];

export function itemId(kind, number) {
  return `${REPO}/${kind}/${number}`;
}

export function parseId(value) {
  const match = /^microsoft\/apm\/(issue|pr)\/([1-9]\d{0,8})$/.exec(String(value));
  if (!match) throw new Error("Invalid work item ID.");
  return { kind: match[1], number: Number(match[2]) };
}

export function safeUrl(value) {
  try {
    const u = new URL(value);
    if (u.protocol !== "https:" || u.hostname !== "github.com" || u.username || u.password || u.port) return null;
    if (!u.pathname.startsWith("/microsoft/apm/") &&
        !/^\/orgs\/microsoft\/projects\/\d+(\/|$)/.test(u.pathname)) return null;
    return u.href;
  } catch {
    return null;
  }
}

export function cleanText(value) {
  return String(value ?? "")
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b/g, "[redacted credential]")
    .replace(/\bBearer\s+[A-Za-z0-9._~-]{12,}/gi, "Bearer [redacted]")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
}

export function sanitize(value) {
  if (typeof value === "string") return cleanText(value);
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, sanitize(v)]));
  return value;
}

export async function pool(values, fn, concurrency = CONCURRENCY) {
  const result = new Array(values.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(values.length, concurrency) }, async () => {
    while (next < values.length) {
      const i = next++;
      result[i] = await fn(values[i], i);
    }
  }));
  return result;
}
