/**
 * ESM loader that maps bare "solid-js" imports to the local client node_modules
 * dev build, which works in Node.js without DOM or hydration context.
 *
 * Usage: node --loader ./tests/solid-loader.mjs --test tests/stores.test.mjs
 */
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Resolve to the dev build (works in Node without DOM/hydration)
const SOLID_DEV_URL = pathToFileURL(
  path.resolve(__dirname, "../client/node_modules/solid-js/dist/dev.js"),
).href;

export async function resolve(specifier, context, nextResolve) {
  // Map the solid-js bare specifier to the local dev build.
  if (specifier === "solid-js") {
    return { shortCircuit: true, url: SOLID_DEV_URL };
  }

  // Vite resolves extensionless relative imports; Node.js ESM requires ".js".
  // When a relative specifier has no extension, try appending ".js".
  if (
    (specifier.startsWith("./") || specifier.startsWith("../")) &&
    !specifier.includes("?") &&
    !/\.[a-z]+$/i.test(specifier)
  ) {
    return nextResolve(specifier + ".js", context);
  }

  return nextResolve(specifier, context);
}
