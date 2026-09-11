# Conditional specialist activation

Load this reference before the review panel selects persona activation.
Test-coverage activation is owned by the P8 section in `../SKILL.md`.

## Auth Expert

Activate when the PR changes any of:
- `src/apm_cli/core/auth.py`
- `src/apm_cli/core/token_manager.py`
- `src/apm_cli/core/azure_cli.py`
- `src/apm_cli/deps/github_downloader.py`
- `src/apm_cli/marketplace/client.py`
- `src/apm_cli/utils/github_host.py`
- `src/apm_cli/install/validation.py`
- `src/apm_cli/install/pipeline.py`
- `src/apm_cli/deps/registry_proxy.py`

Fallback self-check (when no fast-path file matched): "Does this PR
change authentication behavior, token management, credential resolution,
host classification used by `AuthResolver`, git or HTTP authorization
headers, or remote-host fallback semantics? If unsure, answer YES."

## Doc Writer

Activate when the PR changes any of:
- `README.md`
- `CHANGELOG.md`
- `MANIFESTO.md`
- `docs/src/content/docs/**`
- `.apm/skills/**/*.md`
- `.apm/agents/**/*.md`
- `.github/skills/**/*.md`
- `.github/agents/**/*.md`
- `.github/instructions/**/*.md`
- `.github/workflows/*.md` (gh-aw natural-language workflows)
- `packages/apm-guide/**`

Fallback self-check (when no fast-path file matched): "Does this PR
change user-facing documentation, agent or skill prose, instruction
files, CHANGELOG entries, README claims, or any natural-language
artifact a reader will rely on? If unsure, answer YES."

When the doc-writer is active and the PR includes documentation changes,
the persona reviews them for: (a) consistency with the existing voice
and structure, (b) accuracy against the code being changed, (c)
completeness for the typical reader (no orphan claims, no missing
prerequisites), (d) discoverability (cross-links, sidebar order if
Starlight content). When the doc-writer is active because of code
changes that SHOULD have updated docs but did not, the persona surfaces
that gap as a finding.

## Performance Expert

Activate when the PR changes any of:
- `src/apm_cli/cache/**`
- `src/apm_cli/deps/**`
- `src/apm_cli/install/phases/**`
- `src/apm_cli/install/pipeline.py`
- `src/apm_cli/install/resolve.py`
- `src/apm_cli/utils/**`
- `src/apm_cli/marketplace/**`
- `src/apm_cli/compilation/**`
- `scripts/perf/**`
- `src/apm_cli/core/command_logger.py` (when the diff adds perf-instrumentation logs)

Also activate when:
- The PR description claims a performance win (speedup ratio, latency
  reduction, bytes-on-disk reduction, throughput improvement) or
  attaches a perf-harness measurement table.
- The diff introduces loops over collections (`for x in collection`)
  where the collection may grow with dependency count or file count.
- The diff adds `os.scandir`, `os.walk`, `os.listdir`, or
  `subprocess.run` calls on a path that executes per-package or
  per-dependency.
- The diff adds `x in list_variable` inside a loop body.

Fallback self-check (when no fast-path file matched): "Does this PR
change the hot path for dependency download, materialization, cache
layout, transport (git protocol, partial clone, sparse checkout),
parallelism, or any user-visible install/update wall-time? Does it
introduce an algorithmic complexity regression (O(n^2) loops, repeated
I/O, missing indexes, unconditional full scans, blocking synchronous
calls, heavy top-level imports)? If unsure, answer YES."

When active, the performance-expert reviews against BOTH:
1. The package-manager performance playbook: transport minimization
   (depth, filter, sparse scope), cache layering and dedup keys,
   parallelism and lock contention, working-tree materialization cost,
   perf-harness methodology (cache wipe, warm/cold separation,
   statistical noise), and pervasive application of the chosen
   technique across install / update / run surfaces.
2. The algorithmic performance lens (Big O analysis): complexity class
   of every loop/lookup in the diff, index vs linear scan patterns,
   unconditional expensive operations, import startup costs, redundant
   computation, and parallelism opportunities. See the agent's
   `references/algorithmic-patterns.md` for the full pattern catalogue.
