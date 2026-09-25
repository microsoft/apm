import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { Governance } from "./governance.mjs";

const exec = promisify(execFile);
const configUrl = new URL("./config.mjs", import.meta.url);
const readStore = async override => {
  const env = { ...process.env };
  delete env.APM_MAINTAINER_DATA_DIR;
  if (override !== undefined) env.APM_MAINTAINER_DATA_DIR = override;
  const { stdout } = await exec(process.execPath, ["--input-type=module", "-e",
    `const { STORE } = await import(${JSON.stringify(configUrl.href)}); process.stdout.write(STORE);`], { env });
  return stdout;
};

test("backup defaults to local private state without an original session path", async () => {
  assert.equal(await readStore(), fileURLToPath(new URL("./.local/", import.meta.url)));
});

test("explicit absolute state override is preserved; relative overrides fail visibly", async () => {
  const path = fileURLToPath(new URL("./.local/isolated/", import.meta.url));
  assert.equal(await readStore(path), path);
  await assert.rejects(() => readStore("relative-state"), /must be an absolute path/);
});

test("missing or relative trusted checkout fails before any GitHub read or helper execution", async () => {
  const github = { run: async () => assert.fail("No GitHub read before root configuration") };
  for (const root of [null, "relative-checkout"]) {
    await assert.rejects(() => new Governance(github, { root }).trust(), /APM_MAINTAINER_TRUSTED_ROOT/);
  }
});
