import * as fs from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const APM_CLI_ROOT = resolve(fileURLToPath(import.meta.url), "../..");
const MANIFEST_PATH = resolve(APM_CLI_ROOT, "package.json");

const rootManifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf-8"));

if (
	typeof process.env.GITHUB_SHA !== "string" ||
	process.env.GITHUB_SHA === ""
) {
	throw new Error("GITHUB_SHA environment variable is undefined");
}

const version = `0.0.0-preview.${process.env.GITHUB_SHA.substring(0, 7)}`;
rootManifest.version = version;

const content = JSON.stringify(rootManifest);
fs.writeFileSync(MANIFEST_PATH, content);

console.log(version);
