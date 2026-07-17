---
title: Install packages
description: Add APM packages to your project as dependencies and let install resolve everything.
---

To add an APM package to your project, declare it in `apm.yml` under
`dependencies.apm:` and run `apm install`. APM resolves the graph,
scans every primitive, writes `apm.lock.yaml`, and deploys the result
into each agent harness directory you target.

```bash
apm install microsoft/apm-sample-package
```

That command edits `apm.yml`, fetches the package, walks its
transitive deps, runs the security scan, writes the lockfile, and
integrates skills, prompts, instructions, agents, hooks, and commands
into every detected harness. The next contributor runs bare
`apm install` and gets the same bytes, pinned by commit and content
hash.

This page covers APM packages. For MCP servers, see
[Install MCP servers](../install-mcp-servers/). For local bundles, see
[Deploy a bundle](../deploy-a-bundle/).

## Add a dependency

Two ways. They produce the same `apm.yml`.

**With the CLI.** Pass the package as a positional argument. APM
resolves it, validates it, and appends it to `dependencies.apm:` in
`apm.yml`:

```bash
apm install microsoft/apm-sample-package#v1.0.0
apm install github/awesome-copilot/skills/review-and-refactor
apm install ./packages/my-shared-skills        # local path
apm install --dev acme/internal-debug-agents   # devDependencies
```

**By editing `apm.yml`.** Add entries under `dependencies.apm:` then
run bare `apm install`:

```yaml
name: my-project
version: 0.1.0
dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0
    - github/awesome-copilot/skills/review-and-refactor
    - https://gitlab.com/acme/coding-standards.git
    - ./packages/my-shared-skills
devDependencies:
  apm:
    - acme/internal-debug-agents
```

```bash
apm install
```

Supported reference forms: `owner/repo` shorthand (with optional
`#ref` for a tag, branch, or commit), full HTTPS or SSH git URLs,
virtual subdirectory paths into a monorepo, single-file references,
and local paths starting with `./`, `../`, or `/`. For private hosts
and tokens, see [Authentication](../authentication/).

## What `apm install` does

The pipeline is deterministic. Each phase must pass before the next runs.

1. **Resolve.** Walk `dependencies.apm:` and `devDependencies.apm:`,
   follow transitive deps, pick versions.
2. **Policy gate.** If `apm-policy.yml` is in scope (locally or via
   your org), every resolved dependency is checked against the
   allow-list before anything touches disk. Skip with `--no-policy`
   for a single invocation; this does not bypass `apm audit --ci`.
3. **Scan.** The pre-deploy security scan inspects every primitive
   for hidden Unicode (zero-width characters, bidi controls, tag
   characters). Critical findings block the install. Override with
   `--force`.
4. **Integrate.** Write primitives into each target harness's native
   directory (`.github/`, `.claude/`, `.cursor/`, `.opencode/`,
   `.codex/`, `.gemini/`, `.windsurf/`, `.kiro/`) and the cross-tool
   `.agents/skills/` directory.
5. **Lockfile.** Write `apm.lock.yaml` with pinned versions, content
   hashes, and the resolved dependency set.

For the deeper view of how compile fits in, see
[Lifecycle](../../concepts/lifecycle/).

:::note[Coming from npm?]
`apm install` mirrors `npm install` deliberately. The big difference:
APM also runs a security scan and, if present, an org policy gate
before writing anything to disk. To refresh dependencies to their
latest matching versions or refs, use `apm update` (mirrors `npm update`). To
upgrade the `apm` CLI binary itself, use `apm self-update`.
:::

## Where files land

APM detects which harnesses your project uses and deploys to all of
them. Detection priority:

1. `--target <slug>` flag (highest).
2. The `targets:` field in `apm.yml`.
3. Auto-detect: any harness directory (`.github/`, `.claude/`,
   `.cursor/`, `.opencode/`, `.codex/`, `.gemini/`, `.windsurf/`, `.kiro/`)
   that already exists in the workspace.
4. Fallback: minimal output to `AGENTS.md` only.

Pin targets explicitly when you want reproducibility across machines:

```yaml
name: my-project
targets:
  - copilot
  - claude
  - cursor
```

For the full reach map of which primitive lands where on each
harness, see [Primitives and targets](../../concepts/primitives-and-targets/).

