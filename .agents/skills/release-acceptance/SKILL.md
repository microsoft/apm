---
name: release-acceptance
description: >-
  Use this skill to accept or reject an APM release candidate before
  tagging: walk the program's critical user journeys (consume, publish,
  govern) end to end on the exact candidate build, look for broken
  hints, inconsistent commands, scopes, modes and targets, trust and
  integrity gaps, upgrade and environment breaks, verify every finding
  independently, turn each one into a failing-before lifecycle test
  brief, and return one release-gate row (GO or NO-GO) with evidence.
  Activate on "red team the release", "acceptance test the RC", "UX
  acceptance", "pre-release QA", "break the train", "is the release
  candidate ready", "re-check the findings on the release PR", or
  before or after cut-release, even when the user does not say
  "skill". Mode `recheck` re-verifies open findings on a moved head.
  It never fixes code, opens PRs, tags, or files issues; reviewing one
  PR's diff belongs to autopilot-pr-review-worker and writing the tests
  belongs to apm-lifecycle-tests.
---

# release-acceptance -- discover what the regression net does not know yet

The test-corpus program is the regression net: deterministic tests,
receipts and journey goldens in CI. This skill is the DISCOVERY engine
that runs on the exact release candidate before the tag. It walks the
program's journeys as a real user would, executes every hint the CLI
prints, repeats steps in sibling cells, and reports what breaks. Every
confirmed finding becomes a failing-before test through
`apm-lifecycle-tests`, so each pass makes the net stronger.

Why it exists: in 0.37, plain user journeys found about 80 issues where
targeted reviews found 14, and many were hints that sent users to a
command that did not work. No test runs the commands the CLI tells
users to run.

## When to use

- Before cut-release: mode `full` on the green release-train tip.
- After cut-release: mode `recheck` on the release PR's exact SHA,
  right before the human tags.
- Any time someone asks whether a candidate build is ready.

Not for: reviewing one PR (autopilot-pr-review-worker), writing tests
(apm-lifecycle-tests), docs prose checks (docs-grounding-verifier),
version bumps (cut-release), tagging (a human).

## Operating rules

- Facts come from tools. Exit codes, output lines, file contents and
  SHAs are read from `scripts/` output, never asserted from memory.
- One writer. Only this orchestrator thread writes the ledger, the
  coverage table, the report and the Atlas drafts. Workers write only
  inside their own world directory.
- Capacity. At most 2 workers at a time (`--slots`, default 2). Run
  `scripts/load_gate.py` before every spawn; exit 75 means wait 120
  seconds and check again. Never run full test suites.
- Hermetic. Every step runs in a world from `scripts/world.py`: isolated
  HOME and APM_HOME, local git remotes and local marketplaces, no
  credentials, no real network writes.
- Neutral briefs. Every brief passes `scripts/brief_lint.py` before it
  is sent. Some model families refuse security-worded briefs.
- No side effects outside the run directory: never fix code, commit,
  push, comment, file issues or tag. Security findings route privately.
- ASCII only in everything this skill writes.
- Reload `run.json` before every spawn and after every return. Do not
  rely on recall of earlier turns.

## Inputs

| Input | Meaning |
|---|---|
| `--sha SHA` or `--pr N` | the exact candidate; a PR resolves to its current head SHA |
| `--mode full\|recheck` | `full` hunts; `recheck` re-verifies open findings only |
| `--slots 2` | concurrent workers (hard maximum 2 unless the operator raises it) |
| `--catalog PATH` | operator-supplied journey catalogue; default is `tests/lifecycle/journeys/index.yaml` |
| `--journeys J01,E01` | restrict to these journey ids (default: risk-ranked full set) |
| `--findings PATH` | prior ledger, required for `recheck` |

Run directory: `<session files>/release-acceptance/<version>/<sha>/`.
State lives in `run.json` (inputs, phase, queue, per-slot status),
`findings.json` (ledger) and `coverage.csv`.

## Procedure

### P0 -- ground (deterministic)

1. Resolve the SHA: `git rev-parse <sha>`, or
   `gh pr view <N> --json headRefOid`. Record it in `run.json`.
2. Build or locate the candidate binary from that SHA and check
   `<apm> --version` prints the SHA prefix. If it prints another commit
   (an in-tree build on a branch), accept it only when
   `git diff --quiet <candidate> <printed> -- src/` succeeds, and record
   both SHAs. Otherwise stop.
3. Load the catalogue:
   `python3 scripts/journeys.py --repo-root <checkout> [--catalog PATH]`.
   Exit 3 means "catalogue unavailable": stop and report it. Never use
   an embedded or hand-converted copy.
4. `python3 scripts/load_gate.py`.

### P1 -- recall (Atlas)

Read `references/atlas-protocol.md` now. Probe for Atlas, read the
listed lessons and decisions, and write the one-page wisdom block into
`run.json`. If Atlas is unavailable, record that and continue.

### P2 -- plan (risk-ranked queue)

1. List what changed since the last tag: `git log --oneline <tag>..<sha>`
   and the `[Unreleased]` block of `CHANGELOG.md`.
