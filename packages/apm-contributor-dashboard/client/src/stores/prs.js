import { createSignal, createResource } from "solid-js";
import { getPrs } from "../services/api";

const [pollTick, setPollTick] = createSignal(0);

setInterval(() => setPollTick(t => t + 1), 30000);

async function fetcher() {
  const data = await getPrs();
  return data;
}

const [prResource, { refetch: refetchPrs }] = createResource(pollTick, fetcher);

export { prResource, refetchPrs };