Rule sync to Cursor (`.cursor/rules/`), Claude Code (`.claude/rules/`), Windsurf (`.windsurf/rules/`), and Kiro (`.kiro/steering/`) is automatic and idempotent -- re-running `apm install` adopts unchanged rules without rewriting them.

## What to commit

Commit `apm.yml`, `apm.lock.yaml`, and every harness directory APM writes to
(`.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.gemini/`, `.windsurf/`,
`.kiro/`). Committed deployed files give teammates and cloud Copilot instant agent
context on clone, before they run `apm install`.

Add `apm_modules/` to `.gitignore` -- it is the package cache and is rebuilt from
the lockfile on every `apm install`. APM adds the entry automatically on first install.

See the [Quickstart](../../quickstart/#what-to-commit) for the full table and
rationale.

## Transitive dependencies and the lockfile

`apm install` resolves the full dependency graph, not just your
top-level entries. Versions and content hashes are pinned in
`apm.lock.yaml` so every contributor and CI run installs the exact
same bytes. Commit the lockfile.

:::note[Lockfile replay]
Your lockfile pins every package your dependencies pull in, including
transitive packages resolved at lock time. If an upstream package later moves
one of its own entries between `dependencies.apm` and `devDependencies.apm`, an
existing lockfile still replays the previously recorded commits. Run
`apm update` or `apm lock --update`, or delete `apm.lock.yaml` and re-run
`apm install` after changing `apm.yml`, when you want APM to read the newer
upstream manifests and produce a new graph. See the
[lockfile specification](../../reference/lockfile-spec/) for the replay
contract.
:::

Transitive **APM** packages flow through automatically. Transitive
**MCP servers** are gated: if a deep dependency declares a new MCP
server, install pauses and asks you to re-declare it in your
top-level `apm.yml`. Use `--trust-transitive-mcp` to skip this in
trusted environments. See
[Drift and secure-by-default](../drift-and-secure-by-default/) for
the rationale.

## Inspect what got installed

```bash
apm list                  # list scripts declared in apm.yml
apm view <package>        # details for one installed package
```

`apm view` reads from `apm.lock.yaml` and `apm_modules/` -- use it to
confirm what shipped without re-running install. `apm list` shows the
runnable scripts your manifest exposes (see
[Run scripts](../run-scripts/)).

## Useful flags

```bash
apm install --dry-run                  # resolve and print the plan; no writes
apm install --target claude,cursor     # only deploy to these harnesses
apm install --exclude gemini           # deploy to all targets except gemini
apm install --only apm                 # skip MCP server integration this run
apm install --frozen                   # CI: lockfile-only; fail on drift
apm install --refresh                  # bypass the cache; re-fetch everything
apm install --dev                      # treat positional args as devDependencies
apm install -g <package>               # install to user scope (~/.apm/)
apm install -v                         # verbose: show resolution and integration
```

Targets with native user-scope instruction files pick up global instructions
during install. Targets whose user-scope instruction surface is a root context
file require explicit
[`apm compile --global`](../../reference/cli/compile/#global-compilation);
`apm install -g` prints a hint and writes no root context file.
For project-scope installs, compile-only targets such as Gemini,
agents-protocol hosts, and Claude print a hint when dependency instructions
require `apm compile`; install leaves `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md`
unchanged until that explicit step.

For the full flag reference, run `apm install --help` or see
[CLI commands](../../reference/cli/install/).

## When things go wrong

- **Critical security finding.** Install aborts with the offending
  characters and file path. Patch upstream when you can; use
  `--force` only when you can document the exception.
- **Policy violation.** Install aborts before any file is written.
  Adjust the dependency or update org policy. See
  [Governance on the consumer ramp](../governance-on-the-consumer-ramp/).
- **Auth failure on a private repo.** Set `GITHUB_APM_PAT` or the
  matching host token. See [Authentication](../authentication/) and
  [Private and org packages](../private-and-org-packages/).
- **Drift between `apm_modules/` and the lockfile.** Run
  `apm audit --ci` locally to reproduce the CI gate; see
  [Update and refresh](../update-and-refresh/) to recover.

Once your dependencies are installed, scripts run them.
[Run scripts](../run-scripts/) shows how to wire `apm.yml`'s
`scripts:` block to invoke compiled prompts in any harness.