2. Map changed files to owners (`.apm/architecture/owners/`) and owners
   to journeys and lenses with `references/lenses.md`.
3. Rank journey x lens pairs: golden journeys first, then pairs that
   touch changed owners, then pairs where the wisdom block shows
   recurring breaks, then the rest. Add the flagship chain in a full
   pass.
4. Write the queue to `run.json`. In `recheck` mode the queue is the
   open findings from `--findings` instead.

### P3 -- walk (waves of at most 2 workers)

Read `references/walker-brief.md` and `references/lenses.md` now.

- Wave 1: plain journeys with lenses L1 and L2.
- Gate: merge the new findings
  (`python3 scripts/findings.py add --ledger findings.json --findings <worker>/findings.jsonl`),
  drop duplicates, check whether the PR head moved (if it did, record
  the new SHA and re-run only the affected steps), and re-rank.
- Wave 2: lenses L3 to L6 on the ranked journeys.

For each queue item: fill the walker brief, lint it, check the load gate,
spawn the worker with the brief as its whole prompt, and wait. One
journey and one lens per worker.

In `recheck` mode: one worker re-runs each finding's repro and replays
the original step at the new SHA, then reports VERIFIED-FIXED,
STILL-OPEN, CHANGED or FALSE-POSITIVE. Apply them with
`python3 scripts/findings.py recheck --ledger findings.json --verdicts <file> --sha <sha>`.

### P4 -- verify (independent, cold)

Read `references/verifier-brief.md` now. For every new finding, spawn a
verifier with the finding record only, preferably on a different model
family. Apply its verdict: CONFIRMED sets status `verified` and may
change severity; FALSE-POSITIVE closes it; CHANGED records the new
observation. A finding the verifier cannot reproduce in 2 attempts is a
false positive. Apply verdicts with
`python3 scripts/findings.py verify --ledger findings.json --verdicts <file>`.

### P5 -- synthesize, convert, checkpoint

Read `references/rubric.md` now.

1. Severity and routing per the rubric. Record them with
   `python3 scripts/findings.py set --ledger findings.json --id RA-NNN route=<lane> pre_existing=<true|false>`.
   A pre-existing non-security finding becomes `status=known-issue`
   unless the release claims to fix it; a security finding never does.
2. For every verified finding, write a failing-before test brief for
   `apm-lifecycle-tests` (template below). The finding keeps status
   `verified` and `lifecycle_test: null` until a test node exists that
   fails on the unfixed code; then record the node id.
3. Coverage: `python3 scripts/findings.py coverage --csv coverage.csv`.
4. Gate row: `python3 scripts/findings.py gate-row --ledger findings.json --sha <sha>`.
5. Write `REPORT.md` from `assets/report-template.md`.
6. HUMAN CHECKPOINT: present the gate row, the findings table and the
   routing to the human or coordinator who started the pass. Do not
   publish the row or route anything until they approve.

Failing-before test brief (one per verified finding):

```text
For apm-lifecycle-tests:
PROMISE: <id or NEW> -- <sentence> (source: <docs page, CHANGELOG line or spec req>)
CELL: <factor tuple from the finding>
OWNER: <owner id or path:line>
FAILING-BEFORE: <exact command sequence> -> <observed line> (at <sha>)
EXPECTED: <what the promise requires>
ORACLES: <I1-I9 and hint-truth as relevant>
FROM: release-acceptance finding <RA-NNN>
```

### P6 -- persist (Atlas drafts)

Follow `references/atlas-protocol.md`: write the experience page draft
and the break-pattern update into the run directory and hand them to
the designated Atlas writer. Update `run.json` to `done`.

## Scripts

All are non-interactive, print JSON on stdout and diagnostics on
stderr, and support `--help`.

| Script | Use |
|---|---|
| `scripts/load_gate.py` | host load check before each spawn (exit 75 = wait) |
| `scripts/world.py` | create a hermetic world (host git config and credentials scrubbed); `--apm-bin` puts the candidate first on PATH; `--git-host` maps a fake host to local bare repos |
| `scripts/capture.py` | run one step in a world; stdout, stderr and exit code saved separately; line lint; hints and docs links extracted |
| `scripts/journeys.py` | load the program catalogue through one schema check; refuses when unavailable |
| `scripts/findings.py` | ledger: `add` (dedupe across journeys), `verify` (P4 verdicts), `set` (route, pre_existing, lifecycle_test, known-issue), `coverage`, `gate-row` (GO, NO-GO, PENDING-VERIFICATION), `recheck` |
| `scripts/brief_lint.py` | neutral-wording check for every brief |

## Output

- `REPORT.md` with the release-gate row, findings, coverage and gaps.
- `findings.json`, `coverage.csv`, `run.json`.
- One failing-before test brief per verified finding.
- Atlas drafts (experience page, break-pattern update).

## References (load on trigger)

- `references/atlas-protocol.md` -- at P1 and P6.
- `references/lenses.md` -- at P2 and P3.
- `references/walker-brief.md` -- at P3, before the first spawn.
- `references/verifier-brief.md` -- at P4.
- `references/rubric.md` -- at P5, and whenever a severity is unclear.
- `assets/report-template.md` -- at P5 step 5.
