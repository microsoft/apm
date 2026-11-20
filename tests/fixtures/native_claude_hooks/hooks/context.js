// Reached only through process.env.CLAUDE_PLUGIN_ROOT, never through a ./ path.
const fs = require("node:fs");
const path = require("node:path");

const root = process.env.CLAUDE_PLUGIN_ROOT;
let event = "";
try {
  event = JSON.parse(fs.readFileSync(0, "utf8") || "{}").hook_event_name || "";
} catch {
  event = "";
}
const resource = fs
  .readFileSync(path.join(root, "resources", "identity.txt"), "utf8")
  .trim();
const marker = process.env.APM_HOOK_PROBE_MARKER;
if (marker) {
  fs.appendFileSync(marker, JSON.stringify({ event, root, resource }) + "\n");
}
