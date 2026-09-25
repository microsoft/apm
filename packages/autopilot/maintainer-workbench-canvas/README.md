# Maintainer workbench canvas source backup

Source snapshot of the session-local `maintainer-control-plane` canvas.
This is a development backup, not an installed APM package or a replacement
for `autopilot-maintainer-canvas`. Checking out this branch does not activate it.

The snapshot includes the runtime, fixture tests and validation harnesses:

- Attention separates scope decisions (issues), workflow permissions (PRs)
  and PR reviews (PRs). Permission and review counts can overlap.
- Selection stays responsive during portfolio reads, with separate progress
  for GitHub reads, explanation generation and workflow execution.
- Explanation requests dispatch independent background subagents with explicit
  `model: "gpt-6-sol"`. The shared relay and prompt hook require this model,
  not the parent default or a silent fallback.
- Workers claim a frozen source packet, read it through bounded SDK pages and
  save grounded prose directly to the canvas. Source changes, cancellation
  and failures cannot turn stale results into current explanations.
- Workflow approval requires a fresh exact account/commit/run preview and an
  explicit confirmation. Durable receipts are separate from observed CI results.
  Other operational requests retain their parent confirmation gates.

No live snapshots, approval/explanation journals, claim tokens, credentials,
session transcripts, private design notes, screenshots or installed dependencies
are included. Restoring the source starts with empty local state.

## Restore deliberately

Requires a GitHub Copilot host with extension canvases, SDK tools/hooks and
background task agents, Node.js, and an authenticated `gh` installation with
the necessary repository access.

1. Choose project or session extension scope in Copilot. Do not load another
   copy alongside an existing `maintainer-control-plane` provider: its canvas
   and tool names would collide.
2. Copy the tracked source files from `extension/` into that scope's
   `maintainer-control-plane/` directory. For project scope, this is
   `.github/extensions/maintainer-control-plane/`.
3. Configure the environment inherited by the Copilot host:

   ```sh
   export APM_MAINTAINER_TRUSTED_ROOT="/absolute/path/to/trusted/microsoft-apm"
   export APM_MAINTAINER_DATA_DIR="/absolute/path/to/private/workbench-state"
   ```

4. Start/reload the extension provider and ask Copilot to open canvas
   `maintainer-control-plane` for `microsoft/apm`.

The trusted checkout must contain governance helpers matching the current
upstream default branch. The existing blob-hash verification remains mandatory;
missing configuration fails closed instead of trusting the current contribution
worktree. Without a data-directory override, state goes under the extension's
ignored `.local/` directory. Do not commit or share that directory.

These two runtime paths are the only adaptations from the running session's
source: no personal checkout path or old session-state directory is required.
The repository and GitHub mutation endpoints remain restricted to `microsoft/apm`.
The SDK is supplied by Copilot; do not install or vendor it into the extension.

## Fixture verification

From the repository root:

```sh
export APM_MAINTAINER_TRUSTED_ROOT="$(git rev-parse --show-toplevel)"
cd packages/autopilot/maintainer-workbench-canvas/extension
node --test --test-timeout=20000 \
  tests.mjs lifecycle-tests.mjs attention-tests.mjs explanation-tests.mjs \
  responsive-tests.mjs workflow-approval-tests.mjs runtime-path-tests.mjs
```

The Node suites use fixtures, not live workflow approvals. The optional
`responsive-dom.mjs` harness expects JSDOM under
`validation-jsdom/node_modules/` inside the effective data directory; it runs with
`node --experimental-vm-modules responsive-dom.mjs`. That dependency is not
vendored. Browser harnesses are historical macOS development utilities;
`browser-test.mjs` also expects a local snapshot. They are not portable CI gates.
The latest interaction increment was checked nonvisually; browser startup crashes
prevented a new layout/contrast verification.

Do not reload a provider while an approval or explanation worker is active.
Reload invalidates outstanding claims and previews rather than replaying them;
an existing subagent can lose its tool catalog after a provider reload.
