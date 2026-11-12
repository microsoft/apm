// Execute the original loader from Kiro CLI 2.33.0's verified @kiro/agent bundle.
// No authentication, model calls, live MCP server, or personal profiles are used.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const [bundle, workspace, home, scope] = process.argv.slice(2);
assert.ok(bundle && workspace && home && ["project", "user"].includes(scope));
const bytes = fs.readFileSync(bundle);
const bundleSha = crypto.createHash("sha256").update(bytes).digest("hex");
assert.equal(bundleSha, "686df0ed3d14a7d9680c4fa590c4bbb96581158508c27047f3ebb784e241c8b0");
const source = bytes.toString("utf8");
const slice = (start, end) => {
  const offset = source.indexOf(start);
  assert.notEqual(offset, -1);
  const stop = source.indexOf(end, offset);
  assert.ok(stop > offset);
  return source.slice(offset, stop);
};
const captured = [
  slice("function XR(", "function AQr("),
  slice("function Cf(", "var c6="),
  slice("function R2e(", "var kft="),
  slice("var YQr=", "function QQr("),
  slice("var een=15e3;", "uk();qR();function a5o("),
].join("\n");
const capturedSha = crypto.createHash("sha256").update(captured).digest("hex");
assert.equal(capturedSha, "fad19e1983f1dc5107f1c455cad93198a8dd5a7eba5d42c4057c3e06f51f7f62");
const context = vm.createContext({
  ZQr: fs, s5o: fs, ZW: path, UCe: path,
  AbortSignal, DOMException, Promise, Map, Set, Error,
  S: { trace() {}, debug() {}, warn() {} },
  ke: async (_operation, _domain, callback) => callback(),
  bn: (error) => error?.name === "AbortError",
  qn: (error) => ({ error: String(error) }),
  $8: Error,
  Ay: class { addWorkspacePaths() {} dispose() {} },
  rc: (resource) => resource,
  o5o() {},
});
vm.runInContext(captured, context);
const Loader = vm.runInContext("LWe", context);
let mcpRequests = 0;
const loader = new Loader({
  workspaceTrusted: true,
  workspacePaths: [workspace],
  userConfigDir: path.join(home, ".kiro"),
  onChange() {},
  hasNonPromptCommandNamed: async () => false,
  getMcpStatus: () => ({
    servers: [{
      name: "fixture-mcp", status: "connected", origin: { kind: "user" },
      prompts: [{ name: "review", promptName: "review", arguments: [] }],
    }],
  }),
  getMcpPrompt: async () => {
    mcpRequests += 1;
    return { messages: [{ content: { type: "text", text: "MCP_CONTROL" } }] };
  },
});
const local = path.join(workspace, ".kiro/prompts/review.md");
const global = path.join(home, ".kiro/prompts/review.md");
const installed = scope === "project" ? local : global;
const control = scope === "project" ? global : local;
assert.equal(fs.existsSync(control), false, "Fixture must own both isolated prompt paths");
const installedBytes = fs.readFileSync(installed);
const invocation = '/review "a b" c d e f g h i j k';
const allArguments = "a b c d e f g h i j k";
const expected = installedBytes.toString("utf8")
  .replaceAll("${1}", "a b").replaceAll("${10}", "k")
  .replaceAll("$ARGUMENTS", allArguments).replaceAll("${@}", allArguments);
let discovery;
try {
  discovery = await loader.listPrompts();
  assert.ok(discovery.some((item) => item.name === "review"));
  assert.ok(!discovery.some((item) => item.name === "review.prompt"));
  assert.ok(!discovery.some((item) => item.name === "not-the-command"));
  assert.equal(await loader.resolve(invocation), expected);
  fs.mkdirSync(path.dirname(control), { recursive: true });
  fs.writeFileSync(control, "PRECEDENCE_CONTROL\n");
  assert.equal(await loader.resolve("/review"),
    scope === "project"
      ? installedBytes.toString("utf8").replaceAll("${1}", "").replaceAll("${10}", "")
        .replaceAll("$ARGUMENTS", "").replaceAll("${@}", "")
      : "PRECEDENCE_CONTROL\n");
  assert.equal(mcpRequests, 0);
  fs.unlinkSync(control);
  assert.equal(await loader.resolve(invocation), expected);
  fs.unlinkSync(installed);
  assert.equal(await loader.resolve("/review"), "MCP_CONTROL");
  assert.equal(mcpRequests, 1);
} finally {
  if (fs.existsSync(control)) fs.unlinkSync(control);
  fs.writeFileSync(installed, installedBytes);
  loader.dispose();
}
console.log(JSON.stringify({
  kiro_cli: "2.33.0", native_agent: "0.66.48",
  bundle_sha256: bundleSha, captured_loader_sha256: capturedSha,
  scope, discovery, resolved_installed_body: expected,
  project_over_user: true, file_over_mcp: true, mcp_fallback: true,
  limits: [
    "Original released loader with real APM-installed files, not authenticated chat",
    "MCP status and prompt callbacks are fixture inputs, not a live server",
    "Logging, telemetry, resource rendering and watching use inert host adapters",
  ],
}, null, 2));
