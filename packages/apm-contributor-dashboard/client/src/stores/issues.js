import { createSignal, createResource } from "solid-js";
import { getIssues } from "../services/api";
import { refreshIntervalMs } from "../config";

const [pollTick, setPollTick] = createSignal(0);

setInterval(() => setPollTick(t => t + 1), refreshIntervalMs);

async function fetcher() {
  const data = await getIssues();
  return data;
}

const [issueResource, { refetch: refetchIssues }] = createResource(pollTick, fetcher);

export { issueResource, refetchIssues };
