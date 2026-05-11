---
title: Lifecycle
description: The five steps every APM project moves through, from init to audit.
sidebar:
  order: 4
---

APM has five lifecycle steps. Most projects use all five; small ones use three.

```
   init  ->  install  ->  compile  ->  run
                                          |
                                          v
                                       audit
                                          |
                                          +--> back to install (fix drift)
```

`init` scaffolds the project. `install` resolves dependencies, scans them, and writes the lockfile. `compile` transforms primitives into the formats each agent harness expects. `run` invokes a script declared in `apm.yml`. `audit` rebuilds the deployed context in scratch and diffs it against your working tree to catch drift before it ships.

You will use `install` and `run` daily, `audit` in CI, and `init` and `compile` rarely.

## 1. INIT

```bash
apm init [project-name]
```

Scaffolds a new APM project in the current directory.

`apm init` writes three things: an `apm.yml` manifest, a `.apm/` directory for your local primitives (instructions, prompts, skills, agents, hooks), and the agent target directories you select (for example `.github/`, `.claude/`, `.cursor/`). It auto-detects sensible defaults for `name`, `author`, and `description` from your git config and surrounding directory; pass `-y` to accept them without prompts.

Targets are picked in priority order. An explicit `--target copilot,claude` flag wins. Otherwise an interactive checklist runs. Otherwise APM scans the working tree for signal directories (`.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.codex/`, `.gemini/`, `.windsurf/`) and pre-checks every harness it finds. With `-y` and no flag, all detected harnesses are written into `apm.yml`. See [primitives and targets](/apm/concepts/primitives-and-targets/) for what each target actually receives.

**Common surprises**

- Re-running `apm init` in a directory that already has `apm.yml` warns and exits unless you pass `-y` (which overwrites the manifest).
- If you set `targets:` to an empty list in `apm.yml`, target detection runs again at compile time. To pin targets, list them explicitly.

**Read more:** [`apm init` reference](/apm/reference/cli/install/), [package anatomy](/apm/concepts/package-anatomy/).

## 2. INSTALL

```bash
apm install [packages...]
```

Resolves the dependency graph declared in `apm.yml`, runs the security scan, and writes `apm.lock.yaml`.

Order of operations is deterministic and worth memorizing:

