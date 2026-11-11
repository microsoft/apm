# preflight-gate

Repo-local APM package for microsoft/apm. It ships:

- `preflight.py` -- a diff-aware local mirror of the PR CI lanes. It writes a
  receipt bound to the exact commit, base and hook bundle into
  `$(git rev-parse --git-dir)/apm-preflight/` (per worktree, never committed).
- a `preToolUse` hook (Copilot CLI / cloud agent; Claude payloads understood)
  that denies `git push`, `gh pr create`, `gh pr ready`, `gh api` PR writes
  and `*create_pull_request` tools unless that receipt is fresh, green,
  complete and matches the pushed commit.

There is intentionally no skill. The hook is the only agent-facing surface:
every deny reason is self-sufficient (why, with HEAD sha; the exact runner
command; failed check ids and log paths; the CI-only-lanes note; the
human-only bypass), so an agent that has never heard of preflight can
recover from the denial alone.

CI stays the authority. The gate is a guardrail against skipped local
verification, not a tamper-proof control.

## Run

```bash
UV_NO_SYNC=1 python3 .github/hooks/scripts/preflight-gate/.apm/hooks/scripts/preflight.py --plan
UV_NO_SYNC=1 python3 .github/hooks/scripts/preflight-gate/.apm/hooks/scripts/preflight.py
```

Flags: `--base REF` (default `origin/main`, fetched first unless
`--no-fetch`), `--tiers T0,T1` (diagnostic; never gate-eligible),
`--keep-going`, `--json`, `--print-ci-fingerprints`.

| Tier | Selected when | Runs |
| --- | --- | --- |
| T0 | always | ci.yml `lint` job verbatim, merge-conflict probe, `git diff --check`, ASCII and uv.lock guards, spec Mode B, CI drift, bundle staleness |
| T1 | src, tests, scripts, workflows, packages, `.apm`, specs, any `.py` | mapped unit tests, ratchet contracts, dashboard node tests, `apm audit --ci`, spec conformance |
| T2 | src, pyproject.toml, uv.lock, shared test utils | full unit suite (both CI shards, `-n auto`, no coverage floors) |
| T3 | src, tests/integration, lifecycle sharding, deps | both lifecycle smoke shards, changed integration tests |

Only a clean worktree with `--tiers auto` produces a gate-eligible receipt.
`ci.drift` fails when a mirrored workflow job changes: update
`preflight_policy.py` and its fingerprints.

## Bypass (humans only)

Start the agent CLI with `APM_PREFLIGHT_BYPASS=accept-unverified`. Any other
value is ignored. Bypasses are appended to `apm-preflight/bypass.log`.

## Limits

- Native Windows: the hook is a no-op (`powershell: exit 0`) and the runner
  refuses to run. Use WSL or rely on CI.
- Fail-open by design: a missing `python3`, a missing or broken gate script,
  or a hook timeout releases the tool call. Only a well-formed deny blocks.
- Tag, `--all`, `--mirror` and wildcard pushes are always denied.
- `gh` aliases and pushes from a shell whose cwd changed in an earlier tool
  call are not tracked; `gh pr ready` does not verify the PR base.
- Hooks require `executables.allow` in the root `apm.yml`; the grant is pinned
  to this package's version, so bump both together.

## Develop

```bash
.venv/bin/pytest packages/preflight-gate/tests -q
.venv/bin/ruff check packages/preflight-gate && .venv/bin/ruff format --check packages/preflight-gate
apm install   # redeploy .github/hooks after edits
```
