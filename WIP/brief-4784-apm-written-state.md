# Design brief: persisted APM-written MCP/hook state (#4784, #4864, #4867; context #4899)

Owner: P3 lead (9a22ec4b) with QL. Rule 22: hourly steps, each a pushed SHA.
Status: STEP 2 OPTIONS (Mon 09:40). Steps: S1 outline (09:40) -> S2 options A/B/C/P
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

## 4. Options: mechanism and call sites (STEP 2)
Shared anchor: `McpWriteReceipts.retire_undeclared` (install/mcp/write_receipts.py:329).
Today it skips a record when `write.name in declared` (line 347), so a still-declared
server is never retired from a runtime the project dropped. Records are already keyed
(runtime, config, native key) (#4818); the gap is the NAME-level skip, not the key.

### C. Report only (no behavior change to removal)
- Mechanism: in `retire_undeclared`, a record whose server is declared but whose
  runtime is outside this run's deployed runtimes goes to `mark_kept` + `warn_kept`
  ("Kept MCP server '<name>' in <config>: APM no longer targets <runtime>").
- Needs: the set of runtimes deployed this run, from the install journal
  (`note_mcp_write`, line 118) passed by `retire_undeclared_mcp_writes` (line 435).
- `audit --ci` already fails on kept-mcp-entries, so the "passes silently" half closes.
- Schema: none. Spec: none (reporting only). Satisfies QL cell arm "kept AND reported".

### A. Retire by deployed destination, with an explicit skip set
- Mechanism: replace the NAME skip with a DESTINATION skip: keep the record iff
  (runtime, config, key) was deployed this run OR its runtime is in `skipped`
  (--exclude, unavailable/undetected runtime, failed write). Otherwise, if proven,
  `_remove_native_entry` as for undeclared servers.
- Needs: journal of deployed destinations + a skip-reason set produced by the MCP
  integrator (mcp_integrator_install); retirement only on full-project installs
  (not `apm install <pkg>`, --only); keep `_selected` and `_declared_keys` guards.
- Schema: none if the skip set is per run; behavior change = removal, so Regent memo.

### B. Persistent per-runtime target baseline (shared with #4864)
- Mechanism: the lockfile (or receipt store) records, per runtime, the declaration
  baseline APM last wrote (additive field). Retire only for runtimes outside the
  project's resolved target set, never on per-run skips; a refused retirement keeps
  its old per-runtime baseline (#4864) so a newly targeted runtime is judged on its own.
- Needs: lockfile field + reader/writer round-trip; OpenAPM lockfile reference update.
- Schema: additive lockfile field (old readers ignore it). Behavior change = removal,
  so Regent memo; one schema change should serve #4784 and #4864 together.

### P. File-creation provenance (#4867, orthogonal)
- Mechanism: when APM creates a native MCP/hook config file, the receipt store records
  "created by APM" for that file; deleting a file APM emptied requires that record.
  Files without the record keep today's content-proof behavior until APM next creates them.
- Call sites: the emptied-file delete paths (MCP config writers; hook_integrator cleanup).
- Schema: additive receipt field. Tightens deletion only (safety-positive): no memo.

### Overlap
- #4864 == B's baseline (same field); #4784 can ship as C now and A or B later.
- #4899 stays narrow (existing fields) per its record; it must not pre-empt B's field.

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
