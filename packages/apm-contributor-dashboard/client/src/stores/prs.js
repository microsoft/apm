import { createSignal, createResource } from "solid-js";
import { getPrs } from "../services/api";
import { refreshIntervalMs } from "../config";

const [pollTick, setPollTick] = createSignal(0);

setInterval(() => setPollTick(t => t + 1), refreshIntervalMs);

async function fetcher() {
  const data = await getPrs();
  return data;
}

const [prResource, { refetch: refetchPrs }] = createResource(pollTick, fetcher);

export { prResource, refetchPrs };
