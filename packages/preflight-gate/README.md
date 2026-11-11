# preflight-gate

Repo-local APM package for microsoft/apm. It ships:

- `preflight.py` -- a diff-aware local mirror of the PR CI lanes. It writes a
  receipt bound to the exact commit, base and hook bundle into
  `$(git rev-parse --git-dir)/apm-preflight/` (per worktree, never committed).
- a `preToolUse` hook (Copilot CLI / cloud agent; Claude payloads understood)
  that denies `git push`, `gh pr create`, `gh pr ready`, `gh api` PR writes,
  `*create_pull_request` tools and `*update_pull_request` calls that clear
  draft, unless that receipt is fresh, green, complete and matches the
  pushed commit. A push/PR must be its own tool call: only read-only
  segments (`cd`, `echo`, `git status/log/diff/show/fetch/rev-parse`, ...)
  may precede it, so `git commit && git push` is denied.

There is intentionally no skill. The hook is the only agent-facing surface:
every deny reason is self-sufficient (why, with HEAD sha; the exact runner
command; failed check ids and log paths; the human-only bypass), so an
agent that has never heard of preflight can recover from the denial alone.

CI stays the authority. The gate is a guardrail against skipped local
verification, not a tamper-proof control.

## Run

```bash
UV_NO_SYNC=1 python3 .github/hooks/scripts/preflight-gate/.apm/hooks/scripts/preflight.py --plan
UV_NO_SYNC=1 python3 .github/hooks/scripts/preflight-gate/.apm/hooks/scripts/preflight.py
```

Flags: `--base REF` (default `origin/main`, fetched first unless
`--no-fetch`), `--tiers T0,T1` (diagnostic; never gate-eligible),
`--keep-going`, `--json`, `--jobs N`, `--no-provision`,
`--print-ci-fingerprints`.

Tiers decide WHAT runs; scheduling is two-phase across all selected tiers.
Phase A runs every non-exclusive check concurrently (`--jobs`, default
`min(4, cpus)`). Phase B then runs the exclusive checks (xdist and E2E
suites) one at a time in tier order. A Phase A failure skips Phase B, and a
failing exclusive check skips the rest, unless `--keep-going`. A tier counts
as ran only when all its checks ran. Logs stay per check.

| Tier | Selected when | Runs |
| --- | --- | --- |
| T0 | always | ci.yml `lint` job verbatim, merge-conflict probe, `git diff --check`, ASCII and uv.lock guards, spec Mode B, CI drift, bundle staleness |
| T1 | src, tests, scripts, workflows, packages, `.apm`, specs, any `.py` | mapped unit tests, ratchet contracts, dashboard node tests, `apm audit --ci`, spec conformance |
| T2 | src, pyproject.toml, uv.lock, shared test utils | full unit suite (both CI shards, `-n auto`, no coverage floors) |
| T3 | src, tests/integration, lifecycle sharding, deps | both lifecycle smoke shards, changed integration tests |

Only a clean worktree with `--tiers auto` produces a gate-eligible receipt;
anything else exits 1.

Command checks run in a CI-like environment: an allowlist of host variables
(no `APM_*`, Claude, Copilot, `GIT_CONFIG_*` or token variables;
`GIT_EXEC_PATH` is kept), `USER=runner` as on hosted runners, an isolated
`HOME`/XDG under the preflight dir, a short `TMPDIR` (`~/.cache/apm-pf`) and
the real uv cache/config.

When test tiers are selected, `env.lock-sync` runs first and requires the uv
project environment to match `uv.lock` (CI runs `uv sync --frozen --extra
dev`). On drift the runner repairs the env in place (disable with
`--no-provision`): `uv sync --frozen --extra dev`, falling back to
`uv export` + `uv pip sync` through your configured index and an editable
re-install of the project. `uv.lock` is never modified.

If your package mirror lacks a locked version, list the package in
`APM_PREFLIGHT_LOCK_TOLERATE` (comma-separated, machine-local; never commit
a default), e.g. `APM_PREFLIGHT_LOCK_TOLERATE=filelock`. Provisioning then
installs the highest version with the same major.minor (or keeps the
installed one); the difference is printed as `[!]` and recorded in the
receipt as `lock_drift`, and the receipt stays gate-eligible. Any other
drift fails.

Files checks are known to regenerate (`VOLATILE_OUTPUTS` in the
policy, e.g. `CONFORMANCE.*`) are restored afterwards and listed in the
receipt as `restored_outputs`; any other change fails `hygiene.tree-mutated`.
`ci.drift` fails when a mirrored workflow job changes: update
`preflight_policy.py` and its fingerprints.

## Bypass (humans only)

Start the agent CLI with `APM_PREFLIGHT_BYPASS=accept-unverified`. Any other
value is ignored. Bypasses are appended to `apm-preflight/bypass.log`.

## Limits

- Windows, macOS-x86 and merge-queue lanes are not reproducible locally and
  still run in CI.
- Native Windows: the hook is a no-op (`powershell: exit 0`) and the runner
  refuses to run. Use WSL or rely on CI.
- Scope: the hook `matcher` limits it to shell, `*create_pull_request` and
  `*update_pull_request` tools, and the launcher starts Python only when the
  payload mentions `push`, `create_pull_request`, `update_pull_request` with
  draft/ready, or `gh` with `pr` / `pulls` / `createPullRequest` /
  `markPullRequestReadyForReview`. Everything else (`git status`,
  `git commit`, `gh issue ...`) exits in shell.
- Fail-open by design: a missing `python3`, a missing or broken gate script,
  or a hook timeout releases the tool call. Only a well-formed deny blocks.
- Not detected (CI remains the authority): git aliases, including
  `git -c alias.p=push p`; a dynamically built argv0 such as
  `$(echo git) push`; plumbing such as `git send-pack`; `gh` aliases.
- Tag, `--all`, `--mirror` and wildcard pushes are always denied.
- Pushes from a shell whose cwd changed in an earlier tool call are not
  tracked; `gh pr ready` does not verify the PR base.
- Hooks require `executables.allow` in the root `apm.yml`; the grant is pinned
  to this package's version, so bump both together.

## Develop

```bash
.venv/bin/pytest packages/preflight-gate/tests -q
.venv/bin/ruff check packages/preflight-gate && .venv/bin/ruff format --check packages/preflight-gate
apm install   # redeploy .github/hooks after edits
```
