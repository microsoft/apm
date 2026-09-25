import { Store } from "./store.mjs";
import { startServer } from "./server.mjs";

const command = process.argv[2] || "serve";
if (!["serve", "refresh"].includes(command) || process.argv.length > 3) {
  throw new Error("Usage: node standalone.mjs [serve|refresh]");
}
const store = await new Store().load();
if (command === "refresh") {
  await store.refresh();
  if (store.state().items.some(i => i.id === store.view.selectedId)) await store.select(store.view.selectedId);
  const state = store.state();
  process.stdout.write(JSON.stringify({
    sources: state.sources, entities: state.items.length,
    workItems: state.workIds.length, counts: state.counts,
    selectedId: state.view.selectedId, error: state.error,
  }, null, 2) + "\n");
  if (state.error || Object.values(state.sources).some(s => s.status !== "current")) process.exitCode = 1;
} else {
  const entry = await startServer(store);
  process.stdout.write(`${entry.url}\n`);
  let closing = false;
  const close = async () => {
    if (closing) return;
    closing = true;
    await entry.close();
    process.exit(0);
  };
  process.on("SIGINT", close);
  process.on("SIGTERM", close);
}
