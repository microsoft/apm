import { createSignal, createResource } from "solid-js";
import { getIssues } from "../services/api";

const [pollTick, setPollTick] = createSignal(0);

// Auto-poll every 30s
setInterval(() => setPollTick(t => t + 1), 30000);

// Quick re-fetch after 3s to pick up PR enrichment data
setTimeout(() => setPollTick(t => t + 1), 3000);

async function fetcher() {
  const data = await getIssues();
  return data;
}

const [issueResource, { refetch: refetchIssues }] = createResource(pollTick, fetcher);

export { issueResource, refetchIssues };
