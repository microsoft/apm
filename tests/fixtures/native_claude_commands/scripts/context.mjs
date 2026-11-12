import { appendFileSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const observation = {
  root,
  resource: readFileSync(resolve(root, "resources/identity.txt"), "utf8").trim(),
  argument: process.argv[2] ?? "",
};
if (process.env.APM_COMMAND_PROBE_MARKER) {
  appendFileSync(process.env.APM_COMMAND_PROBE_MARKER, JSON.stringify(observation) + "\n");
}
console.log(JSON.stringify(observation));