1. **Resolve** -- walk `dependencies` and `devDependencies` (APM packages, MCP servers, Claude skills, plugin collections), follow transitive deps, pick versions.
2. **Policy gate** -- if `apm-policy.yml` is discovered (locally or via your repo's org), every resolved dependency is checked against the allow-list before anything touches disk. Pass `--no-policy` to skip the org policy gate for one invocation; this does not bypass `apm audit --ci`.
3. **Scan** -- the pre-deploy security scan inspects every primitive for hidden Unicode (zero-width characters, bidi controls, tag characters). Critical findings block the install. Pass `--force` to deploy anyway.
4. **Integrate** -- write primitives into each target harness's native directory (`.github/`, `.claude/`, etc.) and merge MCP server configs into the harness-specific config files.
5. **Lockfile** -- write `apm.lock.yaml` with pinned versions, content hashes, and the resolved MCP server set.

`apm install` with no arguments installs from the existing manifest. `apm install <package>` adds a new dependency, re-runs the full pipeline, and updates both `apm.yml` and `apm.lock.yaml`. `--dry-run` runs steps 1 and 2 only and prints the plan.

:::note[Coming from npm?]
`apm install` mirrors `npm install` deliberately. The big difference: APM also runs a security scan and, if present, an org policy gate before writing anything to disk.
:::

**Common surprises**

- The scan is not optional in normal operation. If you need to land an install with a known critical finding (for example, an upstream package you cannot patch yet), use `--force` and document the exception.
- Transitive MCP servers are gated behind explicit trust. If a deep dependency declares a new MCP server, install pauses to ask you to re-declare it in your top-level `apm.yml`. Use `--trust-transitive-mcp` to skip this in trusted environments.

**Read more:** [`apm install` reference](/apm/reference/cli/install/), [security](/apm/enterprise/security/), [policy reference](/apm/enterprise/policy-reference/).

## 3. COMPILE

```bash
apm compile [--target <list>]
```

Transforms the primitives in `.apm/` (and dependencies under `apm_modules/`) into harness-native files: `AGENTS.md` for Codex, `GEMINI.md` for Gemini, populated `.cursor/`, `.opencode/`, `.windsurf/` directories, and so on.

Most users never call `apm compile` directly. `apm install` runs it as part of the integrate phase, and `apm run` auto-compiles any `.prompt.md` files referenced by a script just before execution. Reach for `apm compile` when you want to inspect what will be deployed without changing dependencies, when you are iterating on local primitives between installs, or when you only need output for one harness.

The `--target` flag accepts a comma-separated list (`copilot,claude,cursor,opencode,codex,gemini,windsurf,agent-skills`) or `all`. `--dry-run` prints placement decisions without writing files. `--validate` checks primitive frontmatter and structure without producing output. `--watch` re-runs compilation on every change.

**Common surprises**

- Running `apm compile` does not re-run the security scan. The scan happens at install time. If you hand-edit primitives between installs, run `apm audit` to scan them.
- `--clean` removes orphaned `AGENTS.md` files from previous compilations. Without it, removed primitives can leave stale output behind.

**Read more:** [`apm compile` reference](/apm/reference/cli/install/), [compilation guide](/apm/producer/compile/).

## 4. RUN

```bash
apm run <script-name> [--param key=value ...]
```

Executes a named script from the `scripts:` block in `apm.yml`.

The `scripts:` block is a flat string-to-string mapping, mirroring `package.json`:

```yaml
name: my-project
version: 0.1.0
scripts:
  start: copilot --prompt .apm/prompts/review.prompt.md
  review: copilot --prompt .apm/prompts/review.prompt.md
  test: pytest tests/
```

`apm run` with no script name runs `start`, matching npm. Before invoking the command, APM scans it for `.prompt.md` references, compiles each one to `.apm/compiled/<name>.txt`, and substitutes the compiled path into the command line. Use `--param key=value` (repeatable) to pass parameters that get interpolated into prompt frontmatter.

To preview what will run without executing, use `apm preview <script-name>`. It prints the original command, the rewritten command after prompt compilation, and the list of compiled files.

:::note[Coming from npm?]
The `scripts:` shape is intentionally identical to `package.json`. Object-form scripts (with `description`, `env`, etc.) are not supported; keep them strings.
:::

**Common surprises**

- A script that does not reference any `.prompt.md` file runs as-is. APM only rewrites the command when it finds `.prompt.md` arguments.
- Parameters passed with `--param` only reach prompt files. They do not become shell environment variables.

**Read more:** [`apm run` reference](/apm/reference/cli/install/), [agent workflows guide](/apm/producer/author-primitives/instructions-and-agents/).

## 5. AUDIT

```bash
apm audit                  # local: scan deployed files for hidden Unicode
apm audit --ci             # CI gate: lockfile consistency + drift replay
apm audit --file <path>    # standalone: scan an arbitrary file
```

`apm audit` is the explicit reporting and remediation tool that complements the built-in scan run by `install`. It has two modes worth understanding separately.

**Local mode** (`apm audit`, optionally with `--strip` or `--file <path>`) scans installed primitives -- or any file you point at -- for hidden Unicode and reports findings as text, JSON, SARIF, or markdown. With `--strip`, it removes hidden characters in place, preserving emoji and whitespace. Use `--dry-run` to preview the strip.

**CI mode** (`apm audit --ci`) runs the eight baseline consistency checks in order: `lockfile-exists`, `ref-consistency`, `deployed-files-present`, `no-orphaned-packages`, `skill-subset-consistency`, `config-consistency`, `content-integrity`, and `includes-consent`. After those pass, it performs an install-replay drift check. APM rebuilds the deployed context in a scratch directory and diffs it against your working tree, catching hand-edits to `apm_modules/` or generated files before they ship. Pass `--no-drift` to skip the replay in performance-constrained loops; pass `--no-fail-fast` to run all checks even after a failure. With `--policy <source>` it also evaluates org policy against the lockfile.

**Common surprises**

- `apm audit --ci` exits 1 on any failure -- this is the gate you wire into branch protection. The local `apm audit` exits 0 even when findings exist, unless you also pass `--strip` and writes fail.
- The drift check rebuilds the full context from scratch; on large repos, expect a few seconds of overhead. If your CI loop cannot afford it, narrow with `--no-drift` and accept reduced coverage.

**Read more:** [`apm audit` reference](/apm/reference/cli/install/), [policy reference](/apm/enterprise/policy-reference/) for the full check list, [security](/apm/enterprise/security/).
