import { createSignal, createResource } from "solid-js";
import { getTriageData } from "../services/api";

// fetchTick starts as null -- triage data is not loaded until the tab is activated.
// Once activateTriage() is called, the resource fetches and tracks manual refetch.
const [fetchTick, setFetchTick] = createSignal(null);

async function fetcher(tick) {
  if (tick === null) return null;
  return getTriageData();
}

const [triageResource, { refetch: refetchTriage }] = createResource(fetchTick, fetcher);

export function activateTriage() {
  if (fetchTick() === null) setFetchTick(0);
}

export { triageResource, refetchTriage };
