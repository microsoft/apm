# Design brief: persisted APM-written MCP/hook state (#4784, #4864, #4867; context #4899)

Owner: P3 lead (9a22ec4b) with QL. Rule 22: hourly steps, each a pushed SHA.
Status: STEP 1 OUTLINE (Mon 08:45). Steps: S1 outline (09:40) -> S2 options A/B/C/P
with mechanism and call sites (10:40) -> S3 risks + OpenAPM/compat cells per option (11:40)
-> S4 QL review folded + recommendation + decision memo for A/B (12:40).
Any option that changes MCP retirement behavior goes to the Regent as a decision memo.

## 1. Shared question
Which records of state that APM itself wrote (per runtime, config file and native key)
does APM persist, and when may APM act on them (retire, keep, or delete a file)?
Invariant for every option: APM never vouches for bytes it did not write, and never
removes an entry or file it cannot prove it wrote. Where proof is ambiguous, keep the
entry and report it.

## 2. The three asks
| Issue | Trigger | What is missing today |
|---|---|---|
| #4784 | No lockfile; a server stays declared; a runtime is no longer targeted | Retirement skips by server NAME (write_receipts.py ~341), so the untargeted runtime's entry stays and `audit --ci` passes silently |
| #4864 | Lockfile present; a retirement was refused (kept) and the targets change | No per-runtime declaration baseline, so a newly targeted runtime could be vouched for with an old baseline |
| #4867 | APM empties a native MCP/hook config file | No file-creation provenance, so a user-created (empty) skeleton could be deleted when APM empties it |
| #4899 (context) | Refused-retirement baseline native-key mismatch | Narrow fix via existing fields (Intake record); any new per-runtime field is decided here |

## 3. Evidence
- ENG-B factual note (current behavior, keying options, 7 risks):
  session cda0c81a files/4784-design-note.md
- QL strict cell (XFAIL on CI 45079492557):
  tests/lifecycle/scenarios/test_mcp_write_receipts.py::
  test_an_apm_written_entry_for_a_runtime_no_longer_targeted_is_retired_without_the_lockfile
  (done-when: cursor entry retired, OR kept AND reported)
- INT draft scopes: session 982e07e9 files/records/4864.md, records/4867.md

## 4. Options (to fill in with QL)
- C (report only): a declared server on an untargeted runtime is kept and reported
  ("Kept MCP server ..." + audit --ci kept-mcp-entries). No schema change. This satisfies
  the strict cell's "kept AND reported" arm.
- A (deployed-destination set + explicit skip set): retire a proven record whose
  (runtime, config, key) was not deployed this run, only on full-project installs,
  never for --exclude / unavailable / failed-write skips.
- B (persistent per-runtime target set / baseline): record per-runtime state in the
  lockfile or receipts (additive). Same mechanism as #4864's baseline.
- P (provenance, #4867): record file creation by APM; delete an emptied file only
  with that record. Orthogonal to A/B/C, and shares the receipt store.

## 5. Risks to weigh (from ENG-B, to be confirmed by QL)
transient vs durable skips; auto-detected targets; shared destinations; partial installs;
COPILOT_HOME profiles; user-held identical entries; test surface.

## 6. OpenAPM / compat
To list per option: spec reqs touched (lockfile fields: additive only), old-reader and
new-reader round-trip cells, CHANGELOG.

## 7. Recommendation (draft, pending QL)
- 0.38.x: C for #4784 (report, no removal); #4867 P (additive provenance, delete-guard
  only, which tightens deletion: safety-positive); #4899 narrow per its record.
- 0.39: A+B decided together with #4864 (one schema change for the per-runtime record).
- Decision memo to the Regent: required for A/B (they change retirement behavior),
  not for C or P (they only report, or only add a deletion guard).
